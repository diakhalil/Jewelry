"""LangGraph validation subgraph with deterministic checks and routing."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import re
from collections.abc import Callable, Mapping
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.graph.message import add_messages
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from .adapter import adapt_three_d_configuration
from .catalog import resolve_catalog_preferences
from .compatibility import validate_3d_configuration
from .decision import DecisionState, Route, decide_validation_route
from .inventory import validate_inventory
from .orchestrator import orchestrate_validation
from .pricing import validate_price


logger = logging.getLogger("shecraft.validation.agent")

MAX_REACT_TOOL_ROUNDS = 8

VALIDATION_AGENT_SYSTEM_PROMPT = """You are the SheCraft Jewelry Validation Agent. Your job is to gather reliable validation evidence for the customer's current jewelry design, explain failures clearly, and return actionable clarification or revision guidance. You do not design or alter the customer's jewelry, reserve stock, or approve designs yourself.

Use the available tools dynamically. Choose each tool from the current request and from the actual observations already returned. A request may need one focused check, several focused checks, or the integrated validation tool. Do not follow a fixed tool sequence. Do not say that a tool ran unless its actual result appears in this conversation.

Available tools:
- resolve_catalog: Resolve customer language such as metal, gemstone/color, or charm/design preferences against catalog records. Use it when preferences still need database IDs or catalog existence is uncertain. Ambiguous matches are not resolved: present the returned candidates and ask for a choice. Never choose a candidate or alternative automatically.
- validate_inventory: Check material, stone, and charm IDs and requested quantities against current backend inventory. Use it when checking specific ID-based availability or a focused inventory question. It does not reserve stock; backend creation must revalidate/reserve atomically later.
- validate_price: Ask the Node backend for the authoritative preview price and compare it with the supplied budget. Use it for a focused price/budget check. Never calculate or guess prices yourself.
- validate_3d_compatibility: Check a supplied 3D configuration against supported compatibility rules. Use it when a 3D configuration is present or a supported-configuration question needs evidence.
- adapt_three_d_configuration: Adapt a supplied 3D configuration into canonical catalog, inventory, and pricing inputs using its supplied provenance, confirmed selections, and approved mappings. Use it when those mappings are needed. Do not invent missing 3D fields or component mappings.
- run_unified_validation: Run the existing complete validation workflow using the established inventory, pricing, catalog, and optional 3D interfaces. Use it when a complete end-to-end validation decision is requested. This tool is available to you; it is not run automatically. Its returned result is the only source that can establish complete validation.

Rules for interpreting results:
- Treat tool results as authoritative observations. Never fabricate IDs, prices, quantities, catalog matches, 3D capabilities, successful checks, or alternatives.
- An unavailable component or insufficient quantity is a validation failure. Report the failed component and requested/available quantity, and suggest only corrective actions supported by the tool output. Do not substitute a different material, gemstone, color, grade, or charm unless the customer confirms it.
- An ambiguous catalog result requires clarification. List candidates from the result and ask which one the customer intends; do not select the cheapest, first, or seemingly closest candidate.
- An over-budget result is not valid. State the computed amount and excess, preserve the given budget, and request a design revision or ask the customer before changing an explicit preference.
- An unsupported 3D configuration is not valid. Report the unsupported fields/combinations returned by the compatibility check. Ask for clarification when a customer choice or mapping is needed; otherwise provide only supported revision instructions.
- A technical/backend/tool error means the check could not be completed. Explain that status, do not ask the customer to change the design because of a technical failure, and do not claim a pass.
- Ask concise clarification questions when required inputs, component selections, or approved mappings are missing. Direct revision feedback to the responsible validation/design workflow and preserve all confirmed preferences.
- You may summarize observations in your final message, but your message never changes validation status. Only deterministic validation evidence can approve a design.
- To claim approval, the current turn must contain a successful run_unified_validation result for the same request inputs, with complete_validation=true, valid=true, and every reported required check passed. If no such result is present, say validation is incomplete and do not claim approval. Do not ask another tool or infer a missing check merely to satisfy this rule; decide what to do next based on the request and available observations.

