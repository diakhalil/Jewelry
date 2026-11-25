"""Offline document retrieval for the SheCraft chat service."""

from __future__ import annotations

import importlib
import logging
import os
import re
from pathlib import Path

import numpy as np
from qdrant_client import QdrantClient, models
from sklearn.feature_extraction.text import ENGLISH_STOP_WORDS, TfidfVectorizer


logger = logging.getLogger("uvicorn.error.shecraft")
extract_pdf = importlib.import_module("doc_rag.2_extraction").extract_pdf
chunk_documents = importlib.import_module("doc_rag.4_chunking").chunk_extracted_documents
documents_dir = Path(__file__).resolve().parents[3] / "rag_documents"
# Preserve "May" for birthstone queries. Ignore generic mentions of jewelry.
stop_words = (ENGLISH_STOP_WORDS - {"may"}) | {"jewelry", "jewellery"}


class LocalRagService:
    """Index the repository's PDF passages in an in-process Qdrant collection."""

    text_collection = "shecraft_local_jewelry"
    image_by_id: dict = {}

    def __init__(self):
        paths = sorted(documents_dir.rglob("*.pdf"))
        logger.info("local_index.started pdfs=%s", len(paths))
        documents = []
        for file in paths:
            try:
                document = extract_pdf(file, use_ocr=False, preserve_pdf_order=True)
            except (OSError, RuntimeError, ValueError) as exc:
                logger.warning("local_index.skipped file=%s reason=%s", file.name, type(exc).__name__)
                continue
            documents.append(document)

        rows = chunk_documents(documents, strategy="recursive", chunk_size=550, overlap=60)
        rows = rows[rows["word_count"] >= 12]
        if rows.empty:
            raise RuntimeError("No readable jewelry PDF passages were found in ai-service/rag_documents.")

        self.vectorizer = TfidfVectorizer(
            stop_words=sorted(stop_words),
            ngram_range=(1, 2),
            sublinear_tf=True,
            dtype=np.float32,
        )
        self.tokenize = self.vectorizer.build_tokenizer()
        passages = rows[["file_name", "page_number", "text"]].to_dict(orient="records")
        logger.info("local_index.embedding passages=%s", len(passages))
        vectors = self.vectorizer.fit_transform(
            f"{Path(passage['file_name']).stem} {passage['text']}" for passage in passages
        )
        self.qdrant = QdrantClient(":memory:")
        self.qdrant.create_collection(
            self.text_collection,
            sparse_vectors_config={"text": models.SparseVectorParams()},
        )
        self.qdrant.upsert(self.text_collection, points=[
            models.PointStruct(id=index, vector={"text": self._vector(vectors[index])}, payload=passage)
            for index, passage in enumerate(passages)
        ])
        self.document_count = rows["file_name"].nunique()
        logger.info("local_index.ready documents=%s passages=%s", self.document_count, len(passages))

    @property
    def answer_mode(self) -> str:
        return "gemini" if os.getenv("GEMINI_API_KEY") else "document-excerpts"

    @staticmethod
    def _vector(row) -> models.SparseVector:
        return models.SparseVector(indices=row.indices.tolist(), values=row.data.tolist())

    def _terms(self, text):
        return set(self.tokenize(text.casefold())) - stop_words

    def _excerpt(self, passage, terms):
        lines = [line.strip() for line in passage.splitlines()]
        text = " ".join(line for line in lines if not line.startswith(("Courtesy:", "Photo:")))
        sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z])", text)
        complete = [sentence for sentence in sentences if sentence.endswith((".", "!", "?"))]
        if complete:
            sentences = complete
        position = max(range(len(sentences)), key=lambda i: len(self._terms(sentences[i]) & terms))
        excerpt = sentences[position]
        if len(excerpt) < 80 and position + 1 < len(sentences):
            excerpt += " " + sentences[position + 1]
        if len(excerpt) > 300:
            excerpt = excerpt[:300].rsplit(" ", 1)[0] + "…"
        return excerpt

    def ask(self, query, *, max_tokens=2048, public_base_url="", jewelry=True, request_id="-"):
        logger.info("request=%s step=retrieval.started mode=local-qdrant", request_id)
        encoded = self.vectorizer.transform([query])
        terms = self._terms(query)
        sources = []
        if encoded.nnz:
            hits = self.qdrant.query_points(
                self.text_collection,
                query=self._vector(encoded),
                using="text",
                limit=24,
                score_threshold=0.045,
                with_payload=True,
            ).points
            candidates = [
                (len(self._terms(hit.payload["text"]) & terms), hit.score, hit.payload)
                for hit in hits
            ]
            candidates.sort(key=lambda candidate: candidate[:2], reverse=True)
            minimum_overlap = min(len(terms), max(2, (len(terms) + 1) // 2))
            seen = set()
            for overlap, score, source in candidates:
                page = (source["file_name"], source["page_number"])
                if (overlap < minimum_overlap or page in seen or
                    overlap < candidates[0][0] or score < 0.65 * candidates[0][1]):
                    continue
                sources.append(source)
                seen.add(page)
                if len(sources) == 3:
                    break

        logger.info("request=%s step=retrieval.completed sources=%s", request_id, len(sources))
        if not sources:
            answer = "I found no close matches in the jewelry guides. Try a more specific question about sizing, gemstones, or care."
        elif self.answer_mode == "gemini":
            logger.info("request=%s step=generation.started mode=gemini", request_id)
            generation = importlib.import_module("doc_rag.11_generation")
            answer = generation.generate_answer(query, sources, max_tokens=max_tokens, jewelry=jewelry)
        else:
            logger.info("request=%s step=formatting.started mode=document-excerpts", request_id)
            sources = [{**source, "text": self._excerpt(source["text"], terms)} for source in sources]
            excerpts = [
                f"{source['text']} [T{index}]"
                for index, source in enumerate(sources, start=1)
            ]
            answer = "Relevant excerpts from the jewelry guides:\n\n" + "\n\n".join(excerpts)
        return {
            "answer": answer,
            "mode": f"local-{self.answer_mode}",
            "sources": sources,
            "image_sources": [],
            "cards": [],
            "public_base_url": public_base_url,
        }
