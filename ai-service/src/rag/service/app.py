"""FastAPI endpoint for retrieving Jewelry RAG context and generating answers."""

from __future__ import annotations

import importlib
import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path, PureWindowsPath
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field


AI_SERVICE_ROOT = Path(__file__).resolve().parents[3]
RAG_ROOT = AI_SERVICE_ROOT / "src" / "rag"
if str(RAG_ROOT) not in sys.path:
    sys.path.insert(0, str(RAG_ROOT))
if str(RAG_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(RAG_ROOT.parent))

from agents.api import create_router

indexing = importlib.import_module("scripts.6_indexing")
embedding_utils = importlib.import_module("scripts.5_embeddings")
retrieval = importlib.import_module("scripts.7_retrieval")
generation = importlib.import_module("scripts.11_generation")

logger = logging.getLogger("shecraft.rag")
EMBEDDING_MODEL = "gemini-embedding-2"
TEXT_COLLECTION = indexing.DEFAULT_COLLECTION_NAME
IMAGE_COLLECTION = indexing.IMAGE_COLLECTION_NAME
PROCESSED_DOCUMENTS_ROOT = (
    indexing.PROJECT_ROOT / "data" / "rag" / "processed_documents"
).resolve()


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    text_top_k: int = Field(default=5, ge=1, le=10)
    image_top_k: int = Field(default=3, ge=1, le=10)


class GeminiQueryEncoder:
    """Adapt the Gemini embedding API to the retrieval helpers' encode API."""

    def __init__(self, client: Any):
        self.client = client
        self._last_query: str | None = None
        self._last_vector = None

    def encode(self, sentences, **_kwargs):
        values = [str(sentence).strip() for sentence in sentences]
        if len(values) != 1:
            raise ValueError("The RAG query encoder expects one query at a time.")
        query = values[0]
        if query != self._last_query:
            vectors = embedding_utils.embed_gemini_texts(
                [query],
                client=self.client,
                model_name=EMBEDDING_MODEL,
                text_type="query",
            )
            self._last_query = query
            self._last_vector = vectors
        return self._last_vector


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    client = getattr(_app.state, "qdrant", None)
    if client is not None:
        client.close()


app = FastAPI(
    title="SheCraft Jewelry RAG",
    version="1.0.0",
    description="Retrieves jewelry knowledge from Qdrant and generates cited answers.",
    lifespan=lifespan,
)