Examples (illustrative, not required sequences):
1. If the customer asks whether selected stone IDs have enough stock, validate_inventory may be the relevant first action. If the user also requests a final approval, further evidence may be needed; the final decision still depends on the unified result.
2. If the customer says “pink diamond” without an ID, resolve_catalog can establish whether that preference exists. If it returns not_found plus alternatives, report them as suggestions and ask for confirmation; do not continue with an alternative as if selected.
3. If a price is requested for a design with a stated budget, validate_price can provide authoritative pricing evidence. If it exceeds budget, explain the difference and ask for revision while preserving the budget.
4. If a complete review includes a supplied 3D configuration, compatibility or adaptation tools may help inspect specific concerns; the integrated tool can then provide complete evidence when appropriate. The order depends on the actual inputs and observations.
5. If any tool reports a backend failure, communicate that validation is blocked by a technical issue. Do not convert that error into an inventory, pricing, or compatibility conclusion."""


class ConversationMessage(BaseModel):
    role: Literal["user", "assistant", "system"]
    content: str


class ValidationNarrative(BaseModel):
    """LLM wording only; deterministic validation/routing stays authoritative."""

    response: str = Field(min_length=1, max_length=1800)
    clarification_questions: list[str] = Field(default_factory=list, max_length=8)
    revision_feedback: list[str] = Field(default_factory=list, max_length=8)


class ValidationAgentState(BaseModel):
    """Serializable Pydantic state for a single customer validation thread."""

    session_id: str = ""
    customer_preferences: dict[str, Any] = Field(default_factory=dict)
    proposed_design: dict[str, Any] | None = None
    budget: float | None = None
    inventory_request: dict[str, Any] | None = None
    price_request: dict[str, Any] | None = None
    catalog_preferences: dict[str, Any] | None = None
    three_d_configuration: dict[str, Any] | None = None
    three_d_field_provenance: dict[str, str] | None = None
    three_d_confirmed_components: dict[str, Any] | None = None
    three_d_approved_mappings: dict[str, Any] | None = None
    canonical_resolved_configuration: dict[str, Any] | None = None
    validation_results: dict[str, Any] | None = None
    routing_decision: dict[str, Any] | None = None
    revision_state: DecisionState = Field(default_factory=DecisionState)
    conversation_messages: list[ConversationMessage] = Field(default_factory=list)
    clarification_requests: list[dict[str, Any]] = Field(default_factory=list)
    input_conflicts: list[dict[str, Any]] = Field(default_factory=list)
    incoming_message: str | None = None
    final_response: str = ""
    deterministic_response: str = ""
    handoff: dict[str, Any] | None = None
    llm_status: Literal["pending", "used", "unavailable"] = "pending"
    llm_error: str | None = None
    llm_clarification_questions: list[str] = Field(default_factory=list)
    llm_revision_feedback: list[str] = Field(default_factory=list)
    # LangGraph's message reducer keeps ReAct tool-call/observation history per
    # MemorySaver thread. It is excluded from the public JSON response.
    react_messages: Annotated[list[Any], add_messages] = Field(default_factory=list, exclude=True)
    react_steps: int = 0
    agent_response: str = ""
    agent_error: str | None = None
    validation_evidence: dict[str, Any] | None = None
    tool_observations: list[dict[str, Any]] = Field(default_factory=list)
    tool_errors: list[dict[str, Any]] = Field(default_factory=list)


class ValidationAgentRequest(BaseModel):
    """Standalone endpoint input; existing validator payloads are reused."""

    session_id: str = Field(min_length=1, max_length=128)
    customer_preferences: dict[str, Any] = Field(default_factory=dict)
    proposed_design: dict[str, Any] | None = None
    budget: float | None = Field(default=None, ge=0)
    inventory_request: dict[str, Any] = Field(default_factory=dict)
    price_request: dict[str, Any] | None = None
    catalog_preferences: dict[str, Any] | None = None
    three_d_configuration: dict[str, Any] | None = None
    three_d_field_provenance: dict[str, str] | None = None
    three_d_confirmed_components: dict[str, Any] | None = None
    three_d_approved_mappings: dict[str, Any] | None = None
    revision_state: DecisionState | None = None
    message: str | None = Field(default=None, max_length=4000)


class _CompatibilityToolInput(BaseModel):
    configuration: dict[str, Any]


class _CatalogToolInput(BaseModel):
    catalog_preferences: dict[str, Any]


class _InventoryToolInput(BaseModel):
    material_id: str | None = None
    stones: list[dict[str, Any]] = Field(default_factory=list)
    charms: list[dict[str, Any]] = Field(default_factory=list)


class _AdapterToolInput(BaseModel):
    configuration: dict[str, Any]
    field_provenance: dict[str, str] | None = None
    confirmed_components: dict[str, Any] | None = None
    approved_mappings: dict[str, Any] | None = None
    inventory_request: dict[str, Any] | None = None
    price_request: dict[str, Any] | None = None
    catalog_preferences: dict[str, Any] | None = None


class _PriceToolInput(BaseModel):
    design: dict[str, Any]
    budget: float


class _UnifiedToolInput(BaseModel):
    inventory_request: dict[str, Any]
    price_request: dict[str, Any]
    catalog_preferences: dict[str, Any] | None = None
    three_d_configuration: dict[str, Any] | None = None
    three_d_field_provenance: dict[str, str] | None = None
    three_d_confirmed_components: dict[str, Any] | None = None
    three_d_approved_mappings: dict[str, Any] | None = None


def _tool(name: str, description: str, schema: type[BaseModel], function: Callable[..., Any]) -> StructuredTool:
    return StructuredTool.from_function(
        func=function,
        name=name,
        description=description,
        args_schema=schema,
    )


def _resolve_catalog_tool(catalog_preferences: dict[str, Any]) -> dict[str, Any]:
    return resolve_catalog_preferences(catalog_preferences)


VALIDATION_TOOLS = [
    _tool("validate_3d_compatibility", "Check a 3D generator configuration against confirmed capabilities.", _CompatibilityToolInput, validate_3d_configuration),
    _tool("resolve_catalog", "Resolve requested catalog attributes without selecting ambiguous candidates.", _CatalogToolInput, _resolve_catalog_tool),
    _tool("adapt_three_d_configuration", "Resolve 3D fields into shared canonical catalog, inventory, and pricing inputs without selecting ambiguous components.", _AdapterToolInput, adapt_three_d_configuration),
    _tool("validate_inventory", "Check confirmed material, stone, and charm IDs and quantities against inventory.", _InventoryToolInput, validate_inventory),
    _tool("validate_price", "Check a design against the supplied budget using the authoritative pricing backend.", _PriceToolInput, validate_price),
    _tool("run_unified_validation", "Run the mandatory integrated validation workflow; this is the only tool result that can establish complete validation.", _UnifiedToolInput, orchestrate_validation),
]


@lru_cache(maxsize=1)
def _rag_generation_module():
    """Load the project's existing Gemini-compatible generation provider."""
    module_path = Path(__file__).resolve().parents[1] / "rag" / "scripts" / "11_generation.py"
    spec = importlib.util.spec_from_file_location("_shecraft_rag_generation", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load the configured RAG generation provider.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _generate_narrative(context: Mapping[str, Any]) -> ValidationNarrative:
    """Use the existing Gemini/OpenAI-compatible provider for grounded wording."""
    provider = _rag_generation_module()
    client = provider.get_llm_client()
    prompt_data = {
        "route": context["route"],
        "deterministic_response": context["deterministic_response"],
        "feedback": context["feedback"],
        "clarifications": context["clarifications"],
        "validation_results": context["validation_results"],
        "customer_preferences": context["customer_preferences"],
        "proposed_design": context["proposed_design"],
        "budget": context["budget"],
    }
    system = (
        "You explain a jewelry design validation result to a customer. Return one JSON object with keys "
        "response, clarification_questions, revision_feedback. Use only facts and choices in the supplied "
        "validation data. Never invent, infer, or substitute inventory, availability, prices, IDs, supported "
        "configurations, gemstones, colors, dimensions, or alternatives. Do not change the route or claim "
        "approval unless route is approved. Preserve stated preferences and budget. Ask the customer before "
        "changing an explicitly requested property. If information is missing, ask a concise question. "
        "Revision feedback must only restate corrective actions present in the supplied deterministic feedback. "
        "For a technical error, explain that validation could not be completed and do not request a design change."
    )
    response = client.chat.completions.create(
        model=provider.DEFAULT_LLM_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(prompt_data, ensure_ascii=False, default=str)},
        ],
        response_format={"type": "json_object"},
        temperature=0,
        max_tokens=900,
    )
    content = response.choices[0].message.content
    if not content:
        raise RuntimeError("Gemini returned an empty validation explanation.")
    return ValidationNarrative.model_validate_json(content)


