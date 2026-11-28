"""Typed customer choices and the rules for passing a design brief to the supervisor."""

import json
import re
from calendar import month_name
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from .birthstones import available_values, load_birthstones

OPTIONS_FILE = (Path(__file__).resolve().parents[3] / "FrontEnd" / "shecraft-frontend"
                / "src" / "config" / "jewelryOptions.json")
OPTIONS = json.loads(OPTIONS_FILE.read_text(encoding="utf-8"))
STYLE_FIELDS = {"name": {"personalization", "lettering_style"}, "charm": {"charm", "charm_color", "charm_text", "earring_shape"},
                "birthstone": {"birth_month", "stone", "cut", "stone_color", "backing"}}
RING_STONES = ("stone", "side_stone_1", "side_stone_2")
ALL_STYLE_FIELDS = set().union(*STYLE_FIELDS.values())
Facet = Literal["jewelry_type", "style", "metal", "stone", "birth_month", "cut", "chain", "measurement",
                "budget_max", "budget_min", "personalization", "charm", "occasion", "other",
                "stone_count", "side_stone_1", "side_stone_2", "lettering_style", "charm_color",
                "charm_text", "earring_shape", "stone_color", "backing"]


class Preference(BaseModel):
    field: Facet
    value: str = Field(description="Customer's choice, with its original meaning and units.")
    intent: Literal["required", "preferred", "delegated", "excluded"]
    delegation_quote: str | None = Field(default=None, description="For a newly delegated choice, quote the customer's exact words granting that choice.")
    amount: float | None = Field(default=None, allow_inf_nan=False, description="Numeric budget or measurement, if supplied.")
    unit: str | None = Field(default=None, description="Currency code or measurement unit, e.g. USD, mm or in.")

    @model_validator(mode="after")
    def normalize_choice(self):
        if self.intent == "excluded":
            # Store the excluded object itself, so "no pearls" reads as "pearls · excluded".
            if self.field == "stone" and self.value.strip().casefold() == "none":
                self.value = "stones"
            else:
                self.value = re.sub(r"^(?:no|without)\s+", "", self.value.strip(), flags=re.IGNORECASE)
        if self.field != "measurement" or self.intent in ("excluded", "delegated"):
            return self
        aliases = {"millimeters": "mm", "millimetres": "mm", "millimeter": "mm", "millimetre": "mm",
                   "centimeters": "cm", "centimetres": "cm", "centimeter": "cm", "centimetre": "cm",
                   "inches": "in", "inch": "in"}
        if self.unit:
            unit = self.unit.strip().lower()
            self.unit = aliases.get(unit, unit)
        # Fill missing structured values only from a single explicit number + unit.
        # A bare ring size (e.g. "US 7") must never become an inferred diameter.
        matches = re.findall(r"(-?\d+(?:\.\d+)?)\s*(mm|cm|inches|inch|in|millimet(?:er|re)s?|centimet(?:er|re)s?)\b",
                             self.value, re.IGNORECASE)
        if len(matches) == 1:
            amount, unit = matches[0]
            if self.amount is None:
                self.amount = float(amount)
            if not self.unit:
                self.unit = aliases.get(unit.lower(), unit.lower())
        return self