allowed_origins = [
    origin.strip()
    for origin in os.getenv(
        "RAG_ALLOWED_ORIGINS", "http://localhost:3000"
    ).split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


def _get_qdrant():
    client = getattr(app.state, "qdrant", None)
    if client is None:
        try:
            client = indexing.connect_to_qdrant()
            app.state.qdrant = client
        except Exception as exc:
            logger.exception("Could not connect the RAG service to Qdrant.")
            raise HTTPException(
                status_code=503,
                detail="Cannot connect to Qdrant. Check QDRANT_URL and that the server is running.",
            ) from exc

    try:
        missing = [
            name for name in (TEXT_COLLECTION, IMAGE_COLLECTION)
            if not client.collection_exists(name)
        ]
    except Exception as exc:
        logger.exception("Could not inspect Qdrant collections.")
        raise HTTPException(status_code=503, detail="Qdrant is unavailable.") from exc

    if missing:
        raise HTTPException(
            status_code=503,
            detail=(
                "RAG collections are missing from Qdrant: " + ", ".join(missing)
                + ". Run the document ingestion pipeline first."
            ),
        )
    return client


def _get_embedding_client():
    client = getattr(app.state, "gemini_embeddings", None)
    if client is None:
        try:
            client = embedding_utils.create_gemini_client(AI_SERVICE_ROOT / ".env")
            app.state.gemini_embeddings = client
        except Exception as exc:
            logger.exception("Could not initialize Gemini embeddings.")
            raise HTTPException(
                status_code=503,
                detail="Gemini embeddings are unavailable. Check GEMINI_API_KEY in ai-service/.env.",
            ) from exc
    return client


@app.get("/health")
def health():
    """Report whether the configured Qdrant collections are ready."""
    client = _get_qdrant()
    return {
        "status": "ok",
        "qdrant": "connected",
        "text_collection": TEXT_COLLECTION,
        "image_collection": IMAGE_COLLECTION,
    }


@app.get("/api/images/{image_id}")
def get_image(image_id: str):
    """Serve an extracted image by ID, never by a caller-supplied path."""
    if not image_id or len(image_id) > 180:
        raise HTTPException(status_code=404, detail="Image not found.")

    if PROCESSED_DOCUMENTS_ROOT.is_dir():
        for context_file in PROCESSED_DOCUMENTS_ROOT.glob("*/image_context.json"):
            try:
                payload = json.loads(context_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue

            for record in payload.get("images", []):
                if str(record.get("image_id", "")) != image_id:
                    continue
                stored_name = str(record.get("stored_file_name") or "").strip()
                if not stored_name:
                    stored_name = PureWindowsPath(
                        str(record.get("image_path") or "")
                    ).name
                if not stored_name or Path(stored_name).name != stored_name:
                    break

                image_path = (
                    context_file.parent / "images" / stored_name
                ).resolve()
                if (
                    image_path.is_relative_to(PROCESSED_DOCUMENTS_ROOT)
                    and image_path.is_file()
                ):
                    return FileResponse(image_path)
                break

    raise HTTPException(
        status_code=404,
        detail="Image file is not available in this service's processed-document cache.",
    )


@app.post("/api/chat")
def chat(request: ChatRequest):
    """Retrieve text and image descriptions, then generate a cited response."""
    question = request.message.strip()
    if not question:
        raise HTTPException(status_code=422, detail="Enter a question.")

    qdrant = _get_qdrant()
    encoder = GeminiQueryEncoder(_get_embedding_client())
    try:
        text_results = retrieval.retrieve_dense_qdrant(
            question,
            qdrant,
            encoder,
            collection_name=TEXT_COLLECTION,
            top_k=request.text_top_k,
            model_name=EMBEDDING_MODEL,
        )
        image_results = retrieval.retrieve_images_qdrant(
            question,
            qdrant,
            encoder,
            collection_name=IMAGE_COLLECTION,
            top_k=request.image_top_k,
            model_name=EMBEDDING_MODEL,
        )

        text_contexts = text_results.to_dict(orient="records")
        image_contexts = image_results.to_dict(orient="records")
        answer = generation.generate_answer(
            question,
            text_contexts,
            image_contexts=image_contexts,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("RAG request failed.")
        raise HTTPException(
            status_code=502,
            detail="The RAG request failed. Check the service logs and Gemini API configuration.",
        ) from exc

    sources = []
    for index, row in enumerate(text_contexts, start=1):
        sources.append({
            "label": f"T{index}",
            "kind": "text",
            "file_name": row.get("file_name", "Unknown PDF"),
            "page_number": row.get("page_number"),
            "score": _safe_score(row.get("score")),
            "excerpt": str(row.get("text") or "")[:420],
        })
    for index, row in enumerate(image_contexts, start=1):
        sources.append({
            "label": f"I{index}",
            "kind": "image",
            "file_name": row.get("file_name", "Unknown PDF"),
            "page_number": row.get("page_numbers") or row.get("page_number"),
            "score": _safe_score(row.get("score")),
            "excerpt": str(row.get("visual_description") or "")[:420],
            "image_id": str(row.get("image_id") or ""),
            "image_url": (
                f"/api/images/{quote(str(row['image_id']), safe='')}"
                if row.get("image_id") else None
            ),
        })

    return {
        "answer": answer,
        "sources": sources,
        "retrieval": {
            "text_method": "dense_qdrant",
            "image_method": "image_context_qdrant",
            "embedding_model": EMBEDDING_MODEL,
        },
    }


def _safe_score(value):
    try:
        return round(float(value), 4)
    except (TypeError, ValueError):
        return None


app.include_router(create_router(lambda question: chat(ChatRequest(message=question))))
