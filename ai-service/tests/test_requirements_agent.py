"""Offline tests for the real graph, tool execution, checkpointing and streaming transport."""

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, ToolMessage

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agents.api import create_router
from agents.brief import Brief, Preference, RequirementsUpdate, apply_update, inconsistencies
from agents.graph import build_graph
from agents.tools import lookup_supported_options


def call(name, args):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": str(uuid4())}])


def choice(field, value, **kwargs):
    return Preference(field=field, value=value, intent=kwargs.pop("intent", "required"), **kwargs)


def update(*choices, question=None, reply="Saved your preferences.", **kwargs):
    return RequirementsUpdate(informational=False, new_design=False, replacements=list(choices),
                              clear_fields=[], conflicts=[], reply=reply, question=question, **kwargs)


class RequirementsTests(unittest.TestCase):
    def setUp(self):
        catalog = patch("agents.brief.load_birthstones", return_value=[
            ("February", "Amethyst Purple", "Round"), ("February", "Amethyst Purple", "Oval"),
            ("March", "Aquamarine Blue", "Pear"),
        ])
        self.birthstones = catalog.start()
        self.addCleanup(catalog.stop)

    def test_checkpointed_brief_and_conditional_routes(self):
        # The scripted model supplies tool decisions. All tools, nodes, state and HTTP handling are real.
        supervisor = Mock()
        supervisor.invoke.side_effect = [
            call("DelegateRequirements", {"request": "Collect the necklace brief."}),
            call("DelegateRequirements", {"request": "Update the same brief."}),
            call("ConsultGuides", {"question": "How do I clean a pearl?"}),
        ]
        specialist = Mock()
        specialist.invoke.side_effect = [
            call("lookup_supported_options", {"jewelry_type": "necklace"}),
            AIMessage(content="Which length?"), AIMessage(content="Which length?"),
        ]
        structure = Mock()
        structure.invoke.side_effect = [
            update(choice("jewelry_type", "necklace"), choice("metal", "silver"),
                   choice("budget_max", "USD 150", amount=150, unit="USD"),
                   question="Which length?", reply="Which length?"),
            update(choice("budget_max", "USD 100", amount=100, unit="USD"),
                   choice("chain", "your choice", intent="delegated"), question="Which length?"),
        ]
        model = Mock()
        model.bind_tools.side_effect = [supervisor, specialist]
        model.with_structured_output.return_value = structure
        source = {"label": "T1", "file_name": "Pearl care.pdf", "kind": "text"}
        knowledge = Mock(return_value={"answer": "Use a soft cloth. [T1]", "sources": [source]})
        graph = build_graph(knowledge, model)
        app = FastAPI()
        app.include_router(create_router(knowledge, lambda _: graph))

        with TestClient(app) as client:
            def send(thread, message):
                response = client.post("/api/assistant/chat", json={"conversation_id": thread, "message": message})
                self.assertEqual(response.status_code, 200)
                events = [json.loads(line) for line in response.text.splitlines()]
                self.assertEqual(events[-1]["type"], "result", events)
                return events

            first_id = str(uuid4())
            first = send(first_id, "I want a silver necklace under USD 150.")
            self.assertEqual(first[-1]["answer"].count("?"), 1)
            self.assertIn("Which length", first[-1]["brief"]["next_question"])
            self.assertTrue(any("supported jewelry" in e.get("message", "") for e in first))
            observations = [m for m in specialist.invoke.call_args_list[1].args[0] if isinstance(m, ToolMessage)]
            self.assertIn("lengths_inches", observations[0].content)
            second = send(first_id, "Actually USD 100 and you can choose the chain.")
            preferences = {p["field"]: p for p in second[-1]["brief"]["preferences"]}
            self.assertEqual(preferences["budget_max"]["amount"], 100)
            self.assertEqual(preferences["metal"]["value"], "silver")
            self.assertEqual(preferences["chain"]["intent"], "delegated")
            self.assertFalse(any("supported jewelry" in e.get("message", "") for e in second))
            other = send(str(uuid4()), "How do I clean a pearl?")[-1]
            self.assertEqual(other["brief"]["preferences"], [])
            self.assertEqual(other["sources"], [source])
            self.assertIn("[T1]", other["answer"])
            knowledge.assert_called_once()

    def test_handoff_and_conflicts(self):
        ring = [choice("jewelry_type", "ring"), choice("metal", "silver"),
                choice("style", "Aurora Band"), choice("stone_count", "1"), choice("stone", "Ruby"), choice("cut", "Oval"),
                choice("measurement", "17.3 mm inner diameter", amount=17.3, unit="mm")]
        brief, _ = apply_update(Brief(), update(*ring))
        self.assertEqual(brief.status, "ready")
        recovered, reply = apply_update(brief, update(choice("measurement", "17.3 millimeters inner diameter"),
                                        question="What is the unit?", reply="What is the unit?"))
        self.assertEqual(recovered.status, "ready")
        self.assertEqual(recovered.choices("measurement")[0].amount, 17.3)
        self.assertNotIn("?", reply)
        self.assertEqual(choice("stone", "none", intent="excluded").value, "stones")
        self.assertEqual(choice("personalization", "no engraving", intent="excluded").value, "engraving")
        invalid, _ = apply_update(brief, update(choice("measurement", "US 7", amount=7, unit="US")))
        self.assertEqual(invalid.status, "collecting")
        invalid, _ = apply_update(brief, update(choice("metal", "platinum")))
        self.assertEqual(invalid.status, "collecting")
        invalid, _ = apply_update(brief, update(choice("stone", "sodalite")))
        self.assertEqual(invalid.status, "collecting")
        self.assertTrue(inconsistencies(Brief(preferences=[
            choice("budget_min", "USD 200", amount=200, unit="USD"),
            choice("budget_max", "USD 100", amount=100, unit="USD")])))
        self.assertTrue(inconsistencies(Brief(preferences=[
            choice("stone", "pearl"), choice("stone", "Pearl", intent="excluded")])))

    def test_offered_answers_work_without_model_calls(self):
        model = Mock()
        graph = build_graph(Mock(), model)
        thread = {"configurable": {"thread_id": str(uuid4())}}
        for answer, next_options in [("necklace", ["Gold"]), ("Gold", ["14 inches"]),
                                     ("14 inches", ["charm", "birthstone"]), ("Birthstone", ["Cable"]), ("Cable", ["February"]),
                                     ("February", ["Amethyst Purple"]), ("Amethyst Purple", ["Round"]), ("Round", [])]:
            result = graph.invoke({"message": answer}, thread)
            self.assertTrue(set(next_options).issubset(result["brief"]["options"]))
        self.assertEqual(result["brief"]["status"], "ready")
        self.assertEqual(result["brief"]["options"], [])
        self.assertIn("Round", result["answer"])
        model.bind_tools.return_value.invoke.assert_not_called()
        model.with_structured_output.return_value.invoke.assert_not_called()

    def test_unavailable_ring_style_never_enters_the_saved_brief(self):
        previous, _ = apply_update(Brief(), update(choice("jewelry_type", "ring"), choice("metal", "Rose Gold"),
            choice("measurement", "18.2 mm"), choice("personalization", "Hala")))
        request = update(choice("style", "halo"))
        request.conflicts = ["Style halo is not available; customer must pick a different style."]
        rejected, reply = apply_update(previous, request, "I want the style halo")
        self.assertEqual(rejected.preferences, previous.preferences)
        self.assertNotIn("recorded", reply)
        self.assertIn("halo", reply)
        self.assertNotIn("customer must", reply)
        self.assertEqual(rejected.option_field, "style")
        self.assertEqual(rejected.options, ["Aurora Band", "Celeste Solitaire", "Tennis Horizon"])
        self.assertEqual(rejected.status, "collecting")
        selected, _ = apply_update(rejected, update(choice("style", "Aurora Band")), "Aurora Band")
        self.assertEqual(selected.option_field, "stone_count")
        self.assertEqual(selected.choices("style")[0].value, "Aurora Band")
        self.assertFalse(selected.pending_options)

        edit = update(choice("style", "halo", intent="preferred"), choice("metal", "Gold"))
        edit.clear_fields = ["style"]
        revised, reply = apply_update(selected, edit)
        self.assertEqual(revised.choices("style"), selected.choices("style"))
        self.assertEqual(revised.choices("metal")[0].value, "Gold")
        self.assertNotIn("style: halo", reply)
        self.assertEqual(revised.option_field, "style")

    def test_multiple_catalog_rejections_can_be_resolved_one_at_a_time(self):
        brief, _ = apply_update(Brief(), update(choice("jewelry_type", "ring"), choice("metal", "platinum"),
                                choice("measurement", "100 mm"), choice("style", "Aurora Band"), choice("stone_count", "0")))
        self.assertEqual(brief.pending_options, ["metal", "measurement"])
        self.assertFalse(brief.choices("metal"))
        self.assertFalse(brief.choices("measurement"))
        brief, _ = apply_update(brief, update(choice("metal", "Silver")))
        self.assertEqual(brief.option_field, "measurement")
        brief, _ = apply_update(brief, update(choice("measurement", "1.82 cm")))
        self.assertEqual(brief.status, "ready")

    def test_only_current_style_details_remain_after_switching(self):
        for kind, length in (("necklace", "20 inches"), ("bracelet", "7 inches")):
            with self.subTest(kind=kind):
                base, _ = apply_update(Brief(), update(choice("jewelry_type", kind), choice("metal", "Silver"),
                    choice("measurement", length), choice("budget_max", "100 USD", unit="USD", amount=100),
                    choice("chain", "Cable"), choice("style", "name"), choice("personalization", "Hala"), choice("lettering_style", "Bubble")))
                self.assertEqual(base.status, "ready")
                info = update()
                info.informational, info.options_requested = True, "style"
                browsing, _ = apply_update(base, info, "What other styles are there?")
                self.assertEqual(browsing.preferences, base.preferences)
                self.assertEqual(browsing.options, ["name", "charm", "birthstone"])
                revised, reply = apply_update(browsing, update(choice("style", "birthstone"),
                    choice("birth_month", "February"), choice("personalization", "Hala")),
                    "Okay I want to switch to birthstone. My birth month is February.")
                self.assertEqual(revised.missing, ["stone_color", "cut"])
                self.assertNotIn("Hala", reply)
                self.assertFalse(revised.review_reasons)
                self.assertCountEqual([p.field for p in revised.preferences], [
                    "jewelry_type", "metal", "measurement", "budget_max", "chain", "style", "birth_month"])
                self.assertEqual(revised.choices("birth_month")[0].value, "February")
                charm, _ = apply_update(revised, update(choice("style", "charm"), choice("charm", "heart")))
                self.assertEqual(charm.missing, ["charm_color"])
                self.assertFalse(charm.choices("birth_month"))
                named, _ = apply_update(charm, update(choice("style", "name")))
                self.assertEqual(named.missing, ["personalization", "lettering_style"])
                self.assertFalse(named.choices("charm"))

    def test_mixed_style_request_asks_for_one_choice(self):
        brief, reply = apply_update(Brief(), update(choice("jewelry_type", "necklace"),
            choice("style", "name", intent="preferred"), choice("style", "charm")))
        self.assertEqual(brief.choices("style"), [])
        self.assertEqual(brief.option_field, "style")
        self.assertEqual(brief.status, "collecting")
        self.assertIn("style", reply)
        self.assertEqual(brief.options, ["name", "charm", "birthstone"])

    def test_catalog_unavailable_is_explicit(self):
        import httpx
        with patch("agents.tools.httpx.get", side_effect=httpx.ConnectError("offline")):
            result = lookup_supported_options.invoke({"catalog": "materials", "jewelry_type": "ring"})
        self.assertFalse(result["available"])

    def test_birthstone_selection_reopens_only_missing_gem_details_after_a_compound_style(self):
        previous = Brief(preferences=[
            choice("jewelry_type", "necklace"), choice("metal", "silver"),
            choice("measurement", "14 inches"), choice("budget_max", "USD 100", amount=100, unit="USD"),
            choice("style", "name, heart charm and birthstone"), choice("personalization", "Mira engraved"),
            choice("charm", "heart"), choice("stone", "February"), choice("chain", "designer choice", intent="delegated"),
        ])
        stale = update(choice("style", "birthstone design"), reply="Everything is ready for production!",
                       question="Which style would you prefer?")
        for message in ("I prefer birthstone", "Birthstone.", "I'd like the birthstone design."):
            with self.subTest(message=message):
                brief, reply = apply_update(previous, stale, message)
                self.assertEqual([p.value for p in brief.choices("style")], ["birthstone"])
                self.assertEqual(brief.status, "collecting")
                self.assertEqual(brief.option_field, "stone_color")
                self.assertEqual(brief.missing, ["stone_color", "cut"])
                self.assertEqual(reply.count("?"), 1)
                self.assertNotIn("heart", reply)
                self.assertNotIn("Mira", reply)
                self.assertNotIn("ready for production", reply)
        plain = previous.model_copy(update={"preferences": [p for p in previous.preferences
                                     if p.field not in ("personalization", "charm")]})
        brief, reply = apply_update(plain, stale, "I prefer birthstone")
        self.assertEqual(brief.status, "collecting")
        self.assertEqual(reply.count("?"), 1)

    def test_blocked_reply_and_delegation_use_the_recorded_facts(self):
        previous = Brief(preferences=[choice("jewelry_type", "necklace"), choice("metal", "silver")],
                         next_question="Which length would you like?")
        patch = update(choice("chain", "designer", intent="delegated"),
                       choice("measurement", "designer", intent="delegated"),
                       reply="It is ready for production!", question="Which length and which style?")
        brief, reply = apply_update(previous, patch, "You can choose the chain.")
        self.assertEqual(brief.status, "collecting")
        self.assertFalse(brief.choices("measurement"))
        self.assertTrue(brief.choices("chain"))
        self.assertEqual(reply.count("?"), 1)
        self.assertNotIn("ready", reply)

    def test_charm_tool_uses_catalog_labels(self):
        response = Mock()
        response.json.return_value = [{"design": "shapes"}, {"design": "letter cursive"}]
        with patch("agents.tools.httpx.get", return_value=response) as get:
            result = lookup_supported_options.invoke({"catalog": "charms", "jewelry_type": "necklace"})
        self.assertTrue(get.call_args.args[0].endswith("/api/charms"))
        self.assertEqual(result["catalog"], ["letter cursive", "shapes"])
        self.assertIn("photos", result["note"])

    def test_ring_gem_counts_slots_and_revisions(self):
        ring, _ = apply_update(Brief(), update(choice("jewelry_type", "ring"), choice("metal", "14K Gold"),
            choice("measurement", "17.3 mm"), choice("style", "Celeste Solitaire")))
        self.assertEqual((ring.option_field, ring.options), ("stone_count", ["0", "1", "2"]))
        too_many, _ = apply_update(ring, update(choice("stone_count", "3")))
        self.assertFalse(too_many.choices("stone_count"))
        two, _ = apply_update(too_many, update(choice("stone_count", "2"), choice("stone", "Ruby")))
        self.assertEqual(two.missing, ["side_stone_1", "cut"])
        complete, _ = apply_update(two, update(choice("side_stone_1", "Lab-grown Pink"), choice("cut", "Oval")))
        self.assertEqual(complete.status, "ready")
        plain, _ = apply_update(complete, update(choice("stone_count", "0")))
        self.assertEqual(plain.status, "ready")
        self.assertFalse(any(plain.choices(f) for f in ("stone", "cut", "side_stone_1")))
        tennis, _ = apply_update(complete, update(choice("style", "Tennis Horizon")))
        self.assertEqual(tennis.status, "ready")
        self.assertFalse(any(tennis.choices(f) for f in ("stone_count", "cut", "side_stone_1")))
        aurora, _ = apply_update(tennis, update(choice("style", "Aurora Band"), choice("stone_count", "3")))
        self.assertEqual(aurora.missing, ["side_stone_1", "side_stone_2", "cut"])

    def test_earring_builders_and_birthstone_dependencies(self):
        earrings, _ = apply_update(Brief(), update(choice("jewelry_type", "earrings"), choice("metal", "Gold"),
            choice("style", "charm"), choice("charm", "letter cursive")))
        self.assertEqual(earrings.missing, ["earring_shape", "charm_color", "charm_text"])
        done, _ = apply_update(earrings, update(choice("earring_shape", "Dangling"), choice("charm_color", "Gold"),
            choice("charm_text", "Hala")))
        self.assertEqual(done.status, "ready")
        feb, _ = apply_update(done, update(choice("style", "birthstone"), choice("birth_month", "February"),
            choice("backing", "Clasp Back"), choice("stone_color", "Amethyst Purple"), choice("cut", "Oval")))
        self.assertEqual(feb.status, "ready")
        self.assertFalse(feb.choices("charm_text"))
        march, _ = apply_update(feb, update(choice("birth_month", "March")))
        self.assertEqual(march.missing, ["stone_color", "cut"])
        self.assertEqual(march.options, ["Aquamarine Blue"])
        invalid, _ = apply_update(march, update(choice("stone_color", "Aquamarine Blue"), choice("cut", "Round")))
        self.assertEqual(invalid.options, ["Pear"])
        self.assertFalse(invalid.choices("cut"))
        self.birthstones.return_value = None
        unavailable, reply = apply_update(invalid, update(choice("cut", "Pear")))
        self.assertEqual(unavailable.status, "collecting")
        self.assertIn("catalog", reply)

    def test_mixed_turn_preserves_both_results_without_supervisor_inventions(self):
        decision = call("DelegateRequirements", {"request": "Record necklace wishes"})
        decision.tool_calls += call("ConsultGuides", {"question": "What is a birthstone?"}).tool_calls
        supervisor, specialist, structure = Mock(), Mock(), Mock()
        supervisor.invoke.side_effect = [decision, AIMessage(content="I'll choose the measurement. Ready for production!")]
        specialist.invoke.return_value = AIMessage(content="Saved.")
        structure.invoke.return_value = update(choice("jewelry_type", "necklace"), choice("charm", "heart"),
                                               choice("personalization", "Mira engraved"))
        model = Mock()
        model.bind_tools.side_effect = [supervisor, specialist]
        model.with_structured_output.return_value = structure
        knowledge = lambda _: {"answer": "A gemstone associated with a month [T1].",
                               "sources": [{"label": "T1", "kind": "text"}]}
        graph = build_graph(knowledge, model)
        result = graph.invoke({"message": "Heart charm and Mira engraved on the necklace. What is a birthstone?"},
                              {"configurable": {"thread_id": str(uuid4())}})
        self.assertIn("[T1]", result["answer"])
        self.assertIn("Mira engraved", result["answer"])
        self.assertNotIn("choose the measurement", result["answer"])
        self.assertEqual(result["answer"].count("?"), 1)
        supervisor.invoke.assert_called_once()

    def test_clear_budget_and_start_separate_design(self):
        brief, _ = apply_update(Brief(), update(choice("jewelry_type", "necklace"),
            choice("metal", "silver"), choice("budget_max", "USD 100", amount=100, unit="USD")))
        remove_budget = update()
        remove_budget.clear_fields = ["budget_max"]
        brief, _ = apply_update(brief, remove_budget)
        self.assertFalse(brief.choices("budget_max"))
        self.assertTrue(brief.choices("metal"))
        new_piece = update(choice("jewelry_type", "earrings"))
        new_piece.new_design = True
        brief, _ = apply_update(brief, new_piece)
        self.assertFalse(brief.choices("metal"))
        self.assertEqual(brief.jewelry_type, "earrings")
        self.assertIn("style", brief.missing)

    def test_month_normalization_survives_style_switch(self):
        base = Brief(preferences=[choice("jewelry_type", "necklace"), choice("metal", "silver"),
                                  choice("measurement", "14 inches"),
                                  choice("budget_max", "USD 150", amount=150, unit="USD")])
        brief, _ = apply_update(base, update(choice("style", "birthstone"), choice("birth_month", "February"),
                                             choice("stone", "amethyst"), choice("other", "February")),
                                "I want a birthstone necklace. My birth month is February.")
        self.assertEqual(brief.missing, ["chain", "stone_color", "cut"])
        self.assertEqual([p.value for p in brief.choices("birth_month")], ["February"])
        self.assertFalse(brief.choices("stone"))
        self.assertFalse(brief.choices("other"))

        # Reopen a checkpoint like the screenshot, including an already saved stone.
        legacy = base.preferences + [choice("style", "birthstone"), choice("stone", "amethyst"), choice("other", "February")]
        brief = Brief.model_validate({"preferences": [p.model_dump() for p in legacy]})
        changed, _ = apply_update(brief, update(choice("style", "name"), choice("personalization", "Hala")),
                                  "Switch to a name necklace instead and engrave Hala.")
        self.assertEqual(changed.choices("style")[0].value, "name")

    def test_month_correction_and_unrelated_occasions(self):
        original = Brief(preferences=[choice("style", "birthstone"), choice("birth_month", "February")])
        revised, _ = apply_update(original, update(choice("other", "March")), "My birth month is March.")
        self.assertEqual([p.value for p in revised.choices("birth_month")], ["March"])
        occasion = Brief(preferences=[choice("other", "February")])
        self.assertFalse(occasion.choices("birth_month"))


if __name__ == "__main__":
    unittest.main()