class Brief(BaseModel):
    preferences: list[Preference] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    pending_options: list[Facet] = Field(default_factory=list)
    next_question: str | None = None
    review_reasons: dict[str, str] = Field(default_factory=dict)
    option_field: Facet | None = None
    options: list[str] = Field(default_factory=list)
    status: Literal["collecting", "ready", "needs_review"] = "collecting"

    @model_validator(mode="after")
    def normalize_birth_month(self):
        # Older briefs stored a standalone month as "other" or "stone".
        if not any(p.value.casefold() == "birthstone" for p in self.choices("style")):
            return self
        explicit = self.choices("birth_month")
        preferences = []
        for p in self.preferences:
            month = p.value.strip().capitalize()
            if p.field in ("other", "stone") and month in month_name[1:] and p.intent != "excluded":
                if explicit:
                    continue
                p = p.model_copy(update={"field": "birth_month", "value": month})
                explicit.append(p)
            preferences.append(p)
        self.preferences = preferences
        return self

    @model_validator(mode="after")
    def keep_one_style(self):
        if self.jewelry_type not in ("necklace", "bracelet", "earrings"):
            return self
        supported = OPTIONS[self.jewelry_type]["styles"]
        styles = self.choices("style")
        selected = {p.value.strip().casefold() for p in styles}
        active = next(iter(selected)) if len(selected) == 1 else None
        # An ambiguous or delegated style stays open for the customer to pick.
        if active not in supported or any(p.intent == "delegated" for p in styles):
            self.preferences = [p for p in self.preferences if p.field != "style" or p.intent == "excluded"]
        else:
            allowed = STYLE_FIELDS[active]
            self.preferences = [p for p in self.preferences if
                                (p.field not in ALL_STYLE_FIELDS or p.field in allowed)
                                and (p.field != "style" or p.intent == "excluded" or p is styles[0])]
        # Checkpoints from earlier versions can contain obsolete builder warnings.
        self.review_reasons = {k: v for k, v in self.review_reasons.items() if k not in ALL_STYLE_FIELDS}
        if self.status == "needs_review" and not self.review_reasons:
            self.status = "collecting" if missing_fields(self) or self.conflicts else "ready"
        return self

    def choices(self, field: str) -> list[Preference]:
        return [p for p in self.preferences if p.field == field and p.intent != "excluded"]

    @model_validator(mode="after")
    def reopen_incomplete_brief(self):
        if self.status == "ready":
            missing = missing_fields(self)
            if missing:
                self.status, self.missing = "collecting", missing
                self.next_question = missing_question(self, missing[0])
                set_options(self, missing[0])
        return self

    def value(self, field: str) -> str:
        return next((p.value.strip().casefold() for p in self.choices(field) if p.intent != "delegated"), "")

    @property
    def jewelry_type(self) -> str:
        choices = self.choices("jewelry_type")
        return choices[0].value.strip().lower() if choices else ""


class RequirementsUpdate(BaseModel):
    """Only changes introduced by this customer turn. Preserve unrelated checkpointed fields."""
    informational: bool = Field(description="True for an options question that makes no changes to the brief.")
    new_design: bool = Field(description="True only if the customer started a separate design.")
    replacements: list[Preference] = Field(description="All current choices for each changed field. Empty if unchanged.")
    clear_fields: list[Facet] = Field(description="Fields the customer explicitly removed.")
    conflicts: list[str] = Field(description="All still-unresolved conflicts, including earlier ones.")
    reply: str = Field(description="For informational turns, answer from the observed facts. For design changes, leave empty; the application renders the validated result.")
    options_requested: Facet | None = Field(default=None, description="For a pure options question, which field's choices did the customer ask to see?")


def missing_fields(brief: Brief) -> list[str]:
    required = ["jewelry_type", "metal"]
    if brief.jewelry_type in ("ring", "necklace", "bracelet"):
        required.append("measurement")
    if brief.jewelry_type in OPTIONS:
        required.append("style")
    style = brief.value("style")
    if style and brief.jewelry_type in ("necklace", "bracelet"):
        required.append("chain")
    if style == "name":
        required.extend(["personalization", "lettering_style"])
    elif style == "charm":
        if brief.jewelry_type == "earrings":
            required.append("earring_shape")
        required.extend(["charm_color", "charm"])
        if any("letter" in p.value.casefold() for p in brief.choices("charm")):
            required.append("charm_text")
    elif style == "birthstone":
        if brief.jewelry_type == "earrings":
            required.append("backing")
        required.extend(["birth_month", "stone_color", "cut"])
    elif brief.jewelry_type == "ring" and style:
        tennis = style == "tennis horizon"
        if not tennis:
            required.append("stone_count")
        count = "1" if tennis else brief.value("stone_count")
        if not count and any(p.intent == "delegated" for p in brief.choices("stone_count")):
            counts = catalog_options("ring", "stone_count", style)
            count = counts[-1]  # Collect preferences for any positions the designer may use.
        if count in ("1", "2", "3"):
            required.extend(RING_STONES[:int(count)])
            if not tennis:
                required.append("cut")
    return [key for key in required if not brief.choices(key)
            or key == "jewelry_type" and brief.jewelry_type not in OPTIONS
            or key == "style" and not style
            or key in RING_STONES and brief.jewelry_type == "ring" and brief.value(key) == "lab-grown"]


