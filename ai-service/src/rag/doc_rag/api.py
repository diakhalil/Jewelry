"""OpenAI-compatible FastAPI server for Open WebUI."""

from __future__ import annotations

import logging
import os
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from doc_rag.local_rag import LocalRagService
    from doc_rag.rag_service import RagService

AI_SERVICE_ROOT = Path(__file__).resolve().parents[3]
load_dotenv(AI_SERVICE_ROOT / ".env")
load_dotenv(AI_SERVICE_ROOT.parent / ".env")
PUBLIC_BASE_URL = os.getenv(
    "RAG_PUBLIC_BASE_URL",
    "http://host.docker.internal:8001",
)


logger = logging.getLogger("uvicorn.error.shecraft")
local_mode = os.getenv("RAG_MODE") == "local"
service: RagService | LocalRagService | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global service
    logger.info("RAG API starting mode=%s", "local" if local_mode else "server")
    if local_mode:
        from doc_rag.local_rag import LocalRagService

        service = LocalRagService()
    else:
        from doc_rag.rag_service import RagService

        service = RagService()
    try:
        yield
    finally:
        service.qdrant.close()
        service = None


app = FastAPI(title="Gatherly RAG API", version="1.0.0", lifespan=lifespan)

RAG_MODEL_ID = "gatherly-rag"


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str = RAG_MODEL_ID
    messages: list[ChatMessage]
    stream: bool = False
    temperature: Optional[float] = 0.2
    max_tokens: Optional[int] = 2048

class AskRequest(BaseModel):
    question: str
    max_tokens: Optional[int] = 2048
    experience: Literal["shecraft"] | None = None

class AskOpsRequest(BaseModel):
    question: str
    topics: list[str] = Field(default_factory=list)
    max_tokens: Optional[int] = 2048



@app.get("/health")
def health() -> dict[str, Any]:
    if service is None:
        raise HTTPException(status_code=503, detail="RAG service not ready")
    if local_mode:
        return {
            "status": "ok",
            "text_collection": service.text_collection,
            "documents": service.document_count,
            "answer_mode": service.answer_mode,
        }
    if not os.getenv("GEMINI_API_KEY"):
        raise HTTPException(status_code=503, detail="Set GEMINI_API_KEY in ai-service/.env")
    return {"status": "ok", "text_collection": service.text_collection}


@app.get("/v1/models")
def list_models() -> dict[str, Any]:
    return {
        "object": "list",
        "data": [
            {
                "id": RAG_MODEL_ID,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "gatherly",
            }
        ],
    }





@app.get("/media/{image_id}")
def get_image(image_id: str):
    if service is None:
        raise HTTPException(status_code=503, detail="RAG service not ready")
    record = service.image_by_id.get(image_id)
    if not record:
        raise HTTPException(status_code=404, detail="image not found")
    path = Path(record["image_path"])
    if not path.is_file():
        raise HTTPException(status_code=404, detail="image file missing")
    return FileResponse(path)

@app.post("/v1/ask")
def ask(body: AskRequest, x_request_id: str | None = Header(default=None)) -> dict[str, Any]:
    """Structured RAG result for Gatherly chat (answer + paired cards)."""
    if service is None:
        raise HTTPException(status_code=503, detail="RAG service not ready")
    question = (body.question or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="question is required")

    try:
        request_id = str(uuid.UUID(x_request_id)) if x_request_id else str(uuid.uuid4())
    except ValueError:
        request_id = str(uuid.uuid4())
    started = time.perf_counter()
    logger.info("request=%s step=chat.started characters=%s", request_id, len(question))
    if not local_mode and not os.getenv("GEMINI_API_KEY"):
        logger.warning("request=%s step=chat.failed reason=missing_gemini_key", request_id)
        raise HTTPException(status_code=503, detail="Set GEMINI_API_KEY in ai-service/.env")
    try:
        result = service.ask(
            question,
            max_tokens=body.max_tokens or 2048,
            public_base_url=PUBLIC_BASE_URL,
            jewelry=body.experience == "shecraft",
            request_id=request_id,
        )
    except Exception as exc:
        logger.error(
            "request=%s step=chat.failed reason=%s upstream_status=%s",
            request_id, type(exc).__name__, getattr(exc, "status_code", None),
        )
        raise HTTPException(status_code=502, detail="RAG failed. See the Python service log.") from exc
    logger.info("request=%s step=chat.completed elapsed_ms=%.1f", request_id, (time.perf_counter() - started) * 1000)
    text_sources = []
    for s in (result.get("sources") or [])[:8]:
        text = str(s.get("text") or "").strip()
        if len(text) > 280:
            text = text[:280] + "…"
        text_sources.append(
            {
                "file_name": s.get("file_name"),
                "page_number": s.get("page_number"),
                "section_title": str(s.get("section_title") or "").strip(),
                "text": text,
            }
        )

    image_sources = []
    for img in result.get("image_sources") or []:
        pages = img.get("page_numbers") or img.get("page_number") or ""
        image_sources.append(
            {
                "image_id": img.get("image_id"),
                "file_name": img.get("file_name"),
                "section_title": str(img.get("section_title") or "").strip(),
                "page": pages,
            }
        )

    return {
        "answer": result["answer"],
        "mode": result["mode"],
        "cards": result["cards"],
        "text_sources": text_sources,
        "image_sources": image_sources,
        "public_base_url": result["public_base_url"],
        "image_debug": [
            {
                "image_id": img.get("image_id"),
                "section_title": img.get("section_title"),
                "score": img.get("score"),
                "confidence": img.get("confidence"),
                "fallback_used": img.get("fallback_used"),
                "method": img.get("method"),
            }
            for img in (result.get("image_sources") or [])
        ],
    }