def _get_tool_calling_model(tools: list[StructuredTool] | None = None):
    """Create a Gemini tool model that retains raw provider tool-call metadata."""
    provider = _rag_generation_module()
    return _GeminiToolCallingModel(
        client=provider.get_llm_client(),
        model=provider.DEFAULT_LLM_MODEL,
        tools=tools or VALIDATION_TOOLS,
    )


class _GeminiToolCallingModel:
    """OpenAI-compatible Gemini adapter preserving raw assistant tool calls.

    ChatOpenAI normalizes tool calls to LangChain's AIMessage schema. Gemini 3
    also returns provider-only thought signatures under ``extra_content`` on
    each raw tool call; those signatures must be replayed unchanged alongside
    their paired ToolMessage. Keep the parsed AIMessage for LangGraph routing
    and store the raw assistant message in additional_kwargs for the next turn.
    """

    def __init__(self, *, client: Any, model: str, tools: list[StructuredTool]):
        self.client = client
        self.model = model
        self.tools = [convert_to_openai_tool(tool) for tool in tools]

    @staticmethod
    def _serialize_message(message: Any) -> dict[str, Any]:
        if isinstance(message, SystemMessage):
            return {"role": "system", "content": message.content}
        if isinstance(message, HumanMessage):
            return {"role": "user", "content": message.content}
        if isinstance(message, ToolMessage):
            return {
                "role": "tool",
                "tool_call_id": message.tool_call_id,
                "name": message.name,
                "content": message.content,
            }
        if isinstance(message, AIMessage):
            raw = message.additional_kwargs.get("gemini_raw_message")
            if isinstance(raw, Mapping):
                # Do not normalize or rebuild this message: Gemini requires
                # extra_content.google.thought_signature to be replayed intact.
                return dict(raw)
            raw_calls = []
            for call in message.tool_calls or []:
                raw_calls.append({
                    "id": call.get("id"),
                    "type": "function",
                    "function": {
                        "name": call["name"],
                        "arguments": json.dumps(call.get("args", {}), ensure_ascii=False),
                    },
                })
            raw_message = {"role": "assistant", "content": message.content or ""}
            if raw_calls:
                raw_message["tool_calls"] = raw_calls
            return raw_message
        raise TypeError(f"Unsupported ReAct message type: {type(message).__name__}")

    def invoke(self, messages: list[Any]) -> AIMessage:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[self._serialize_message(message) for message in messages],
            tools=self.tools,
            tool_choice="auto",
            temperature=0,
            max_tokens=1200,
        )
        provider_message = response.choices[0].message
        raw_message = provider_message.model_dump(exclude_none=True)
        normalized_calls = []
        for index, raw_call in enumerate(raw_message.get("tool_calls", []) or []):
            call_id = raw_call.get("id") or f"gemini-tool-{index}"
            raw_call["id"] = call_id
            function = raw_call.get("function") or {}
            try:
                arguments = json.loads(function.get("arguments") or "{}")
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError("Gemini returned malformed tool-call arguments.") from exc
            if not isinstance(arguments, dict):
                raise ValueError("Gemini tool-call arguments must be a JSON object.")
            normalized_calls.append({
                "name": function.get("name"),
                "args": arguments,
                "id": call_id,
                "type": "tool_call",
            })
        return AIMessage(
            content=raw_message.get("content") or "",
            tool_calls=normalized_calls,
            additional_kwargs={"gemini_raw_message": raw_message},
            response_metadata={
                "model_name": self.model,
                "finish_reason": response.choices[0].finish_reason,
                "provider_response_id": getattr(response, "id", None),
            },
        )


