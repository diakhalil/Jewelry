from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool
from pydantic import BaseModel

from src.validation import agent as agent_module
from src.validation.agent import (
    VALIDATION_TOOLS,
    ValidationNarrative,
    create_validation_graph,
    run_validation_agent,
)


def _passed_result():
    return {
        "valid": True,
        "status": "passed",
        "complete_validation": True,
        "checks_valid": True,
        "checks": {
            "compatibility": {"status": "passed", "result": {"valid": True}},
            "catalog": {"status": "passed", "result": {"status": "matched"}},
            "inventory": {"status": "passed", "result": {"valid": True}},
            "price": {"status": "passed", "result": {"valid": True}},
        },
        "technical_errors": [],
        "clarifications": [],
        "conflicts": [],
        "unresolved_mappings": [],
    }


def _narrator(_context):
    return ValidationNarrative(response="I checked the design using the returned validation results.")


def _request(session_id="session-1", **overrides):
    result = {
        "session_id": session_id,
        "customer_preferences": {"metal": "gold"},
        "proposed_design": {"type": "ring", "materialID": "M-1"},
        "budget": 100,
        "inventory_request": {"material_id": "M-1", "stones": [], "charms": []},
        "price_request": {"design": {"type": "ring", "materialID": "M-1"}, "budget": 100},
        "message": "Please check this ring.",
    }
    result.update(overrides)
    return result


class _UnifiedModel:
    """Mock model that chooses unified validation, then observes its result."""

    def bind_tools(self, _tools, **_kwargs):
        return self

    def invoke(self, messages):
        if messages[-1].type == "tool":
            return AIMessage(content="I have considered the returned validation result.")
        data = _request()
        for message in reversed(messages):
            if message.type == "human" and "Current request state" in message.content:
                import json
                data = json.loads(message.content.split("Current request state (use these exact values for validation tool arguments):\n", 1)[1])
                break
        arguments = {
            "inventory_request": data.get("inventory_request") or {"material_id": None, "stones": [], "charms": []},
            "price_request": data.get("price_request") or {"design": data.get("proposed_design") or {}, "budget": data.get("budget")},
        }
        for name in (
            "catalog_preferences", "three_d_configuration", "three_d_field_provenance",
            "three_d_confirmed_components", "three_d_approved_mappings",
        ):
            if data.get(name) is not None:
                arguments[name] = data[name]
        return AIMessage(content="", tool_calls=[{
            "name": "run_unified_validation",
            "args": arguments,
            "id": "unified-call",
            "type": "tool_call",
        }])


class _CatalogInput(BaseModel):
    query: str


class _PriceInput(BaseModel):
    design: dict
    budget: float


class _RequestSelectingModel:
    def __init__(self):
        self.selections = []

    def bind_tools(self, _tools, **_kwargs):
        return self

    def invoke(self, messages):
        last = messages[-1]
        if last.type == "tool":
            return AIMessage(content="The focused check completed.")
        text = str(last.content).lower()
        if "resolve the catalog preference" in text:
            name, args = "resolve_catalog", {"query": "pink diamond"}
        else:
            name, args = "validate_price", {"design": {"type": "ring"}, "budget": 75}
        self.selections.append(name)
        return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"call-{len(self.selections)}", "type": "tool_call"}])


class _NoToolModel:
    def bind_tools(self, _tools, **_kwargs):
        return self

    def invoke(self, _messages):
        return AIMessage(content="Everything passed. This design is approved.")


class _AlwaysCallsInventoryModel:
    def bind_tools(self, _tools, **_kwargs):
        return self

    def invoke(self, _messages):
        return AIMessage(content="", tool_calls=[{
            "name": "validate_inventory",
            "args": {"material_id": "M-1", "stones": [], "charms": []},
            "id": "inventory-call",
            "type": "tool_call",
        }])


def _graph(**kwargs):
    kwargs.setdefault("agent_model", _UnifiedModel())
    return create_validation_graph(**kwargs)


