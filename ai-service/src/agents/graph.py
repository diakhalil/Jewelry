"""The shared supervisor graph. Only model-issued tool calls dispatch specialists or checks."""

import logging
import os
import re
from collections.abc import Callable
from typing import Literal

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.rate_limiters import InMemoryRateLimiter
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field, ValidationError

from .brief import Brief, RequirementsUpdate, apply_update, menu_update
from .prompts import REQUIREMENTS, STRUCTURE, SUPERVISOR
from .tools import REQUIREMENTS_TOOLS

logger = logging.getLogger("uvicorn.error.shecraft.agents")


class DelegateRequirements(BaseModel):
    """Delegate a customer design request, preference revision or storefront options question."""
    request: str


class ConsultGuides(BaseModel):
    """Consult the jewelry knowledge specialist for a standalone factual question and cited sources."""
    question: str


class Conversation(BaseModel):
    message: str
    history: list[AnyMessage] = Field(default_factory=list)
    brief: Brief = Field(default_factory=Brief)
    # The working state is cleared at the start of each turn. Commit the draft only on success.
    draft: Brief = Field(default_factory=Brief)
    supervisor_work: list[AnyMessage] = Field(default_factory=list)
    requirements_work: list[AnyMessage] = Field(default_factory=list)
    pending: list[dict] = Field(default_factory=list)
    sources: list[dict] = Field(default_factory=list)
    answer: str = ""


def activity(message: str):
    get_stream_writer()({"type": "progress", "message": message})


def trace(message):
    if os.getenv("SHECRAFT_AGENT_TRACE", "").lower() in {"1", "true"}:
        # Log public text/tool messages, excluding model thought blocks and opaque signatures.
        content = message.text
        logger.info("%s %s %s", type(message).__name__, content,
                    getattr(message, "tool_calls", "") or getattr(message, "tool_call_id", ""))


def text_message(content):
    message = SystemMessage(content=content)
    trace(message)
    return message