def configuration_issues(brief: Brief) -> list[tuple[str | None, str]]:
    issues = []
    kind = brief.jewelry_type
    style = next((p.value.casefold() for p in brief.choices("style")), "")
    for p in brief.preferences:
        if not supported_choice(p, kind, style):
            issues.append((p.field, option_question(kind, p.field, style)))
    for field in ("jewelry_type", "style", "measurement", "budget_min", "budget_max", "stone_count",
                  "side_stone_1", "side_stone_2", "cut", "stone_color", "birth_month", "lettering_style",
                  "charm_color", "earring_shape", "backing"):
        choices = {p.value.casefold() for p in brief.choices(field) if p.intent == "required"}
        if len(choices) > 1:
            issues.append((field, f"Which of the requested {field.replace('_', ' ')} options should I use?"))
    minimum = maximum = None
    for p in brief.preferences:
        if p.intent == "delegated":
            continue
        if p.field in ("budget_min", "budget_max", "measurement"):
            if p.amount is None or p.amount <= 0 or not p.unit:
                issues.append((p.field, f"Could you confirm the amount and unit for {p.field.replace('_', ' ')}?"))
                continue
            if p.field == "budget_min":
                minimum = p
            elif p.field == "budget_max":
                maximum = p
    if minimum and maximum and (minimum.unit != maximum.unit or minimum.amount > maximum.amount):
        issues.append(("budget_max", "Could you clarify the budget range and its currency?"))
    for p in brief.preferences:
        if p.intent == "excluded" and any(v.value.casefold() == p.value.casefold() for v in brief.choices(p.field)):
            issues.append((None, f"Should {p.value} be included or excluded?"))
    return list(dict.fromkeys(issues))


def inconsistencies(brief: Brief) -> list[str]:
    return list(dict.fromkeys([*brief.conflicts, *(question for _, question in configuration_issues(brief))]))


def catalog_options(kind: str, field: str | None, style: str) -> list[str]:
    catalog = OPTIONS.get(kind, {})
    values = []
    if field == "jewelry_type":
        values = list(OPTIONS)
    elif field == "measurement":
        values = ([f"{n} mm" for n in catalog.get("diameters_mm", [])]
                  or [f"{n} inches" for n in catalog.get("lengths_inches", [])])
    elif field == "birth_month":
        values = list(month_name[1:])
    elif field in RING_STONES and kind == "ring":
        values = [*catalog["natural_stones"], *(f"Lab-grown {c}" for c in catalog["lab_colors"])]
    elif field == "stone_count" and kind == "ring":
        counts = catalog["stone_counts"]
        values = [str(n) for n in next((ns for label, ns in counts.items() if label.casefold() == style), range(4))]
    elif field == "cut":
        if kind == "ring" or style == "birthstone":
            values = catalog.get("cuts", catalog.get("cuts_for_birthstones", []))
    else:
        keys = {"metal": "metals", "style": "styles", "chain": "chains", "lettering_style": "lettering_styles",
                "charm_color": "charm_colors", "earring_shape": "charm_shapes", "backing": "birthstone_backings"}
        values = catalog.get(keys.get(field), [])
    return values