class ValidationAgentTests(unittest.TestCase):
    def test_success_runs_unified_validation_and_routes_approved(self):
        calls = []

        def runner(**kwargs):
            calls.append(kwargs)
            return _passed_result()

        graph = _graph(validation_runner=runner, narrator=_narrator)
        state = run_validation_agent(_request(), graph=graph)
        self.assertEqual(state.routing_decision["route"], "approved")
        self.assertTrue(state.routing_decision["approved"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(state.handoff["status"], "ready_for_downstream_workflow")
        self.assertFalse(state.handoff["executed"])
        self.assertEqual(state.llm_status, "used")

    def test_unavailable_component_creates_revision_handoff_without_substitution(self):
        failed = _passed_result()
        failed.update({
            "valid": False,
            "complete_validation": False,
            "status": "failed",
            "checks_valid": False,
            "checks": {"inventory": {"status": "failed", "result": {"errors": ["Stone S-1 has insufficient stock."]}}},
            "errors": [{"check": "inventory", "message": "Stone S-1 has insufficient stock."}],
        })
        graph = _graph(validation_runner=lambda **_kwargs: failed, narrator=_narrator)
        state = run_validation_agent(_request(), graph=graph)
        self.assertEqual(state.routing_decision["route"], "revision_required")
        self.assertEqual(state.handoff["status"], "revision_handoff_pending")
        self.assertFalse(state.handoff["executed"])
        self.assertTrue(any("Preserve all other explicitly requested properties" in item for item in state.handoff["feedback"][0]["suggested_actions"]))

    def test_over_budget_result_keeps_budget_and_produces_revision_feedback(self):
        failed = _passed_result()
        failed.update({
            "valid": False,
            "complete_validation": False,
            "status": "failed",
            "checks_valid": False,
            "checks": {"price": {"status": "failed", "result": {"errors": ["Design exceeds the budget by 10."]}}},
            "errors": [{"check": "price", "message": "Design exceeds the budget by 10."}],
        })

        def narrator(_context):
            return ValidationNarrative(
                response="The price check did not pass.",
                revision_feedback=["Review the design while keeping the stated budget."],
            )

        graph = _graph(validation_runner=lambda **_kwargs: failed, narrator=narrator)
        state = run_validation_agent(_request(), graph=graph)
        self.assertEqual(state.routing_decision["route"], "revision_required")
        self.assertEqual(state.budget, 100)
        self.assertTrue(any("stated budget" in item for item in state.routing_decision["revision_constraints"]))
        self.assertIn("keeping the stated budget", state.final_response)
        self.assertEqual(state.llm_revision_feedback, ["Review the design while keeping the stated budget."])

    def test_unsupported_configuration_routes_to_customer_clarification(self):
        unsupported = _passed_result()
        unsupported.update({
            "valid": False,
            "complete_validation": False,
            "status": "failed",
            "checks_valid": False,
            "checks": {"compatibility": {"status": "failed", "result": {
                "valid": False,
                "unsupported_fields": ["engraving"],
                "errors": [{"field": "engraving", "message": "The generator cannot represent engraving."}],
            }}},
        })
        graph = _graph(validation_runner=lambda **_kwargs: unsupported, narrator=_narrator)
        state = run_validation_agent(_request(three_d_configuration={"kind": "ring", "engraving": "name"}), graph=graph)
        self.assertEqual(state.routing_decision["route"], "clarification_required")
        self.assertTrue(state.clarification_requests[0]["customer_confirmation_required"])

    def test_unresolved_catalog_mapping_routes_to_clarification(self):
        unresolved = _passed_result()
        unresolved.update({
            "valid": False,
            "complete_validation": False,
            "status": "clarification_required",
            "clarification_required": True,
            "checks_valid": False,
            "clarifications": [{"code": "ambiguous_catalog_match", "message": "Select a material.", "fields": ["materialID"]}],
            "unresolved_mappings": [{"field": "materialID"}],
            "checks": {"catalog": {"status": "clarification_required", "result": {"status": "ambiguous"}}},
        })
        graph = _graph(validation_runner=lambda **_kwargs: unresolved, narrator=_narrator)
        state = run_validation_agent(_request(), graph=graph)
        self.assertEqual(state.routing_decision["route"], "clarification_required")
        self.assertTrue(state.clarification_requests)
        self.assertEqual(state.handoff["recipient"], "customer")

    def test_revision_limit_routes_to_human_review(self):
        failed = {
            "valid": False,
            "complete_validation": False,
            "status": "failed",
            "checks": {"inventory": {"status": "failed", "result": {"errors": ["Unavailable"]}}},
            "technical_errors": [],
        }
        graph = _graph(validation_runner=lambda **_kwargs: failed, narrator=_narrator)
        state = run_validation_agent(_request(revision_state={"revision_attempts": 3, "max_revision_attempts": 3}), graph=graph)
        self.assertEqual(state.routing_decision["route"], "retry_limit_reached")
        self.assertEqual(state.handoff["status"], "human_review_required")

    def test_technical_error_does_not_request_design_revision(self):
        technical = {
            "valid": False,
            "complete_validation": False,
            "status": "error",
            "checks": {"inventory": {"status": "error", "result": {"backend_error": "connection"}}},
            "technical_errors": [{"check": "inventory", "message": "Backend unavailable."}],
        }
        graph = _graph(validation_runner=lambda **_kwargs: technical, narrator=_narrator)
        state = run_validation_agent(_request(), graph=graph)
        self.assertEqual(state.routing_decision["route"], "technical_error")
        self.assertEqual(state.revision_state.revision_attempts, 0)
        self.assertEqual(state.handoff["status"], "technical_handoff_pending")

    def test_llm_failure_uses_fallback_without_changing_deterministic_route(self):
        def broken_narrator(_context):
            raise RuntimeError("private provider details")

        graph = _graph(validation_runner=lambda **_kwargs: _passed_result(), narrator=broken_narrator)
        state = run_validation_agent(_request(), graph=graph)
        self.assertEqual(state.routing_decision["route"], "approved")
        self.assertEqual(state.llm_status, "unavailable")
        self.assertIn("RuntimeError", state.llm_error)
        self.assertNotIn("private provider details", state.final_response)
        self.assertIn("all required checks passed", state.final_response)

    def test_memory_persists_turns_and_revision_state_with_session_isolation(self):
        invalid = {
            "valid": False,
            "complete_validation": False,
            "status": "failed",
            "checks": {"inventory": {"status": "failed", "result": {"errors": ["Unavailable"]}}},
            "technical_errors": [],
        }
        graph = _graph(validation_runner=lambda **_kwargs: invalid, narrator=_narrator)
        first_a = run_validation_agent(_request("customer-a", message="A first turn"), graph=graph)
        first_b = run_validation_agent(_request("customer-b", message="B private turn"), graph=graph)
        second_a = run_validation_agent({"session_id": "customer-a", "message": "A follow up"}, graph=graph)
        self.assertEqual(first_a.revision_state.revision_attempts, 1)
        self.assertEqual(second_a.routing_decision["route"], "retry_limit_reached")
        self.assertEqual(len(second_a.conversation_messages), 4)
        self.assertEqual(len(first_b.conversation_messages), 2)
        self.assertNotIn("A first turn", [message.content for message in first_b.conversation_messages])
        self.assertNotIn("B private turn", [message.content for message in second_a.conversation_messages])

    def test_validation_tools_are_structured_and_compatibility_tool_runs(self):
        names = {tool.name for tool in VALIDATION_TOOLS}
        self.assertEqual(names, {
            "validate_3d_compatibility", "resolve_catalog", "adapt_three_d_configuration",
            "validate_inventory", "validate_price", "run_unified_validation"
        })
        result = next(tool for tool in VALIDATION_TOOLS if tool.name == "validate_3d_compatibility").invoke({
            "configuration": {"kind": "ring", "style": "solitaire", "metal": "gold", "gem": "diamond", "color": "white", "size": 7}
        })
        self.assertTrue(result["valid"])

    def test_react_selects_different_tools_for_different_requests(self):
        selected = []

        def resolve_catalog(query: str):
            selected.append("resolve_catalog")
            return {"status": "not_found", "query": query}

        def validate_price(design: dict, budget: float):
            selected.append("validate_price")
            return {"valid": True, "computed_price": 50, "budget": budget, "design": design}

        tools = [
            StructuredTool.from_function(resolve_catalog, name="resolve_catalog", description="Resolve catalog preference", args_schema=_CatalogInput),
            StructuredTool.from_function(validate_price, name="validate_price", description="Check price", args_schema=_PriceInput),
        ]
        model = _RequestSelectingModel()
        graph = create_validation_graph(tools=tools, agent_model=model, narrator=_narrator)
        catalog_state = run_validation_agent(_request("catalog", message="Please resolve the catalog preference."), graph=graph)
        price_state = run_validation_agent(_request("price", message="Please check only the price."), graph=graph)

        self.assertEqual(model.selections, ["resolve_catalog", "validate_price"])
        self.assertEqual(selected, model.selections)
        self.assertEqual(catalog_state.routing_decision["route"], "clarification_required")
        self.assertEqual(price_state.routing_decision["route"], "clarification_required")
        self.assertEqual(catalog_state.tool_observations[0]["tool"], "resolve_catalog")
        self.assertEqual(price_state.tool_observations[0]["tool"], "validate_price")

    def test_model_claim_without_unified_evidence_cannot_approve(self):
        graph = create_validation_graph(agent_model=_NoToolModel(), narrator=_narrator)
        state = run_validation_agent(_request(), graph=graph)
        self.assertFalse(state.routing_decision["approved"])
        self.assertEqual(state.routing_decision["route"], "clarification_required")
        self.assertFalse(state.validation_results["complete_validation"])

    def test_mismatched_unified_tool_arguments_cannot_establish_approval(self):
        runner_calls = []

        class MismatchedModel:
            def bind_tools(self, _tools, **_kwargs):
                return self

            def invoke(self, _messages):
                return AIMessage(content="", tool_calls=[{
                    "name": "run_unified_validation",
                    "args": {
                        "inventory_request": {"material_id": "OTHER", "stones": [], "charms": []},
                        "price_request": {"design": {"type": "ring"}, "budget": 100},
                    },
                    "id": "wrong-design",
                    "type": "tool_call",
                }])

        def runner(**_kwargs):
            runner_calls.append(True)
            return _passed_result()

        state = run_validation_agent(
            _request(),
            graph=create_validation_graph(agent_model=MismatchedModel(), validation_runner=runner, narrator=_narrator),
        )
        self.assertFalse(state.routing_decision["approved"])
        self.assertNotEqual(state.routing_decision["route"], "approved")
        self.assertEqual(runner_calls, [])

    def test_react_tool_round_limit_stops_repeated_calls(self):
        calls = []

        def inventory(material_id: str | None = None, stones: list | None = None, charms: list | None = None):
            calls.append(material_id)
            return {"valid": True, "errors": [], "warnings": []}

        inventory_tool = StructuredTool.from_function(
            inventory,
            name="validate_inventory",
            description="Check inventory",
            args_schema=agent_module._InventoryToolInput,
        )
        graph = create_validation_graph(
            agent_model=_AlwaysCallsInventoryModel(),
            tools=[inventory_tool],
            max_react_steps=2,
            narrator=_narrator,
        )
        state = run_validation_agent(_request(), graph=graph)
        self.assertEqual(len(calls), 2)
        self.assertFalse(state.routing_decision["approved"])
        self.assertFalse(state.validation_results["complete_validation"])

    def test_tool_exception_is_a_technical_failure_not_a_design_failure(self):
        def fail_inventory(material_id: str | None = None, stones: list | None = None, charms: list | None = None):
            raise RuntimeError("private backend details")

        inventory_tool = StructuredTool.from_function(
            fail_inventory,
            name="validate_inventory",
            description="Check inventory",
            args_schema=agent_module._InventoryToolInput,
        )
        graph = create_validation_graph(
            agent_model=_AlwaysCallsInventoryModel(),
            tools=[inventory_tool],
            max_react_steps=1,
            narrator=_narrator,
        )
        state = run_validation_agent(_request(), graph=graph)
        self.assertEqual(state.routing_decision["route"], "technical_error")
        self.assertEqual(state.revision_state.revision_attempts, 0)
        self.assertNotIn("private backend details", str(state.validation_results))

    def test_existing_gemini_compatible_provider_is_called_with_structured_json_mode(self):
        captured = {}
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs: (
            captured.update(kwargs) or SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content='{"response":"The selected design needs review.","clarification_questions":[],"revision_feedback":[]} '
            ))])
        ))))
        provider = SimpleNamespace(
            DEFAULT_LLM_MODEL="configured-model",
            get_llm_client=lambda: client,
        )
        context = {
            "route": "revision_required",
            "deterministic_response": "Revise based on the price check.",
            "feedback": [{"failed_check": "price", "suggested_actions": ["Keep budget unchanged."]}],
            "clarifications": [],
            "validation_results": {"checks": {"price": {"status": "failed"}}},
            "customer_preferences": {"metal": "gold"},
            "proposed_design": {"type": "ring"},
            "budget": 100,
        }
        with patch.object(agent_module, "_rag_generation_module", return_value=provider):
            response = agent_module._generate_narrative(context)
        self.assertEqual(response.response, "The selected design needs review.")
        self.assertEqual(captured["model"], "configured-model")
        self.assertEqual(captured["response_format"], {"type": "json_object"})
        self.assertNotIn("API_KEY", str(captured["messages"]))

    def test_react_model_uses_configured_gemini_provider_and_binds_registry(self):
        captured = {}

        class FakeProviderMessage:
            def model_dump(self, exclude_none=True):
                return {"role": "assistant", "content": "ready"}

        class FakeClient:
            class chat:
                class completions:
                    @staticmethod
                    def create(**kwargs):
                        captured.update(kwargs)
                        return SimpleNamespace(
                            id="response-id",
                            choices=[SimpleNamespace(message=FakeProviderMessage(), finish_reason="stop")],
                        )

        provider = SimpleNamespace(
            DEFAULT_LLM_MODEL="configured-model",
            get_llm_client=lambda: FakeClient(),
        )
        with patch.object(agent_module, "_rag_generation_module", return_value=provider):
            model = agent_module._get_tool_calling_model()
        response = model.invoke([SystemMessage(content="system"), HumanMessage(content="request")])

        self.assertEqual(model.model, "configured-model")
        self.assertEqual({tool["function"]["name"] for tool in model.tools}, {tool.name for tool in VALIDATION_TOOLS})
        self.assertEqual(captured["model"], "configured-model")
        self.assertEqual(captured["tool_choice"], "auto")
        self.assertEqual(len(captured["tools"]), len(VALIDATION_TOOLS))
        self.assertEqual(response.content, "ready")

    def test_gemini_raw_tool_metadata_and_call_ids_survive_tool_round_trip(self):
        captured = []
        signature_fixture = "opaque-signature-fixture"

        class FakeProviderMessage:
            def __init__(self, payload):
                self.payload = payload

            def model_dump(self, exclude_none=True):
                return self.payload

        class FakeClient:
            def __init__(self):
                self.responses = [
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{
                            "id": "gemini-call-123",
                            "type": "function",
                            "function": {"name": "resolve_catalog", "arguments": '{"catalog_preferences": {"stone": "diamond", "color": "pink"}}'},
                            "extra_content": {"google": {"thought_signature": signature_fixture}},
                        }],
                    },
                    {"role": "assistant", "content": "The catalog did not match; please confirm an alternative."},
                ]

            class chat:
                class completions:
                    @staticmethod
                    def create(**kwargs):
                        captured.append(kwargs)
                        payload = fake_client.responses.pop(0)
                        return SimpleNamespace(
                            id=f"response-{len(captured)}",
                            choices=[SimpleNamespace(message=FakeProviderMessage(payload), finish_reason="tool_calls" if "tool_calls" in payload else "stop")],
                        )

        fake_client = FakeClient()

        def resolve_catalog(catalog_preferences: dict):
            return {"status": "not_found", "alternatives": ["real catalog result"]}

        catalog_tool = StructuredTool.from_function(
            resolve_catalog,
            name="resolve_catalog",
            description="Resolve requested catalog preferences",
            args_schema=agent_module._CatalogToolInput,
        )
        model = agent_module._GeminiToolCallingModel(client=fake_client, model="configured-model", tools=[catalog_tool])
        system = SystemMessage(content="system")
        user = HumanMessage(content="Resolve pink diamond")
        assistant = model.invoke([system, user])
        call = assistant.tool_calls[0]
        observation = ToolMessage(
            content=json.dumps(catalog_tool.invoke(call["args"])),
            tool_call_id=call["id"],
            name=call["name"],
        )
        final = model.invoke([system, user, assistant, observation])

        replayed_assistant = captured[1]["messages"][2]
        replayed_tool = captured[1]["messages"][3]
        self.assertEqual(replayed_assistant["tool_calls"][0]["extra_content"]["google"]["thought_signature"], signature_fixture)
        self.assertEqual(replayed_assistant["tool_calls"][0]["id"], call["id"])
        self.assertEqual(replayed_tool["tool_call_id"], call["id"])
        self.assertEqual(replayed_assistant["role"], "assistant")
        self.assertEqual(replayed_tool["role"], "tool")
        self.assertEqual(final.content, "The catalog did not match; please confirm an alternative.")

    def test_provider_error_details_are_useful_and_redacted(self):
        error = RuntimeError("provider rejected this request")
        error.status_code = 400
        error.body = [{"error": {
            "status": "INVALID_ARGUMENT",
            "message": "Function call is missing a thought_signature; api_key=super-secret-value",
        }}]
        details = agent_module._safe_provider_error(error)
        self.assertIn("HTTP 400", details)
        self.assertIn("INVALID_ARGUMENT", details)
        self.assertIn("thought_signature", details)
        self.assertNotIn("super-secret-value", details)

    def test_not_found_catalog_with_alternatives_routes_to_customer_confirmation(self):
        catalog_result = {
            "status": "not_found",
            "errors": [{"component": "stones", "message": "No exact catalog match was found."}],
            "matches": {"stones": [{
                "status": "not_found",
                "requested_attributes": {"gem": "Diamond", "color": "Pink"},
                "alternatives": [{
                    "id": "S019",
                    "label": "alternative; requires explicit confirmation",
                    "metadata": {"gem": "Sapphire", "color": "Pink"},
                }],
            }]},
        }
        incomplete = agent_module._focused_tool_results([{
            "tool": "resolve_catalog",
            "arguments": {"catalog_preferences": {"stones": [{"gem": "Diamond", "color": "Pink"}]}},
            "result": catalog_result,
        }])
        decision = agent_module.decide_validation_route(incomplete)

        self.assertEqual(decision.route.value, "clarification_required")
        self.assertFalse(decision.approved)
        self.assertTrue(incomplete["clarification_required"])
        self.assertEqual(
            incomplete["checks"]["catalog"]["result"]["matches"]["stones"][0]["alternatives"][0]["id"],
            "S019",
        )
        self.assertIn("S019 (gem Sapphire, color Pink)", incomplete["clarifications"][0]["message"])
        self.assertIn("requires your confirmation", incomplete["clarifications"][0]["message"])
        self.assertTrue(any(item.customer_confirmation_required for item in decision.feedback))

    def test_clarification_response_keeps_grounded_alternatives_without_duplicate_errors(self):
        rich_message = "No exact match was found. Available alternatives are S019 (Sapphire, Pink); each requires confirmation."
        response = agent_module._format_deterministic_response(
            "clarification_required",
            [
                {"explanation": "No exact match was found."},
                {"explanation": rich_message},
                {"explanation": "No exact match was found."},
            ],
        )
        self.assertEqual(response.count("No exact match was found."), 1)
        self.assertIn("S019", response)
        self.assertIn("requires confirmation", response)


if __name__ == "__main__":
    unittest.main()