def _safe_provider_error(exc: Exception) -> str:
    """Return useful provider diagnostics without credentials or raw request data."""
    status_code = getattr(exc, "status_code", None)
    body = getattr(exc, "body", None)
    if status_code is None and body is None:
        error_name = type(exc).__name__
        if error_name in {"ValidationError", "JSONDecodeError"}:
            return f"Gemini response could not be parsed ({error_name})."
        if "Connection" in error_name:
            return f"Gemini provider connection failed ({error_name})."
        if "Timeout" in error_name:
            return f"Gemini provider request timed out ({error_name})."
        return f"Gemini provider interaction failed ({error_name})."
    details: Mapping[str, Any] = body if isinstance(body, Mapping) else {}
    if isinstance(body, list) and body and isinstance(body[0], Mapping):
        details = body[0]
    error_payload = details.get("error", details)
    if isinstance(error_payload, list) and error_payload and isinstance(error_payload[0], Mapping):
        error_payload = error_payload[0].get("error", error_payload[0])
    if not isinstance(error_payload, Mapping):
        error_payload = {}
    message = error_payload.get("message") or getattr(exc, "message", None) or str(exc)
    code = error_payload.get("status") or error_payload.get("code") or getattr(exc, "code", None)
    message = re.sub(r"(?i)(api[_ -]?key|authorization|bearer|access[_ -]?token)(\s*[:=]\s*)[^\s,;]+", r"\1\2[REDACTED]", str(message))
    message = re.sub(r"https?://[^\s]+", "[provider URL]", message)
    message = message[:900]
    prefix = "Gemini provider request failed"
    if status_code is not None:
        prefix += f" (HTTP {status_code}"
        if code:
            prefix += f", {code}"
        prefix += ")"
    elif code:
        prefix += f" ({code})"
    return f"{prefix}: {message}"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _expected_unified_inputs(state: Mapping[str, Any]) -> dict[str, Any]:
    inventory = state.get("inventory_request") or {"material_id": None, "stones": [], "charms": []}
    price = state.get("price_request")
    if not isinstance(price, Mapping):
        price = {"design": state.get("proposed_design") or {}, "budget": state.get("budget")}
    result: dict[str, Any] = {
        "inventory_request": dict(inventory),
        "price_request": dict(price),
    }
    for name in (
        "catalog_preferences", "three_d_configuration", "three_d_field_provenance",
        "three_d_confirmed_components", "three_d_approved_mappings",
    ):
        if state.get(name) is not None:
            result[name] = state[name]
    return result


def _same_unified_inputs(arguments: Mapping[str, Any], expected: Mapping[str, Any]) -> bool:
    supplied = {key: value for key, value in arguments.items() if value is not None}
    required_keys = set(expected)
    if set(supplied) != required_keys:
        return False
    return all(_canonical_json(supplied[key]) == _canonical_json(expected[key]) for key in required_keys)