def supported_choice(p: Preference, kind: str, style: str) -> bool:
    if p.intent in ("delegated", "excluded"):
        return True
    if p.field == "charm_text" or (p.field == "personalization" and style == "name"):
        return 0 < len(p.value.strip()) <= (10 if p.field == "charm_text" else 12)
    options = catalog_options(kind, p.field, style)
    if not options:
        return True  # Free text, or no configurator for this field yet.
    if p.field == "measurement":
        mm = {"mm": 1, "cm": 10, "in": 25.4}
        if p.amount is None or p.unit not in mm:
            return False
        sizes, unit = ("diameters_mm", "mm") if kind == "ring" else ("lengths_inches", "in")
        amount = p.amount * mm[p.unit] / mm[unit]
        return any(abs(amount - n) < 0.001 for n in OPTIONS[kind][sizes])
    if p.field in RING_STONES and p.value.casefold() == "lab-grown":
        return True
    return any(p.value.strip().casefold() == v.casefold() for v in options)


def option_question(kind: str, field: str, style: str) -> str:
    if field == "measurement":
        return ("Which inner diameter in millimeters would you like?" if kind == "ring"
                else "Which length in inches would you like?")
    options = ", ".join(catalog_options(kind, field, style))
    return f"Which {field.replace('_', ' ')} would you like ({options})?"


def set_options(brief: Brief, field: str | None, birthstones=None):
    style = next((p.value.casefold() for p in brief.choices("style")), "")
    brief.options = catalog_options(brief.jewelry_type, field, style)
    if style == "birthstone" and birthstones is not None and field in ("birth_month", "stone_color", "cut"):
        brief.options = available_values(birthstones, field, brief.value("birth_month"), brief.value("stone_color"))
    brief.option_field = field


def selected_style(text: str, kind: str) -> str | None:
    """Recognize a complete, unambiguous menu answer without matching questions or negations."""
    match = re.fullmatch(
        r"(?:(?:switch to|i (?:prefer|want|choose)|i['’]d (?:like|prefer)|let['’]s (?:do|go with))\s+)?"
        rf"(?:the |a )?(.+?)(?:\s+(?:style|design|option|{re.escape(kind)}))?[.!]?",
        text.strip(), flags=re.IGNORECASE,
    )
    if match:
        return next((v for v in OPTIONS.get(kind, {}).get("styles", [])
                     if v.casefold() == match[1].casefold()), None)
    return None


def missing_question(brief: Brief, field: str) -> str:
    if field == "measurement":
        if brief.jewelry_type == "ring":
            return "What inner diameter in millimeters would you like for the ring?"
        lengths = OPTIONS[brief.jewelry_type]["lengths_inches"]
        return f"Which length would you like ({', '.join(map(str, lengths))} inches)?"
    if field == "style":
        styles = ", ".join(OPTIONS[brief.jewelry_type]["styles"])
        return f"Which {brief.jewelry_type} style would you like ({styles})?"
    if field == "personalization":
        return f"What name would you like on your {brief.jewelry_type}?"
    if field == "stone_count":
        return f"How many gems would you like on your {brief.choices('style')[0].value}?"
    if field in RING_STONES:
        position = {"stone": "center stone", "side_stone_1": "first side stone", "side_stone_2": "second side stone"}[field]
        if brief.value("style") == "tennis horizon":
            position = "matching tennis stones"
        return f"Which natural gem or lab-grown color would you like for the {position}?"
    if field == "cut" and brief.jewelry_type == "ring":
        return "Which cut would you like for the center gem?"
    if field == "charm_text":
        return "What text would you like on the letter charm (up to 10 characters)?"
    if field == "birth_month":
        return "Which birth month would you like?"
    if field == "stone_color":
        return "Which birthstone color would you like?"
    if field == "lettering_style":
        return "Which lettering style would you like for the name?"
    if field == "charm":
        return "Which charm would you like? You can pick a catalog photo or describe the motif."
    return f"What {field.replace('_', ' ')} would you like for this piece?"