@app.post("/v1/chat/completions")
def chat_completions(
    body: ChatCompletionRequest,
    authorization: Optional[str] = Header(default=None),
) -> dict[str, Any]:
    if body.stream:
        raise HTTPException(status_code=400, detail="stream=false only for now")
    if service is None:
        raise HTTPException(status_code=503, detail="RAG service not ready")

    user_msgs = [m.content for m in body.messages if m.role == "user" and m.content.strip()]
    if not user_msgs:
        raise HTTPException(status_code=400, detail="No user message found")
    query = user_msgs[-1].strip()

    result = service.ask(
        query,
        max_tokens=body.max_tokens or 2048,
        public_base_url=PUBLIC_BASE_URL,
    )
    answer = result["answer"]
    base = result["public_base_url"]

    # Text sources
    cites = []
    for i, s in enumerate(result["sources"][:5], start=1):
        text = str(s.get("text", "")).strip()
        if len(text) > 400:
            text = text[:400] + "…"
        cites.append(f"[T{i}] {s.get('file_name')} p.{s.get('page_number')}\n{text}")

    # Image sources + markdown previews for Open WebUI
    for i, s in enumerate(result["image_sources"][:3], start=1):
        image_id = s.get("image_id")
        visual = str(s.get("visual_description") or "")[:400]
        url = f"{base}/media/{image_id}"
        cites.append(
            f"[I{i}] {s.get('file_name')} | {s.get('section_title')}\n"
            f"{visual}\n"
            f"![]({url})"
        )

    if cites:
        answer = answer + "\n\nSources:\n\n" + "\n\n".join(cites)

    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": body.model or RAG_MODEL_ID,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": answer},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


@app.post("/v1/ask_by_image")
async def ask_by_image(
    file: UploadFile = File(...),
    question: str = Form(""),
    max_tokens: int = Form(2048),
) -> dict[str, Any]:
    if service is None:
        raise HTTPException(status_code=503, detail="RAG service not ready")
    if local_mode:
        raise HTTPException(status_code=501, detail="Image questions require the full RAG service")

    suffix = Path(file.filename or "upload.jpg").suffix or ".jpg"
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="empty image")

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(raw)
        tmp_path = tmp.name

    try:
        return service.ask_by_image(
            tmp_path,
            question=question or "",
            max_tokens=max_tokens,
            public_base_url=PUBLIC_BASE_URL,
        )
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error
    finally:
        Path(tmp_path).unlink(missing_ok=True)


@app.post("/v1/ask_ops")
def ask_ops(body: AskOpsRequest) -> dict[str, Any]:
    """Ops/checklist/sustainable/Arabic RAG only."""
    if service is None:
        raise HTTPException(status_code=503, detail="RAG service not ready")
    if local_mode:
        raise HTTPException(status_code=501, detail="Operations questions require the full RAG service")
    question = (body.question or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="question is required")
    topics = [str(t).strip().lower() for t in (body.topics or []) if str(t).strip()]
    if not topics:
        raise HTTPException(status_code=400, detail="topics is required")

    try:
        result = service.ask_ops(
            question,
            topics,
            max_tokens=body.max_tokens or 2048,
            public_base_url=PUBLIC_BASE_URL,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    if result.get("status") != "success":
        return result

    text_sources = []
    for s in (result.get("sources") or [])[:10]:
        text = str(s.get("text") or "").strip()
        snippet = text[:280] + "…" if len(text) > 280 else text
        text_sources.append(
            {
                "file_name": s.get("file_name"),
                "page_number": s.get("page_number"),
                "section_title": str(s.get("section_title") or "").strip(),
                "text": text,
                "snippet": snippet,
                "_topic": s.get("_topic") or "",
            }
        )

    return {
        "status": "success",
        "answer": result.get("answer"),
        "topics": result.get("topics"),
        "text_sources": text_sources,
        "mode": result.get("mode"),
        "public_base_url": result.get("public_base_url"),
    }

