"""Stream user-visible graph activity and a final response to the React chat widget."""

import json
import logging
from collections.abc import Callable
from uuid import UUID

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .graph import build_graph

logger = logging.getLogger("uvicorn.error.shecraft.agents")


class AgentRequest(BaseModel):
    conversation_id: UUID
    message: str = Field(min_length=1, max_length=2000)


def create_router(knowledge: Callable[[str], dict], graph_factory=build_graph) -> APIRouter:
    router = APIRouter()
    graph = None
    active: set[str] = set()

    @router.post("/api/assistant/chat")
    async def chat(request: AgentRequest):
        nonlocal graph
        message = request.message.strip()
        if not message:
            raise HTTPException(422, "Enter a message.")
        thread = str(request.conversation_id)
        if thread in active:
            raise HTTPException(409, "A reply is already in progress for this conversation.")
        if graph is None:
            try:
                graph = graph_factory(knowledge)
            except (KeyError, ValueError) as exc:
                raise HTTPException(503, "Check the Gemini configuration in ai-service/.env.") from exc
        active.add(thread)

        async def stream():
            try:
                config = {"configurable": {"thread_id": thread}, "recursion_limit": 28}
                async for mode, data in graph.astream({"message": message}, config, stream_mode=["custom", "updates"]):
                    if mode == "custom":
                        yield json.dumps(data) + "\n"
                    elif "finish" in data:
                        final = data["finish"]
                        yield json.dumps({"type": "result", "answer": final["answer"],
                                          "brief": final["brief"], "sources": final["sources"]}) + "\n"
            except Exception as exc:
                logger.warning("Assistant turn failed (%s)", type(exc).__name__)
                detail = "The assistant could not finish this turn. Please try again."
                if "429" in str(exc) or "RESOURCE_EXHAUSTED" in str(exc):
                    detail = "Gemini's free-tier limit was reached. Please retry after your quota refreshes."
                elif isinstance(exc, HTTPException):
                    detail = exc.detail
                yield json.dumps({"type": "error", "message": detail}) + "\n"
            finally:
                active.discard(thread)

        return StreamingResponse(stream(), media_type="application/x-ndjson",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    return router