def _complete_legacy_unified_result(result: dict[str, Any], arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Certify the legacy non-3D orchestrator shape only when every required check passed."""
    if result.get("complete_validation") is True or arguments.get("three_d_configuration") is not None:
        return result
    checks = result.get("checks")
    if not isinstance(checks, Mapping):
        return result
    expected_checks = {"inventory", "price"}
    if arguments.get("catalog_preferences") is not None:
        expected_checks.add("catalog")
    if (
        result.get("valid") is True
        and result.get("status") == "passed"
        and not result.get("technical_errors")
        and not result.get("clarifications")
        and not result.get("conflicts")
        and set(checks) == expected_checks
        and all(isinstance(checks.get(name), Mapping) and checks[name].get("status") == "passed" for name in expected_checks)
    ):
        result["complete_validation"] = True
        result["canonical_design"] = {
            "inventory_request": arguments.get("inventory_request"),
            "price_request": arguments.get("price_request"),
        }
    return result


def _focused_tool_results(observations: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Build a non-approving decision view from focused deterministic observations."""
    tool_checks = {
        "resolve_catalog": "catalog",
        "validate_inventory": "inventory",
        "validate_price": "price",
        "validate_3d_compatibility": "compatibility",
        "adapt_three_d_configuration": "adapter",
    }
    checks: dict[str, Any] = {}
    errors: list[Any] = []
    warnings: list[Any] = []
    clarifications: list[Any] = []
    technical_errors: list[dict[str, Any]] = []
    for observation in observations:
        check_name = tool_checks.get(str(observation.get("tool")))
        result = observation.get("result")
        if check_name is None or not isinstance(result, Mapping):
            continue
        status = result.get("status")
        if result.get("backend_error") or status == "error":
            check_status = "error"
            technical_errors.extend(
                item if isinstance(item, Mapping) else {"check": check_name, "message": str(item)}
                for item in (result.get("errors") or [{"message": f"{check_name} validation failed technically."}])
            )
        elif status in {"ambiguous", "not_found", "clarification_required"} or result.get("clarifications") or result.get("unresolved_mappings"):
            check_status = "clarification_required"
            details = result.get("clarifications", [])
            if not details:
                error_message = next((
                    str(item.get("message") or "") if isinstance(item, Mapping) else str(item)
                    for item in result.get("errors", [])
                    if item
                ), f"The {check_name} result requires customer clarification.")
                alternatives = _catalog_alternative_summary(result) if check_name == "catalog" else ""
                details = [{
                    "check": check_name,
                    "message": f"{error_message} {alternatives}".strip(),
                    "fields": ["catalog_preferences"] if check_name == "catalog" else [],
                    "customer_confirmation_required": True,
                }]
            clarifications.extend(details)
        elif result.get("conflicts"):
            check_status = "conflict"
        elif result.get("valid") is True or status in {"matched", "passed"}:
            check_status = "passed"
        else:
            check_status = "failed"
        checks[check_name] = {"status": check_status, "result": dict(result)}
        errors.extend(
            {"check": check_name, **item} if isinstance(item, Mapping)
            else {"check": check_name, "message": str(item)}
            for item in (result.get("errors", []) if isinstance(result.get("errors"), list) else [])
        )
        warnings.extend(result.get("warnings", []) if isinstance(result.get("warnings"), list) else [])
    return {
        "valid": False,
        "status": "error" if technical_errors else "incomplete",
        "complete_validation": False,
        "checks": checks,
        "errors": errors,
        "warnings": warnings,
        "technical_errors": technical_errors,
        "clarifications": clarifications,
        "clarification_required": bool(clarifications),
    }


def _catalog_alternative_summary(catalog_result: Mapping[str, Any]) -> str:
    """Render only database-returned alternative IDs and attributes for clarification."""
    matches = catalog_result.get("matches")
    if not isinstance(matches, Mapping):
        return ""
    summaries: list[str] = []
    seen: set[str] = set()
    display_fields = ("metal", "gem", "color", "cut", "purity", "design")
    for group in matches.values():
        entries = group if isinstance(group, list) else [group]
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            for candidate in entry.get("alternatives", []):
                if not isinstance(candidate, Mapping):
                    continue
                candidate_id = str(candidate.get("id") or "").strip()
                if not candidate_id or candidate_id in seen:
                    continue
                seen.add(candidate_id)
                metadata = candidate.get("metadata")
                metadata = metadata if isinstance(metadata, Mapping) else {}
                attributes = [f"{field} {metadata[field]}" for field in display_fields if metadata.get(field) is not None]
                description = f" ({', '.join(attributes)})" if attributes else ""
                summaries.append(f"{candidate_id}{description}")
    if not summaries:
        return ""
    return "Available alternatives are {0}; each requires your confirmation before use.".format(
        "; ".join(summaries)
    )


def _state_payload(state: Mapping[str, Any]) -> dict[str, Any]:
    return ValidationAgentState.model_validate(state).model_dump(mode="json")


def _react_messages(state: Any) -> list[Any]:
    if isinstance(state, Mapping):
        return list(state.get("react_messages", []))
    return list(getattr(state, "react_messages", []))


def _route_from_state(raw_state: Mapping[str, Any] | ValidationAgentState) -> str:
    state = _state_payload(raw_state)
    decision = state.get("routing_decision") or {}
    route = decision.get("route") if isinstance(decision, Mapping) else None
    return route if route in {item.value for item in Route} else Route.TECHNICAL_ERROR.value


def _format_deterministic_response(route: str, feedback: list[dict[str, Any]]) -> str:
    messages = list(dict.fromkeys(str(item.get("explanation", "")) for item in feedback if item.get("explanation")))
    messages = [
        message for message in messages
        if not any(message != other and message in other for other in messages)
    ]
    if route == Route.APPROVED.value:
        return "Validation is complete and all required checks passed. The design is ready for the next workflow step."
    if route == Route.CLARIFICATION_REQUIRED.value:
        return "I need your confirmation before validation can continue. " + (" ".join(messages) if messages else "Please clarify the unresolved design choice.")
    if route == Route.REVISION_REQUIRED.value:
        return "The design needs a revision. " + (" ".join(messages) if messages else "Please review the structured revision feedback.")
    if route == Route.RETRY_LIMIT_REACHED.value:
        return "The design is still unresolved after the allowed attempts. Automatic revision has stopped and human review is needed."
    return "Validation could not be completed because a service error occurred. The design has not been changed."


def _prepare_outcome(raw_state: Mapping[str, Any] | ValidationAgentState, route: str) -> dict[str, Any]:
    state = _state_payload(raw_state)
    decision = state.get("routing_decision") or {}
    feedback = decision.get("feedback", []) if isinstance(decision, Mapping) else []
    feedback = [item for item in feedback if isinstance(item, Mapping)]
    clarifications = [item for item in feedback if item.get("customer_confirmation_required")]
    deterministic_response = _format_deterministic_response(route, feedback)
    handoff_status = {
        Route.APPROVED.value: "ready_for_downstream_workflow",
        Route.CLARIFICATION_REQUIRED.value: "awaiting_customer_confirmation",
        Route.REVISION_REQUIRED.value: "revision_handoff_pending",
        Route.TECHNICAL_ERROR.value: "technical_handoff_pending",
        Route.RETRY_LIMIT_REACHED.value: "human_review_required",
    }[route]
    recipient = {
        Route.APPROVED.value: "requesting_workflow",
        Route.CLARIFICATION_REQUIRED.value: "customer",
        Route.REVISION_REQUIRED.value: "requesting_workflow",
        Route.TECHNICAL_ERROR.value: "service_operator",
        Route.RETRY_LIMIT_REACHED.value: "human_reviewer",
    }[route]
    handoff = {
        "route": route,
        "status": handoff_status,
        "recipient": recipient,
        "executed": False,
        "feedback": [item.model_dump(mode="json") if isinstance(item, BaseModel) else dict(item) for item in feedback],
        "revision_constraints": decision.get("revision_constraints", []) if isinstance(decision, Mapping) else [],
    }
    return {
        "clarification_requests": [dict(item) for item in clarifications],
        "handoff": handoff,
        "deterministic_response": deterministic_response,
        "final_response": deterministic_response,
        "llm_status": "pending",
        "llm_error": None,
    }


def create_validation_graph(
    *,
    checkpointer: Any | None = None,
    validation_runner: Callable[..., Mapping[str, Any]] | None = None,
    narrator: Callable[[Mapping[str, Any]], ValidationNarrative] | None = None,
    agent_model: Any | None = None,
    tools: list[StructuredTool] | None = None,
    max_react_steps: int = MAX_REACT_TOOL_ROUNDS,
):
    """Build a bounded ReAct validation subgraph with deterministic approval routing."""
    if max_react_steps < 1:
        raise ValueError("max_react_steps must be at least 1")
    selected_tools = list(tools or VALIDATION_TOOLS)
    if validation_runner is not None:
        # Retain the existing test seam, but call the injected runner only when
        # the model chooses the registered unified-validation tool.
        selected_tools = [
            StructuredTool.from_function(
                func=validation_runner,
                name="run_unified_validation",
                description="Run the mandatory integrated validation workflow; this is the only tool result that can establish complete validation.",
                args_schema=_UnifiedToolInput,
            ) if tool.name == "run_unified_validation" else tool
            for tool in selected_tools
        ]
    tool_by_name = {tool.name: tool for tool in selected_tools}
    if len(tool_by_name) != len(selected_tools):
        raise ValueError("Validation tool names must be unique")
    selected_model = agent_model
    if selected_model is not None and hasattr(selected_model, "bind_tools"):
        selected_model = selected_model.bind_tools(selected_tools, tool_choice="auto")
    model_cache: dict[str, Any] = {}
    graph = StateGraph(ValidationAgentState)

    def prepare_node(raw_state: Mapping[str, Any]) -> dict[str, Any]:
        state = _state_payload(raw_state)
        messages = list(state.get("conversation_messages", []))
        incoming = state.get("incoming_message")
        if isinstance(incoming, str) and incoming.strip():
            messages.append({"role": "user", "content": incoming.strip()})
        price_request = state.get("price_request")
        conflicts: list[dict[str, Any]] = []
        supplied_budget = state.get("budget")
        price_budget = price_request.get("budget") if isinstance(price_request, Mapping) else None
        if supplied_budget is not None and price_budget is not None and supplied_budget != price_budget:
            conflicts.append({
                "code": "budget_inputs_conflict",
                "check": "price",
                "fields": ["budget", "price_request.budget"],
                "message": "The top-level budget conflicts with the price request budget; confirm which stated limit to use.",
            })
        proposed = state.get("proposed_design")
        nested_design = price_request.get("design") if isinstance(price_request, Mapping) else None
        if proposed is not None and nested_design is not None and proposed != nested_design:
            conflicts.append({
                "code": "design_inputs_conflict",
                "check": "adapter",
                "fields": ["proposed_design", "price_request.design"],
                "message": "The proposed design conflicts with the design sent for pricing; confirm the canonical design.",
            })

        tool_inputs = _expected_unified_inputs(state)
        turn_data = {
            "customer_preferences": state.get("customer_preferences", {}),
            "proposed_design": state.get("proposed_design"),
            "budget": state.get("budget"),
            "inventory_request": tool_inputs["inventory_request"],
            "price_request": tool_inputs["price_request"],
            "catalog_preferences": state.get("catalog_preferences"),
            "three_d_configuration": state.get("three_d_configuration"),
            "three_d_field_provenance": state.get("three_d_field_provenance"),
            "three_d_confirmed_components": state.get("three_d_confirmed_components"),
            "three_d_approved_mappings": state.get("three_d_approved_mappings"),
        }
        user_content = (
            f"Customer message: {incoming.strip() if isinstance(incoming, str) and incoming.strip() else '(no additional message)'}\n\n"
            "Current request state (use these exact values for validation tool arguments):\n"
            f"{json.dumps(turn_data, ensure_ascii=False, default=str)}"
        )
        return {
            "react_messages": [HumanMessage(content=user_content)],
            "validation_results": None,
            "validation_evidence": None,
            "tool_observations": [],
            "tool_errors": [],
            "react_steps": 0,
            "agent_response": "",
            "agent_error": None,
            "canonical_resolved_configuration": None,
            "input_conflicts": conflicts,
            "conversation_messages": messages,
            "incoming_message": None,
        }

    def llm_node(raw_state: Mapping[str, Any]) -> dict[str, Any]:
        state = _state_payload(raw_state)
        try:
            if "model" not in model_cache:
                model_cache["model"] = selected_model or _get_tool_calling_model(selected_tools)
            model = model_cache["model"]
            response = model.invoke([
                SystemMessage(content=VALIDATION_AGENT_SYSTEM_PROMPT),
                *_react_messages(raw_state),
            ])
            if not isinstance(response, AIMessage):
                raise TypeError("The tool-calling model returned an invalid message.")
            calls = [dict(call) for call in (response.tool_calls or [])]
            if calls:
                # Some OpenAI-compatible Gemini responses omit IDs. Add stable
                # local IDs so the corresponding tool observations can be
                # returned in the protocol's required call/response pairing.
                for index, call in enumerate(calls):
                    if not call.get("id"):
                        call["id"] = f"validation-{state.get('session_id') or 'session'}-{state.get('react_steps', 0)}-{index}"
                response = AIMessage(
                    content=response.content,
                    tool_calls=calls,
                    additional_kwargs=response.additional_kwargs,
                    response_metadata=response.response_metadata,
                )
            return {
                "react_messages": [response],
                "react_steps": state.get("react_steps", 0) + 1,
                "agent_response": str(response.content or ""),
                "agent_error": None,
            }
        except Exception as exc:
            safe_error = _safe_provider_error(exc)
            logger.warning("Validation ReAct model call failed: %s", safe_error)
            return {"agent_error": safe_error, "agent_response": ""}

    def route_after_llm(raw_state: Mapping[str, Any]) -> str:
        state = _state_payload(raw_state)
        if state.get("agent_error"):
            return "finalize"
        messages = _react_messages(raw_state)
        last = messages[-1] if messages else None
        if not isinstance(last, AIMessage) or not last.tool_calls:
            return "finalize"
        if state.get("react_steps", 0) > max_react_steps:
            return "limit"
        return "tools"

    def limit_node(_raw_state: Mapping[str, Any]) -> dict[str, Any]:
        return {"agent_error": "max_react_steps_reached"}

    def tools_node(raw_state: Mapping[str, Any]) -> dict[str, Any]:
        state = _state_payload(raw_state)
        messages = _react_messages(raw_state)
        last = messages[-1] if messages else None
        if not isinstance(last, AIMessage) or not last.tool_calls:
            return {"tool_errors": [{"message": "Tool execution was requested without a model tool call."}]}
        expected_inputs = _expected_unified_inputs(state)
        observations = list(state.get("tool_observations", []))
        errors = list(state.get("tool_errors", []))
        tool_messages: list[ToolMessage] = []
        validation_results = state.get("validation_results")
        evidence = state.get("validation_evidence")
        for call in last.tool_calls:
            name = call.get("name")
            call_id = call.get("id") or ""
            arguments = call.get("args")
            if not isinstance(arguments, Mapping):
                arguments = {}
            tool = tool_by_name.get(str(name))
            if tool is None:
                error = {"tool": name, "message": "The model selected an unavailable validation tool."}
                errors.append(error)
                tool_messages.append(ToolMessage(content=json.dumps({"tool_error": error}), tool_call_id=call_id, name=str(name or "unknown_tool")))
                continue
            if name == "run_unified_validation" and not _same_unified_inputs(arguments, expected_inputs):
                error = {"tool": name, "message": "Arguments did not match the current request; no validation was run."}
                errors.append(error)
                tool_messages.append(ToolMessage(content=json.dumps({"tool_error": error}), tool_call_id=call_id, name=str(name)))
                continue
            try:
                result = tool.invoke(dict(arguments))
                observations.append({"tool": str(name), "arguments": dict(arguments), "result": result})
                if isinstance(result, Mapping) and (result.get("backend_error") or result.get("status") == "error"):
                    errors.append({"tool": str(name), "message": "The tool returned a technical error."})
                if name == "run_unified_validation":
                    if not isinstance(result, Mapping):
                        raise TypeError("Unified validation returned a malformed result.")
                    validation_results = dict(result)
                    evidence = {
                        "arguments": dict(arguments),
                        "input_fingerprint": _fingerprint(expected_inputs),
                        "result_fingerprint": _fingerprint(validation_results),
                    }
                tool_messages.append(ToolMessage(
                    content=json.dumps(result, ensure_ascii=False, default=str),
                    tool_call_id=call_id,
                    name=str(name),
                ))
            except Exception as exc:
                logger.exception("Validation tool execution failed (%s).", name)
                error = {"tool": str(name), "message": type(exc).__name__}
                errors.append(error)
                tool_messages.append(ToolMessage(content=json.dumps({"tool_error": error}), tool_call_id=call_id, name=str(name)))
        return {
            "react_messages": tool_messages,
            "tool_observations": observations,
            "tool_errors": errors,
            "validation_results": validation_results,
            "validation_evidence": evidence,
        }

    def finalize_node(raw_state: Mapping[str, Any]) -> dict[str, Any]:
        state = _state_payload(raw_state)
        conflicts = list(state.get("input_conflicts", []))
        result = state.get("validation_results")
        evidence = state.get("validation_evidence")
        expected_inputs = _expected_unified_inputs(state)
        verified = (
            isinstance(result, Mapping)
            and isinstance(evidence, Mapping)
            and evidence.get("input_fingerprint") == _fingerprint(expected_inputs)
            and _same_unified_inputs(evidence.get("arguments", {}), expected_inputs)
            and evidence.get("result_fingerprint") == _fingerprint(result)
        )
        if conflicts:
            final_result = {
                "valid": False,
                "status": "clarification_required",
                "complete_validation": False,
                "checks": {}, "errors": [], "warnings": [], "technical_errors": [],
                "clarification_required": True,
                "conflicts": conflicts,
            }
        elif verified:
            final_result = _complete_legacy_unified_result(dict(result), evidence["arguments"])
            canonical = final_result.get("canonical_design")
            if canonical is None:
                canonical = {
                    "inventory_request": evidence["arguments"].get("inventory_request"),
                    "price_request": evidence["arguments"].get("price_request"),
                }
        elif (state.get("agent_error") and state.get("agent_error") != "max_react_steps_reached") or state.get("tool_errors"):
            error_detail = state.get("agent_error") or "Validation tool execution failed."
            final_result = {
                "valid": False, "status": "error", "complete_validation": False,
                "checks": {}, "errors": [], "warnings": [],
                "technical_errors": [{"check": "validation_agent", "message": str(error_detail)}],
            }
            canonical = None
        else:
            final_result = _focused_tool_results(state.get("tool_observations", []))
            if not final_result.get("checks") and not final_result.get("technical_errors"):
                final_result["clarifications"] = [{
                    "check": "complete_validation",
                    "message": "Complete validation evidence for the current request was not returned.",
                    "fields": [],
                }]
            elif not final_result.get("clarifications") and not final_result.get("errors"):
                final_result["clarifications"] = [{
                    "check": "complete_validation",
                    "message": "Focused checks do not constitute complete validation for the current request.",
                    "fields": [],
                }]
            canonical = None
        if conflicts or (not verified and not state.get("agent_error") and not state.get("tool_errors")):
            canonical = None
        final_result = dict(final_result)
        final_result["agent_tool_observations"] = state.get("tool_observations", [])
        return {
            "validation_results": final_result,
            "canonical_resolved_configuration": canonical,
        }

    def decision_node(raw_state: Mapping[str, Any]) -> dict[str, Any]:
        state = _state_payload(raw_state)
        result = state.get("validation_results") or {}
        decision = decide_validation_route(
            result,
            configuration=(
                state.get("three_d_configuration")
                or state.get("proposed_design")
                or (state.get("price_request") or {}).get("design")
            ),
            state=state.get("revision_state"),
        )
        return {
            "routing_decision": decision.model_dump(mode="json"),
            "revision_state": decision.state.model_dump(mode="json"),
        }

    def route_node(raw_state: Mapping[str, Any]) -> str:
        return _route_from_state(raw_state)

    def make_outcome_node(route: str):
        return lambda state: _prepare_outcome(state, route)

    def narrative_node(raw_state: Mapping[str, Any]) -> dict[str, Any]:
        state = _state_payload(raw_state)
        decision = state.get("routing_decision") or {}
        context = {
            "route": _route_from_state(state),
            "deterministic_response": state.get("deterministic_response", ""),
            "feedback": decision.get("feedback", []) if isinstance(decision, Mapping) else [],
            "clarifications": state.get("clarification_requests", []),
            "validation_results": state.get("validation_results") or {},
            "customer_preferences": state.get("customer_preferences", {}),
            "proposed_design": state.get("proposed_design") or state.get("three_d_configuration"),
            "budget": state.get("budget"),
        }
        generator = narrator or _generate_narrative
        llm_status = "used"
        llm_error = None
        final_response = state.get("deterministic_response", "")
        try:
            raw_narrative = generator(context)
            narrative = raw_narrative if isinstance(raw_narrative, ValidationNarrative) else ValidationNarrative.model_validate(raw_narrative)
            final_response = narrative.response
            revision_feedback = list(narrative.revision_feedback) if context["route"] == Route.REVISION_REQUIRED.value else []
            if revision_feedback:
                final_response += "\n\n" + "\n".join(f"• {item}" for item in revision_feedback)
            if state.get("routing_decision", {}).get("route") == Route.CLARIFICATION_REQUIRED.value:
                clarification_requests = list(context["clarifications"])
                for clarification in context["clarifications"]:
                    if not isinstance(clarification, Mapping) or not clarification.get("customer_confirmation_required"):
                        continue
                    explanation = str(clarification.get("explanation") or "").strip()
                    if explanation and explanation not in final_response:
                        final_response += "\n\n" + explanation
                for question in narrative.clarification_questions:
                    clarification_requests.append({"source": "llm_question", "message": question, "customer_confirmation_required": True})
                llm_questions = narrative.clarification_questions
            else:
                clarification_requests = list(context["clarifications"])
                llm_questions = []
            # Deterministic action guidance remains authoritative and is never
            # replaced by generated text. Narration is an additive presentation layer.
            return {
                "final_response": final_response,
                "clarification_requests": clarification_requests,
                "conversation_messages": [
                    *state.get("conversation_messages", []),
                    {"role": "assistant", "content": final_response},
                ],
                "llm_status": llm_status,
                "llm_error": llm_error,
                "llm_clarification_questions": llm_questions,
                "llm_revision_feedback": revision_feedback,
            }
        except Exception as exc:
            safe_error = _safe_provider_error(exc)
            logger.warning("Validation narration is unavailable; using deterministic response: %s", safe_error)
            llm_status = "unavailable"
            llm_error = safe_error
            final_response = state.get("deterministic_response", "Validation finished; see the structured result.")
            return {
                "final_response": final_response,
                "conversation_messages": [
                    *state.get("conversation_messages", []),
                    {"role": "assistant", "content": final_response},
                ],
                "llm_status": llm_status,
                "llm_error": llm_error,
                "llm_clarification_questions": [],
                "llm_revision_feedback": [],
            }

    route_to_node = {
        Route.APPROVED.value: "approved_handoff",
        Route.CLARIFICATION_REQUIRED.value: "clarification_handoff",
        Route.REVISION_REQUIRED.value: "revision_handoff",
        Route.TECHNICAL_ERROR.value: "technical_handoff",
        Route.RETRY_LIMIT_REACHED.value: "retry_limit_handoff",
    }
    graph.add_node("prepare", prepare_node)
    graph.add_node("agent", llm_node)
    graph.add_node("tools", tools_node)
    graph.add_node("tool_limit", limit_node)
    graph.add_node("finalize", finalize_node)
    graph.add_node("decide", decision_node)
    for route, node_name in route_to_node.items():
        graph.add_node(node_name, make_outcome_node(route))
    graph.add_node("narrate", narrative_node)
    graph.add_edge(START, "prepare")
    graph.add_edge("prepare", "agent")
    graph.add_conditional_edges("agent", route_after_llm, {
        "tools": "tools", "limit": "tool_limit", "finalize": "finalize",
    })
    graph.add_edge("tools", "agent")
    graph.add_edge("tool_limit", "finalize")
    graph.add_edge("finalize", "decide")
    graph.add_conditional_edges("decide", route_node, route_to_node)
    for node_name in route_to_node.values():
        graph.add_edge(node_name, "narrate")
    graph.add_edge("narrate", END)
    return graph.compile(checkpointer=checkpointer or MemorySaver())


@lru_cache(maxsize=1)
def get_validation_graph():
    """Process-local graph and MemorySaver; thread IDs isolate customer sessions."""
    return create_validation_graph()


def run_validation_agent(
    request: ValidationAgentRequest | Mapping[str, Any],
    *,
    graph: Any | None = None,
) -> ValidationAgentState:
    """Run one turn, reusing the checkpointer state for the supplied session."""
    model = request if isinstance(request, ValidationAgentRequest) else ValidationAgentRequest.model_validate(request)
    payload = model.model_dump(mode="json", exclude_unset=True)
    # The existing price request remains the source consumed by the orchestrator.
    # These convenience fields are retained in the state and compared for conflict.
    payload["session_id"] = model.session_id
    if model.revision_state is None:
        payload.pop("revision_state", None)
    if model.message is not None:
        payload["incoming_message"] = model.message
    payload.pop("message", None)
    if model.revision_state is not None:
        payload["revision_state"] = model.revision_state.model_dump(mode="json")
    selected_graph = graph or get_validation_graph()
    result = selected_graph.invoke(
        payload,
        config={"configurable": {"thread_id": model.session_id}},
    )
    return ValidationAgentState.model_validate(result)
