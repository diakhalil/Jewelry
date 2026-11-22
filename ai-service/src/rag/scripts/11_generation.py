"""Jewelry RAG answer generation through the Gemini OpenAI-compatible API."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI

PROJECT_ROOT = Path(__file__).resolve().parents[3]
load_dotenv(PROJECT_ROOT / ".env", override=False)
DEFAULT_LLM_MODEL = os.getenv("GENERATION_MODEL", "gemini-3.5-flash")


def get_llm_client() -> OpenAI:
    """Create the Gemini client using GEMINI_API_KEY from environment/.env."""
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        raise RuntimeError(f"GEMINI_API_KEY is missing from {PROJECT_ROOT / '.env'}")
    return OpenAI(
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        api_key=key,
    )


def build_rag_messages(
    query: str,
    contexts: list[dict[str, Any]],
    image_contexts: list[dict[str, Any]] | None = None,
) -> list[dict[str, str]]:
    """Build jewelry-specific prompts from retrieved text and visual descriptions."""
    blocks: list[str] = []
    for index, ctx in enumerate(contexts or [], start=1):
        file_name = ctx.get("file_name", "unknown PDF")
        page = ctx.get("page_number", "?")
        section = str(ctx.get("section_title") or "").strip()
        text = str(ctx.get("text") or "").strip()
        header = f"[T{index}] DOCUMENT TEXT | {file_name} (page {page})"
        if section:
            header += f" | section: {section}"
        blocks.append(f"{header}\n{text}")

    for index, ctx in enumerate(image_contexts or [], start=1):
        image_id = ctx.get("image_id", "?")
        file_name = ctx.get("file_name", "unknown PDF")
        pages = ctx.get("page_numbers") or ctx.get("page_number") or "?"
        visual = str(ctx.get("visual_description") or "").strip()
        blocks.append(
            f"[I{index}] IMAGE | id={image_id} | {file_name} | pages {pages}\n"
            f"Visible details: {visual or '(no description available)'}"
        )

    context_block = "\n\n".join(blocks) if blocks else "(no context retrieved)"
    system = (
        "You are a helpful jewelry and gemstone information assistant.\n"
        "Answer from the provided retrieved context only.\n"
        "Use document text for factual explanations and instructions. Use image descriptions "
        "only to describe visible jewelry or objects. A PDF filename is source metadata, "
        "not evidence for factual claims. Do not infer hidden details from an image.\n"
        "Be concise, answer in the language of the question, and cite supporting sources "
        "using their labels such as [T1] or [I1]. If the context does not support an answer, "
        "say what information is missing instead of guessing."
    )
    user = f"Question:\n{query}\n\nRetrieved context:\n{context_block}\n\nAnswer:"
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def generate_answer(
    query: str,
    contexts: list[dict[str, Any]],
    *,
    image_contexts: list[dict[str, Any]] | None = None,
    model: str = DEFAULT_LLM_MODEL,
    temperature: float = 0.2,
    max_tokens: int = 2048,
) -> str:
    """Generate an answer using the retrieved Jewelry RAG context."""
    response = get_llm_client().chat.completions.create(
        model=model,
        messages=build_rag_messages(query, contexts, image_contexts),
        temperature=temperature,
        max_tokens=max_tokens,
    )
    answer = response.choices[0].message.content
    if not answer:
        raise RuntimeError("Gemini returned an empty answer.")
    return answer.strip()