def birthstone_mismatches(rows, changes, existing, cleared):
    """Resolve month before color, and color before cut, retaining valid previous choices."""
    rejected, obsolete, parents = [], [], []
    for field in ("birth_month", "stone_color", "cut"):
        allowed = {v.casefold() for v in available_values(rows, field, *parents)}
        for candidates, invalid in ((changes, rejected), (existing, obsolete)):
            invalid.extend(p for p in candidates if p.field == field and p.intent in ("required", "preferred")
                           and p.value.strip().casefold() not in allowed)
        accepted = [p for p in changes if p.field == field and p not in rejected]
        if not accepted and field not in cleared:
            accepted = [p for p in existing if p.field == field and p not in obsolete]
        parents.append(next((p.value.strip() for p in accepted if p.intent in ("required", "preferred")), ""))
    return rejected, obsolete


def prune_inactive_fields(brief: Brief):
    kind, style = brief.jewelry_type, brief.value("style")
    inactive = set()
    if kind == "ring":
        inactive.update({"lettering_style", "charm_color", "charm_text", "earring_shape", "backing", "stone_color"})
        if style == "tennis horizon":
            inactive.update(("stone_count", "side_stone_1", "side_stone_2", "cut"))
        elif brief.value("stone_count") in ("0", "1", "2", "3"):
            count = int(brief.value("stone_count"))
            inactive.update(RING_STONES[count:])
            if count == 0:
                inactive.add("cut")
    elif style in STYLE_FIELDS:
        inactive.update(ALL_STYLE_FIELDS - STYLE_FIELDS[style])
        inactive.update(("stone_count", "side_stone_1", "side_stone_2"))
        inactive.update(("measurement", "chain") if kind == "earrings" else ("backing", "earring_shape"))
        if style == "charm" and not any("letter" in p.value.casefold() for p in brief.choices("charm")):
            inactive.add("charm_text")
    brief.preferences = [p for p in brief.preferences if p.field not in inactive or p.intent == "excluded"]
    brief.pending_options = [field for field in brief.pending_options if field not in inactive
                             or (field == "stone_count" and style == "tennis horizon")]