def build_graph(knowledge: Callable[[str], dict], model=None):
    if model is None:
        model = ChatGoogleGenerativeAI(
            model=os.getenv("REQUIREMENTS_MODEL", "gemini-3.5-flash-lite"),
            api_key=os.environ["GEMINI_API_KEY"], vertexai=False,
            timeout=45, max_retries=2,
            rate_limiter=InMemoryRateLimiter(requests_per_second=0.2, max_bucket_size=1),
        )
    supervisor = model.bind_tools([DelegateRequirements, ConsultGuides])
    specialist = model.bind_tools(REQUIREMENTS_TOOLS)
    structure = model.with_structured_output(RequirementsUpdate, method="json_schema")
    tools = {tool.name: tool for tool in REQUIREMENTS_TOOLS}
    progress = {
        "check_required_fields": "Checking which design details are still needed…",
        "check_preference_consistency": "Checking your preferences for conflicts…",
        "lookup_supported_options": "Looking up supported jewelry options…",
    }

    def begin(state: Conversation):
        user = HumanMessage(content=state.message)
        trace(user)
        draft, answer = state.brief, ""
        selected = menu_update(draft, state.message)
        if selected:
            activity("Saving your selection…")
            draft, answer = apply_update(draft, selected, state.message)
        return {"supervisor_work": [user], "requirements_work": [], "pending": [],
                "draft": draft.model_dump(), "answer": answer, "sources": []}

    def coordinate(state: Conversation):
        activity("Finding the right specialist…")
        system = text_message(SUPERVISOR + "\nCurrent design brief: " + state.draft.model_dump_json())
        result = supervisor.invoke([system, *state.history[-12:], *state.supervisor_work])
        trace(result)
        return {"supervisor_work": [*state.supervisor_work, result], "pending": result.tool_calls}

    def route(state: Conversation) -> Literal["customer_requirements", "gemstone_knowledge", "finish"]:
        if not state.pending:
            return "finish"
        name = state.pending[0]["name"]
        if name == "DelegateRequirements":
            return "customer_requirements"
        if name == "ConsultGuides":
            return "gemstone_knowledge"
        raise ValueError("Unknown supervisor delegation")

    def observation(state, content, **updates):
        call = state.pending[0]
        msg = ToolMessage(content=content, name=call["name"], tool_call_id=call["id"])
        trace(msg)
        return {"supervisor_work": [*state.supervisor_work, msg], "pending": state.pending[1:],
                "requirements_work": [], **updates}

    def requirements(state: Conversation):
        if not state.requirements_work:
            activity("Reading your design preferences…")
            request = DelegateRequirements.model_validate(state.pending[0]["args"]).request
            work = [HumanMessage(content=f"Customer message: {state.message}\nSupervisor handoff: {request}")]
        else:
            work = state.requirements_work
        system = text_message(REQUIREMENTS + "\nSaved brief: " + state.draft.model_dump_json())
        result = specialist.invoke([system, *state.history[-12:], *work])
        trace(result)
        return {"requirements_work": [*work, result]}

    def run_tools(state: Conversation):
        work = list(state.requirements_work)
        for call in state.requirements_work[-1].tool_calls:
            tool = tools.get(call["name"])
            if tool is None:
                msg = ToolMessage(content="Unknown requirements tool.", tool_call_id=call["id"])
            else:
                activity(progress[tool.name])
                try:
                    msg = tool.invoke(call)
                except ValidationError:
                    msg = ToolMessage(content="Invalid tool arguments. Check the tool schema and correct the input.",
                                      name=tool.name, tool_call_id=call["id"], status="error")
            trace(msg)
            work.append(msg)
        return {"requirements_work": work}

    def save_requirements(state: Conversation):
        activity("Preparing your design brief…")
        system = text_message(STRUCTURE + "\nSaved brief: " + state.draft.model_dump_json())
        update = structure.invoke([system, *state.history[-12:], *state.requirements_work,
                                   HumanMessage(content="Extract the update from the customer's exact message:\n" + state.message)])
        brief, reply = apply_update(state.draft, update, state.message)
        if os.getenv("SHECRAFT_AGENT_TRACE", "").lower() in {"1", "true"}:
            logger.info("RequirementsUpdate %s", update.model_dump_json())
            logger.info("Brief %s", brief.model_dump_json())
        return observation(state, reply, draft=brief.model_dump(), answer=reply)

    def consult(state: Conversation):
        activity("Searching the jewelry guides…")
        question = ConsultGuides.model_validate(state.pending[0]["args"]).question
        result = knowledge(question)
        # Each RAG call numbers its citations from 1. Keep labels unique if the
        # supervisor asks more than one guide question during the same turn.
        counts = {prefix: sum(s["label"].startswith(prefix) for s in state.sources) for prefix in ("T", "I")}
        labels = {}
        sources = list(state.sources)
        for source in result["sources"]:
            old = source["label"]
            prefix = old[0]
            counts[prefix] += 1
            labels[old] = f"{prefix}{counts[prefix]}"
            sources.append({**source, "label": labels[old]})
        answer = re.sub(r"\b[TI]\d+\b", lambda m: labels.get(m[0], m[0]), result["answer"])
        return observation(state, answer, sources=sources)

    def finish(state: Conversation):
        # A supervisor paraphrase can invent readiness, delegation or a second question.
        # Preserve cited guide answers and render the final validated brief response last.
        observations = [m for m in state.supervisor_work if isinstance(m, ToolMessage)]
        parts = [m.text for m in observations if m.name == "ConsultGuides"]
        if state.answer:
            parts.append(state.answer)
        answer = "\n\n".join(parts) if parts else state.supervisor_work[-1].text
        if not answer.strip():
            raise ValueError("The supervisor returned an empty response")
        history = [*state.history[-10:], HumanMessage(content=state.message), AIMessage(content=answer)]
        return {"brief": state.draft.model_dump(), "history": history, "answer": answer, "sources": state.sources}

    graph = StateGraph(Conversation)
    for name, node in (("begin", begin), ("supervisor", coordinate),
                       ("customer_requirements", requirements), ("requirements_tools", run_tools),
                       ("requirements_output", save_requirements), ("gemstone_knowledge", consult), ("finish", finish)):
        graph.add_node(name, node)
    graph.add_edge(START, "begin")
    graph.add_conditional_edges("begin", lambda state: "finish" if state.answer else "supervisor", ["finish", "supervisor"])
    graph.add_conditional_edges("supervisor", route)
    graph.add_conditional_edges("customer_requirements",
        lambda state: "requirements_tools" if state.requirements_work[-1].tool_calls else "requirements_output",
        ["requirements_tools", "requirements_output"])
    graph.add_edge("requirements_tools", "customer_requirements")
    for node in ("requirements_output", "gemstone_knowledge"):
        graph.add_conditional_edges(node, route)
    graph.add_edge("finish", END)
    return graph.compile(checkpointer=InMemorySaver())
