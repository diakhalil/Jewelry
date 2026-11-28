"""Decision and conditional routing for unified validation results.

This module consumes validator/orchestrator output only. It does not rerun
validation or generate a revised design, so it is suitable for later use as an
isolated LangGraph node without assuming any agent interface.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class Route(str, Enum):
    APPROVED = "approved"
    CLARIFICATION_REQUIRED = "clarification_required"
    REVISION_REQUIRED = "revision_required"
    TECHNICAL_ERROR = "technical_error"
    RETRY_LIMIT_REACHED = "retry_limit_reached"


class DecisionFeedback(BaseModel):
    recipient: str
    failed_check: str
    affected_fields: list[str] = Field(default_factory=list)
    explanation: str
    suggested_actions: list[str] = Field(default_factory=list)
    customer_confirmation_required: bool = False


class DecisionState(BaseModel):
    revision_attempts: int = Field(default=0, ge=0)
    max_revision_attempts: int = Field(default=3, ge=1)
    last_invalid_configuration_fingerprint: str | None = None


class ValidationDecision(BaseModel):
    route: Route
    approved: bool
    complete_validation: bool
    summary: str
    feedback: list[DecisionFeedback] = Field(default_factory=list)
    state: DecisionState
    repeated_unchanged_configuration: bool = False
    revision_constraints: list[str] = Field(default_factory=list)


_RECIPIENTS = {
    "compatibility": "compatibility_validator",
    "adapter": "integration_adapter",
    "catalog": "catalog_validator",
    "inventory": "inventory_validator",
    "price": "price_validator",
    "budget": "price_validator",
    "orchestrator": "validation_orchestrator",
}


def _fingerprint(configuration: Any) -> str | None:
    if configuration is None:
        return None
    try:
        encoded = json.dumps(configuration, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _decision_state(value: DecisionState | Mapping[str, Any] | None) -> DecisionState:
    if isinstance(value, DecisionState):
        return value
    payload = value or {}
    validator = getattr(DecisionState, "model_validate", None)
    return validator(payload) if validator else DecisionState.parse_obj(payload)


def _fields(item: Mapping[str, Any]) -> list[str]:
    fields = item.get("fields", item.get("affected_fields", []))
    if isinstance(fields, str):
        return [fields]
    if isinstance(fields, list):
        return [str(field) for field in fields if isinstance(field, (str, int, float))]
    field = item.get("field")
    return [str(field)] if field is not None else []


def _message(item: Any) -> str:
    if isinstance(item, Mapping):
        return str(item.get("message") or item.get("explanation") or item.get("code") or "Validation requires attention.")
    return str(item)


def _feedback_from_result(result: Mapping[str, Any]) -> list[DecisionFeedback]:
    feedback: list[DecisionFeedback] = []
    seen: set[tuple[str, str, tuple[str, ...]]] = set()

    def add(
        check: str,
        item: Any,
        *,
        confirmation: bool = False,
        fallback_fields: list[str] | None = None,
        fallback_actions: list[str] | None = None,
    ) -> None:
        detail = item if isinstance(item, Mapping) else {"message": _message(item)}
        explanation = _message(detail)
        default_fields = {
            "compatibility": ["three_d_configuration"],
            "adapter": ["three_d_configuration", "confirmed_components", "approved_mappings"],
            "catalog": ["catalog_preferences"],
            "inventory": ["material_id", "stones", "charms"],
            "price": ["design", "budget"],
        }
        fields = _fields(detail) or fallback_fields or default_fields.get(check, [])
        key = (check, explanation, tuple(fields))
        if key in seen:
            return
        seen.add(key)
        actions = detail.get("revision_suggestions") or detail.get("suggested_actions") or detail.get("actions") or []
        if isinstance(actions, str):
            actions = [actions]
        actions = [str(action) for action in actions] if isinstance(actions, list) else []
        actions.extend(fallback_actions or [])
        if confirmation:
            actions.insert(0, "Ask the customer to confirm the requested choice; do not select or substitute a component automatically.")
        if check in {"inventory", "catalog", "adapter"}:
            actions.append("Preserve all other explicitly requested properties; do not replace an unresolved component without confirmation.")
        if check in {"price", "budget"}:
            actions.append("Keep the stated budget unchanged; revise the design within that limit or ask the customer before changing it.")
        feedback.append(DecisionFeedback(
            recipient=_RECIPIENTS.get(check, "validation_orchestrator"),
            failed_check=check,
            affected_fields=fields,
            explanation=explanation,
            suggested_actions=list(dict.fromkeys(actions)),
            customer_confirmation_required=confirmation,
        ))

    checks = result.get("checks")
    if isinstance(checks, Mapping):
        for check, value in checks.items():
            if not isinstance(value, Mapping):
                continue
            status = value.get("status")
            nested = value.get("result")
            if status not in {"failed", "clarification_required", "conflict", "error"} and not (
                check == "adapter" and status == "skipped" and result.get("clarification_required")
            ):
                continue
            if isinstance(nested, Mapping):
                entries = nested.get("errors", [])
                if not isinstance(entries, list):
                    entries = []
                if check == "adapter":
                    entries = [*entries, *(nested.get("clarifications", []) or []), *(nested.get("conflicts", []) or [])]
                if not entries:
                    entries = [nested.get("message") or f"The {check} check did not pass ({status})."]
                revision_suggestions = nested.get("revision_suggestions", [])
                if isinstance(revision_suggestions, str):
                    revision_suggestions = [revision_suggestions]
                if not isinstance(revision_suggestions, list):
                    revision_suggestions = []
                unsupported_requested_property = check == "compatibility" and bool(
                    nested.get("unsupported_fields") or nested.get("unsupported_combinations")
                )
            else:
                entries = [f"The {check} check did not pass ({status})."]
                revision_suggestions = []
                unsupported_requested_property = False
            for entry in entries:
                needs_confirmation = status in {"clarification_required", "conflict"} or unsupported_requested_property
                add(str(check), entry, confirmation=needs_confirmation, fallback_actions=revision_suggestions)

    for collection_name in ("clarifications", "conflicts"):
        collection = result.get(collection_name)
        if isinstance(collection, list):
            for entry in collection:
                check = str(entry.get("check", "adapter")) if isinstance(entry, Mapping) else "adapter"
                add(check, entry, confirmation=True)

    errors = result.get("errors")
    if isinstance(errors, list):
        for entry in errors:
            if isinstance(entry, Mapping):
                check = str(entry.get("check", "orchestrator"))
                # Most check errors have already been represented with detail
                # from the nested check; this supplements legacy result shapes.
                add(check, entry)

    technical = result.get("technical_errors")
    if isinstance(technical, list):
        for entry in technical:
            check = str(entry.get("check", "orchestrator")) if isinstance(entry, Mapping) else "orchestrator"
            add(check, entry, fallback_fields=["service"])
    return feedback


def decide_validation_route(
    unified_result: Mapping[str, Any],
    *,
    configuration: Any = None,
    state: DecisionState | Mapping[str, Any] | None = None,
) -> ValidationDecision:
    """Choose a safe next route from the unified validation result.

    ``state`` is caller-owned and round-trippable between graph/API calls. The
    attempt counter represents revisions already attempted, not suggestions
    emitted. A repeated unchanged invalid configuration terminates routing.
    """
    current_state = _decision_state(state)
    complete = unified_result.get("complete_validation") is True
    # Old orchestrator responses may omit this field. Individual check success
    # is deliberately insufficient evidence for approval.
    fingerprint = _fingerprint(configuration)
    repeated = bool(
        fingerprint
        and current_state.last_invalid_configuration_fingerprint
        and fingerprint == current_state.last_invalid_configuration_fingerprint
    )
    feedback = _feedback_from_result(unified_result)
    copier = getattr(current_state, "model_copy", None)
    next_state = copier(deep=True) if copier else current_state.copy(deep=True)

    checks = unified_result.get("checks")
    has_check_error = isinstance(checks, Mapping) and any(
        isinstance(item, Mapping) and item.get("status") == "error"
        for item in checks.values()
    )
    compatibility_check = checks.get("compatibility") if isinstance(checks, Mapping) else None
    compatibility_result = compatibility_check.get("result") if isinstance(compatibility_check, Mapping) else None
    unsupported_requested_property = isinstance(compatibility_result, Mapping) and bool(
        compatibility_result.get("unsupported_fields") or compatibility_result.get("unsupported_combinations")
    )
    if unified_result.get("technical_errors") or has_check_error or unified_result.get("status") == "error":
        route = Route.TECHNICAL_ERROR
        summary = "A validation service failed; resolve the technical issue and retry without changing the design."
    elif (
        complete
        and unified_result.get("valid") is True
        and (not isinstance(checks, Mapping) or all(
            isinstance(item, Mapping) and item.get("status") == "passed"
            for item in checks.values()
        ))
    ):
        route = Route.APPROVED
        summary = "All required validation checks are complete and passed."
    elif not complete and not feedback and not unified_result.get("clarifications") and not unified_result.get("unresolved_mappings"):
        route = Route.CLARIFICATION_REQUIRED
        feedback.append(DecisionFeedback(
            recipient="validation_orchestrator",
            failed_check="complete_validation",
            affected_fields=[],
            explanation="The result does not confirm complete validation; successful individual checks cannot establish approval.",
            suggested_actions=["Provide the complete unified validation result, including completion and unresolved-mapping status."],
            customer_confirmation_required=False,
        ))
        summary = "Complete-validation evidence is missing; approval is withheld pending clarification."
    elif repeated or current_state.revision_attempts >= current_state.max_revision_attempts:
        route = Route.RETRY_LIMIT_REACHED
        summary = "The design remains invalid after the allowed revision attempts; stop automatic retries and request human review."
    elif (
        unified_result.get("clarifications")
        or unified_result.get("conflicts")
        or unified_result.get("unresolved_mappings")
        or unified_result.get("clarification_required")
        or unsupported_requested_property
    ):
        route = Route.CLARIFICATION_REQUIRED
        summary = "A customer choice or mapping must be clarified before validation can continue."
        if not feedback:
            feedback.append(DecisionFeedback(
                recipient="customer",
                failed_check="unresolved_mapping",
                affected_fields=[],
                explanation="One or more design mappings remain unresolved.",
                suggested_actions=["Confirm the requested component or provide the missing mapping."],
                customer_confirmation_required=True,
            ))
    else:
        route = Route.REVISION_REQUIRED
        summary = "The design failed one or more checks and needs a constrained revision."
        if not feedback:
            feedback.append(DecisionFeedback(
            recipient="requesting_workflow",
                failed_check="validation",
                affected_fields=[],
                explanation=str(unified_result.get("status") or "The unified validation result is invalid."),
                suggested_actions=["Revise only the fields required to resolve the failed checks."],
                customer_confirmation_required=False,
            ))

    if route == Route.REVISION_REQUIRED:
        next_state.revision_attempts += 1
        next_state.last_invalid_configuration_fingerprint = fingerprint
    elif route == Route.RETRY_LIMIT_REACHED:
        next_state.last_invalid_configuration_fingerprint = fingerprint or current_state.last_invalid_configuration_fingerprint

    if route == Route.TECHNICAL_ERROR and not feedback:
        feedback.append(DecisionFeedback(
            recipient="validation_orchestrator",
            failed_check="technical_error",
            affected_fields=["validation_service"],
            explanation="A validation check reported a technical error; design validity cannot be determined.",
            suggested_actions=["Restore the validation service and retry the same configuration."],
            customer_confirmation_required=False,
        ))

    constraints = []
    if route == Route.REVISION_REQUIRED:
        constraints = [
            "Preserve customer-confirmed preferences and explicitly requested properties unless the customer approves a change.",
            "Preserve the stated budget; do not increase it or weaken its constraints.",
            "Do not automatically select ambiguous components or substitute gemstones or colors.",
        ]

    return ValidationDecision(
        route=route,
        approved=(route == Route.APPROVED),
        complete_validation=complete,
        summary=summary,
        feedback=feedback,
        state=next_state,
        repeated_unchanged_configuration=repeated,
        revision_constraints=constraints,
    )