def apply_update(previous: Brief, update: RequirementsUpdate, message: str = "") -> tuple[Brief, str]:
    # A short selection replaces the entire previous style slot, including legacy
    # compound values. No other change is authorized by a style-only answer.
    selection = selected_style(message, previous.jewelry_type)
    if selection:
        # Recompute catalog and field checks from the new selection. Retain an
        # unrelated customer conflict (e.g. a disputed budget) until resolved.
        prior_checks = inconsistencies(previous.model_copy(update={"conflicts": []}))
        menu = OPTIONS[previous.jewelry_type]["styles"]
        remaining = [c for c in previous.conflicts if c not in prior_checks
                     and "style" not in c.casefold() and not all(v.casefold() in c.casefold() for v in menu)]
        update = update.model_copy(update={
            "informational": False, "new_design": False, "clear_fields": [],
            "replacements": [Preference(field="style", value=selection, intent="preferred")],
            "conflicts": remaining,
        })
    if update.informational and not (previous.value("style") == "birthstone" and
                                     previous.next_question and "retry when the catalog" in previous.next_question
                                     and not update.options_requested):
        brief = previous.model_copy(deep=True)
        set_options(brief, update.options_requested, load_birthstones() if brief.value("style") == "birthstone" else None)
        return brief, update.reply
    wording = message.casefold()
    changes = []
    birthstone_piece = any(p.value.casefold() == "birthstone" for p in
                           [*previous.choices("style"), *(p for p in update.replacements if p.field == "style")])
    stated_months = [p for p in update.replacements if p.field == "birth_month"]
    for choice in update.replacements:
        if birthstone_piece and choice.field in ("stone", "other") and choice.value.strip().capitalize() in month_name[1:]:
            if stated_months:
                continue
            choice = choice.model_copy(update={"field": "birth_month", "value": choice.value.strip().capitalize()})
            stated_months.append(choice)
        if choice.field == "stone" and stated_months:
            if message and choice.value.casefold() not in message.casefold():
                continue  # An inferred gem is not a separate customer choice.
        if choice.intent == "delegated" and message:
            quote = (choice.delegation_quote or message).strip().casefold()
            if not quote or quote not in message.casefold():
                continue
            if choice.field == "measurement":
                explicit_fit = re.search(r"\b(length|size|diameter|measurement|fit|everything)\b", quote)
                answers_fit = (previous.next_question and
                               re.search(r"\b(length|size|diameter|measurement)\b", previous.next_question, re.I) and
                               not re.search(r"\b(chain|metal|stone|style|charm)\b", quote))
                if not (explicit_fit or answers_fit):
                    continue
                if not re.search(r"\b(choose|decide|pick|delegate|any|whatever|surprise)\b|up to you|your choice", quote):
                    continue
        choice = choice.model_copy()
        if choice.field == "stone_count":
            count = re.fullmatch(r"(\d+)(?:\s+(?:gems?|stones?|diamonds?))?", choice.value.strip(), re.I)
            if count:
                choice.value = str(int(count[1]))
        if choice.field == "charm_color" and choice.value.casefold() == "rosegold":
            choice.value = "Rose Gold"
        if choice.field == "budget_max":
            old_budget = previous.choices("budget_max")
            if not choice.unit and len(old_budget) == 1 and not update.new_design:
                choice.unit = old_budget[0].unit
            if choice.intent == "preferred" and re.search(r"\b(?:my max(?:imum)?|hard limit|no more than)\b", wording):
                choice.intent = "required"
        changes.append(choice)
    for p in changes:
        if p.field == "style":
            kind = next((item.value.strip().lower() for item in changes if item.field == "jewelry_type"), previous.jewelry_type)
            canonical = selected_style(p.value, kind)
            if canonical:
                p.value = canonical
    base = Brief() if update.new_design else previous
    # Validate in the proposed piece's context before replacing any accepted choices.
    kind = next((p.value.strip().lower() for p in changes if p.field == "jewelry_type"
                 and p.intent in ("required", "preferred") and p.value.strip().lower() in OPTIONS), base.jewelry_type)
    style = next((p.value.casefold() for p in [*changes, *base.preferences] if p.field == "style"
                  and p.intent in ("required", "preferred") and supported_choice(p, kind, "")), "")
    if kind == "ring" and not any(p.field == "stone_count" for p in changes):
        if any(p.field == "stone" and p.value.casefold() in ("none", "no stones", "stones") for p in changes):
            changes = [p for p in changes if p.field != "stone" or p.intent == "excluded"]
            changes.append(Preference(field="stone_count", value="0", intent="required"))
    birthstones = load_birthstones() if style == "birthstone" else None
    rejected = [p for p in changes if not supported_choice(p, kind, style)]
    obsolete = []
    if birthstones is not None:
        invalid, obsolete = birthstone_mismatches(birthstones, changes, base.preferences, update.clear_fields)
        rejected.extend(p for p in invalid if p not in rejected)
    rejected_fields = {p.field for p in rejected}
    changes = [p for p in changes if p not in rejected]
    replaced = {p.field for p in changes} | (set(update.clear_fields) - rejected_fields)
    kept = [p for p in base.preferences if p.field not in replaced and p not in obsolete
            and not (style == "tennis horizon" and p.field == "stone_count")]
    # Also recheck older choices when a previously unknown piece type becomes known.
    rejected.extend(p for p in kept if not supported_choice(p, kind, style))
    kept = [p for p in kept if p not in rejected]
    brief = Brief(preferences=kept + changes, conflicts=update.conflicts)
    pending = [f for f in base.pending_options if f not in replaced and catalog_options(kind, f, style)]
    brief.pending_options = list(dict.fromkeys([*pending, *(p.field for p in rejected)]))
    prune_inactive_fields(brief)
    styles = brief.choices("style")
    if any(p.intent in ("required", "preferred") for p in brief.choices("other")):
        brief.review_reasons["other"] = "A designer needs to check this custom request."
    prior_checks = {q for _, q in configuration_issues(previous)}
    conflicts = [c for c in brief.conflicts if c not in prior_checks and not any(
        p.value.casefold() in c.casefold() or p.field.replace("_", " ") in c.casefold() for p in rejected)]
    checks = [(None, c) for c in conflicts] + configuration_issues(brief)
    missing = missing_fields(brief)
    field, question = next(iter(checks), (None, None))
    if brief.pending_options:
        field = brief.pending_options[0]
        if field in ("stone_color", "charm_text", "personalization"):
            question = missing_question(brief, field)
        else:
            question = option_question(kind, field, style)
    elif "style" in missing and any(p.field == "style" and p.intent != "excluded" for p in changes):
        field, question = "style", missing_question(brief, "style")
    elif not question and missing:
        field = missing[0]
        question = missing_question(brief, field)
    set_options(brief, field, birthstones)
    if brief.pending_options and field == "cut" and style == "birthstone" and brief.options:
        question = f"Which cut would you like for this birthstone ({', '.join(brief.options)})?"
    if style == "birthstone" and not birthstones and (not question or field in ("birth_month", "stone_color", "cut")):
        question = "I couldn’t verify the birthstone options in the store catalog. Please retry when the catalog is available."
        brief.options = []
    brief.missing = missing
    brief.conflicts = list(dict.fromkeys(q for _, q in checks))
    brief.next_question = question
    notes = []
    for facet, reason in brief.review_reasons.items():
        values = ", ".join(p.value for p in brief.choices(facet) if p.intent != "delegated")
        notes.append(f"{facet.replace('_', ' ').capitalize()} ({values}) — {reason}")
    review = "Saved for review:\n" + "\n".join(f"- {note}" for note in notes) if notes else ""
    details = [f"{p.field.replace('_', ' ')}: {p.value}" if p.intent != "delegated"
               else f"{p.field}: delegated to the designer" for p in changes if p in brief.preferences and p.field not in brief.review_reasons]
    acknowledgment = "I’ve recorded " + ", ".join(details) + "." if details else ""
    if question:
        # The validator owns completion and asks for one blocker at a time.
        if "?" in question:
            question = question.split("?", 1)[0] + "?"
        elif not question.startswith("I couldn’t verify"):
            question = question.rstrip(". ") + ". How would you like to resolve this?"
        brief.next_question = question
        notices = [f"“{p.value}” isn’t a listed {p.field.replace('_', ' ')} option." for p in rejected]
        return brief, "\n\n".join(filter(None, (acknowledgment, review, *notices, question)))
    if review:
        brief.status = "needs_review"
        return brief, "\n\n".join(filter(None, (acknowledgment, review)))
    brief.status = "ready"
    style = next((p.value for p in styles if p.intent != "delegated"), "")
    piece = " ".join(filter(None, (style, brief.jewelry_type)))
    return brief, "\n\n".join(filter(None, (acknowledgment, f"Your {piece} design brief is ready. You can still make changes.")))


def menu_update(brief: Brief, message: str) -> RequirementsUpdate | None:
    field, options = brief.option_field, brief.options
    if not brief.preferences and not options:
        field, options = "jewelry_type", list(OPTIONS)
    answer = message.strip().casefold()
    if answer.startswith("switch to "):
        field, options = "style", OPTIONS.get(brief.jewelry_type, {}).get("styles", [])
        answer = answer.removeprefix("switch to ").rstrip(".!")
    value = next((v for v in options if v.casefold() == answer), None)
    if not field or value is None:
        return None
    selection = Preference(field=field, value=value, intent="required")
    exclusions = [p for p in brief.preferences if p.field == field and p.intent == "excluded"]
    return RequirementsUpdate(informational=False, new_design=False, replacements=[selection, *exclusions],
                              clear_fields=[], conflicts=brief.conflicts, reply="")
