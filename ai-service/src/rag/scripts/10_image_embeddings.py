"""Create rerun-safe VLM-description embeddings for document images.

The image pixels, PDF filename, and OCR text are not embedded. Each vector
represents only the VLM-generated visual description.

The image path and metadata remain linked to the vector so retrieval can
return the original extracted image.

Run:
    py src/rag/scripts/10_image_embeddings.py --device cuda
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import re
from pathlib import Path
from time import perf_counter
from typing import Any

import numpy as np
import torch
from sentence_transformers import SentenceTransformer


PROJECT_ROOT = Path(__file__).resolve().parents[3]

DEFAULT_PROCESSED_ROOT = (
    PROJECT_ROOT / "data" / "rag" / "processed_documents"
)

DEFAULT_OUTPUT_ROOT = (
    PROJECT_ROOT / "data" / "rag" / "image_embeddings"
)
DEFAULT_GEMINI_CACHE_ROOT = (
    PROJECT_ROOT / "data" / "rag" / "gemini_embedding_cache"
)

DEFAULT_CONTEXT_MODEL = "gemini-embedding-2"
DEFAULT_CONTEXT_BATCH_SIZE = 1
DEFAULT_MAX_SEQUENCE_LENGTH = 512
DEFAULT_STAGE_OUTPUT_ROOT = DEFAULT_OUTPUT_ROOT / "stages"

EMBEDDING_SCHEMA_VERSION = 5
STAGE_EMBEDDING_SCHEMA_VERSION = 2


class GeminiQueryEmbeddingAdapter:
    """SentenceTransformer-like query adapter backed by the Gemini API cache."""

    def __init__(self, model_name: str, *, cache_root: str | Path | None = None,
                 env_file: str | Path | None = None):
        self.model_name = model_name
        self.cache_root = Path(cache_root or DEFAULT_OUTPUT_ROOT / "queries")
        self.env_file = Path(env_file or PROJECT_ROOT / ".env")
        self.embedding_module = _load_embedding_module()
        self.client = self.embedding_module.create_gemini_client(self.env_file)

    def encode(self, sentences, *, normalize_embeddings=True,
               convert_to_numpy=True, show_progress_bar=False, **kwargs):
        result = self.embedding_module.get_or_create_gemini_embeddings(
            list(sentences),
            client=self.client,
            text_type="query",
            model_name=self.model_name,
            cache_root=self.cache_root,
            batch_size=DEFAULT_CONTEXT_BATCH_SIZE,
        )
        return result["embeddings"]


def load_image_query_model(
    model_name: str = DEFAULT_CONTEXT_MODEL,
    *,
    cache_root: str | Path | None = None,
    env_file: str | Path | None = None,
    device: str | None = None,
    local_files_only: bool = True,
    max_sequence_length: int = DEFAULT_MAX_SEQUENCE_LENGTH,
    model_kwargs: dict[str, Any] | None = None,
):
    """Load the configured image query embedder (Gemini API or local model)."""
    if model_name.startswith("gemini-embedding-"):
        return GeminiQueryEmbeddingAdapter(
            model_name, cache_root=cache_root, env_file=env_file
        )
    model = SentenceTransformer(
        model_name,
        device=device,
        local_files_only=local_files_only,
        model_kwargs=model_kwargs,
    )
    model.max_seq_length = max_sequence_length
    return model


def _load_embedding_module():
    import importlib

    return importlib.import_module("scripts.5_embeddings")


def _embed_image_texts(
    texts: list[str],
    *,
    model_name: str,
    text_type: str,
    batch_size: int,
    cache_root: Path,
    device: str | None,
    local_files_only: bool,
    max_sequence_length: int,
    force: bool,
) -> tuple[np.ndarray, float, str]:
    if model_name.startswith("gemini-embedding-"):
        embedding_module = _load_embedding_module()
        client = embedding_module.create_gemini_client(PROJECT_ROOT / ".env")
        result = embedding_module.get_or_create_gemini_embeddings(
            texts,
            client=client,
            text_type=text_type,
            model_name=model_name,
            cache_root=cache_root,
            batch_size=batch_size,
            force=force,
        )
        return result["embeddings"], result["elapsed_seconds"], "gemini_api"

    resolved_device = device or ("cuda" if torch.cuda.is_available() else "cpu")
    model_kwargs = {"torch_dtype": torch.float16} if resolved_device == "cuda" else None
    model = SentenceTransformer(
        model_name,
        device=resolved_device,
        local_files_only=local_files_only,
        model_kwargs=model_kwargs,
    )
    try:
        model.max_seq_length = max_sequence_length
        prepared = [
            f"passage: {text}" if "e5" in model_name.casefold() else text
            for text in texts
        ] if text_type == "passage" else [
            f"query: {text}" if "e5" in model_name.casefold() else text
            for text in texts
        ]
        started = perf_counter()
        embeddings = np.asarray(
            model.encode(
                prepared,
                batch_size=batch_size,
                show_progress_bar=True,
                normalize_embeddings=True,
                convert_to_numpy=True,
            ),
            dtype=np.float32,
        )
        elapsed = perf_counter() - started
    finally:
        del model
        _release_memory()
    return embeddings, elapsed, resolved_device


def load_image_records(
    processed_root: str | Path = DEFAULT_PROCESSED_ROOT,
) -> list[dict[str, Any]]:
    """Load image records from image_context.json (no OCR required)."""

    import importlib

    image_context_module = importlib.import_module(
        "scripts.8_image_context"
    )
    records = image_context_module.load_image_context_records(
        processed_root
    )

    missing_images = [
        record["image_path"]
        for record in records
        if not Path(record["image_path"]).is_file()
    ]

    if missing_images:
        raise FileNotFoundError(
            f"{len(missing_images)} extracted image files are missing."
        )

    return records


def build_image_context_text(record: dict[str, Any]) -> str:
    """Return only the VLM description to embed for this image."""
    description = _clean_text(record.get("visual_description", ""))
    if not description:
        raise ValueError(f"Image {record.get('image_id')} has no VLM description.")
    return description


def build_image_stage_texts(record: dict[str, Any]) -> dict[str, str]:
    """Use the PDF filename for stage one and its VLM description for stage two."""
    file_name = str(record.get("file_name", "")).strip()
    description = _clean_text(record.get("visual_description", ""))
    if not file_name and not description:
        raise ValueError(f"Image {record.get('image_id')} has no PDF name or VLM description.")
    stage1_text = f"PDF: {file_name}" if file_name else description
    stage2_text = f"Visual description: {description}" if description else ""
    return {
        "stage1_text": stage1_text,
        "stage2_text": stage2_text,
        "stage1_source": "pdf_name" if file_name else "vlm",
        "stage2_source": "vlm" if description else "none",
    }

def get_or_create_image_stage_embeddings(
    records: list[dict[str, Any]],
    *,
    output_root: str | Path = DEFAULT_STAGE_OUTPUT_ROOT,
    context_model_name: str = DEFAULT_CONTEXT_MODEL,
    device: str | None = None,
    context_batch_size: int = DEFAULT_CONTEXT_BATCH_SIZE,
    max_sequence_length: int = DEFAULT_MAX_SEQUENCE_LENGTH,
    local_files_only: bool = True,
    force: bool = False,
) -> dict[str, Any]:
    """Cache stage-1 / stage-2 embeddings for retrieve-and-rerank image search."""

    _validate_records(records)

    if context_batch_size <= 0:
        raise ValueError("context_batch_size must be positive.")
    if max_sequence_length <= 0:
        raise ValueError("max_sequence_length must be positive.")

    output_root = Path(output_root).resolve()
    artifact_directory = output_root / _safe_model_name(context_model_name)
    artifact_directory.mkdir(parents=True, exist_ok=True)

    stage1_file = artifact_directory / "stage1_embeddings.npy"
    stage2_file = artifact_directory / "stage2_embeddings.npy"
    metadata_file = artifact_directory / "images.json"
    manifest_file = artifact_directory / "manifest.json"

    stage_payloads = [build_image_stage_texts(record) for record in records]
    fingerprint_source = "\n".join(
        f"{record['image_id']}\t{payload['stage1_source']}\t"
        f"{payload['stage1_text']}\t{payload['stage2_source']}\t"
        f"{payload['stage2_text']}"
        for record, payload in zip(records, stage_payloads)
    )
    records_fingerprint = hashlib.sha256(
        fingerprint_source.encode("utf-8")
    ).hexdigest()

    expected_manifest = {
        "schema_version": STAGE_EMBEDDING_SCHEMA_VERSION,
        "embedding_type": "image_retrieve_rerank_stages",
        "context_model_name": context_model_name,
        "normalized": True,
        "max_sequence_length": max_sequence_length,
        "image_count": len(records),
        "records_fingerprint": records_fingerprint,
    }

    if not force:
        cached = _load_valid_stage_cache(
            stage1_file=stage1_file,
            stage2_file=stage2_file,
            metadata_file=metadata_file,
            manifest_file=manifest_file,
            expected_manifest=expected_manifest,
            records=records,
        )
        if cached is not None:
            return cached

    stage1_texts = [payload["stage1_text"] for payload in stage_payloads]
    stage2_texts = [
        payload["stage2_text"] or payload["stage1_text"]
        for payload in stage_payloads
    ]
    stage_cache_root = DEFAULT_GEMINI_CACHE_ROOT
    stage1_embeddings, stage1_seconds, resolved_device = _embed_image_texts(
        stage1_texts,
        model_name=context_model_name,
        text_type="passage",
        batch_size=context_batch_size,
        cache_root=stage_cache_root,
        device=device,
        local_files_only=local_files_only,
        max_sequence_length=max_sequence_length,
        force=force,
    )
    stage2_embeddings, stage2_seconds, _ = _embed_image_texts(
        stage2_texts,
        model_name=context_model_name,
        text_type="passage",
        batch_size=context_batch_size,
        cache_root=stage_cache_root,
        device=device,
        local_files_only=local_files_only,
        max_sequence_length=max_sequence_length,
        force=force,
    )
    elapsed_seconds = stage1_seconds + stage2_seconds
    _validate_embeddings(stage1_embeddings, expected_rows=len(records))
    _validate_embeddings(stage2_embeddings, expected_rows=len(records))

    metadata_records = []
    for position, (record, payload) in enumerate(
        zip(records, stage_payloads)
    ):
        metadata = dict(record)
        metadata["embedding_position"] = position
        metadata.update(payload)
        metadata["context_text"] = build_image_context_text(
            record,
        )
        metadata_records.append(metadata)

    manifest = {
        **expected_manifest,
        "device": resolved_device,
        "context_dimension": int(stage1_embeddings.shape[1]),
        "context_dtype": str(stage1_embeddings.dtype),
        "context_batch_size": context_batch_size,
        "elapsed_seconds": round(elapsed_seconds, 4),
    }
    metadata_payload = {
        "schema_version": STAGE_EMBEDDING_SCHEMA_VERSION,
        "embedding_type": "image_retrieve_rerank_stages",
        "images": metadata_records,
    }

    _write_npy_atomic(stage1_file, stage1_embeddings)
    _write_npy_atomic(stage2_file, stage2_embeddings)
    _write_json_atomic(metadata_file, metadata_payload)
    _write_json_atomic(manifest_file, manifest)

    return {
        "records": records,
        "stage1_embeddings": stage1_embeddings,
        "stage2_embeddings": stage2_embeddings,
        "metadata": metadata_payload,
        "manifest": manifest,
        "artifact_directory": artifact_directory,
        "stage1_embeddings_file": stage1_file,
        "stage2_embeddings_file": stage2_file,
        "metadata_file": metadata_file,
        "manifest_file": manifest_file,
        "cache_hit": False,
    }


def _load_valid_stage_cache(
    *,
    stage1_file: Path,
    stage2_file: Path,
    metadata_file: Path,
    manifest_file: Path,
    expected_manifest: dict[str, Any],
    records: list[dict[str, Any]],
) -> dict[str, Any] | None:
    manifest = _read_json(manifest_file)
    metadata = _read_json(metadata_file)

    if (
        not manifest
        or not metadata
        or not stage1_file.is_file()
        or not stage2_file.is_file()
    ):
        return None

    for key, value in expected_manifest.items():
        if manifest.get(key) != value:
            return None

    stage1 = np.load(stage1_file)
    stage2 = np.load(stage2_file)
    try:
        _validate_embeddings(stage1, expected_rows=len(records))
        _validate_embeddings(stage2, expected_rows=len(records))
        _validate_records(records)
    except ValueError:
        return None

    cached_ids = [
        image.get("image_id") for image in metadata.get("images", [])
    ]
    expected_ids = [record["image_id"] for record in records]
    if cached_ids != expected_ids:
        return None

    return {
        "records": records,
        "stage1_embeddings": np.asarray(stage1, dtype=np.float32),
        "stage2_embeddings": np.asarray(stage2, dtype=np.float32),
        "metadata": metadata,
        "manifest": manifest,
        "artifact_directory": stage1_file.parent,
        "stage1_embeddings_file": stage1_file,
        "stage2_embeddings_file": stage2_file,
        "metadata_file": metadata_file,
        "manifest_file": manifest_file,
        "cache_hit": True,
    }


def get_or_create_image_embeddings(
    records: list[dict[str, Any]],
    *,
    output_root: str | Path = DEFAULT_OUTPUT_ROOT,
    context_model_name: str = DEFAULT_CONTEXT_MODEL,
    device: str | None = None,
    context_batch_size: int = DEFAULT_CONTEXT_BATCH_SIZE,
    max_sequence_length: int = DEFAULT_MAX_SEQUENCE_LENGTH,
    local_files_only: bool = True,
    force: bool = False,
) -> dict[str, Any]:
    """Load cached context embeddings or generate them."""

    _validate_records(records)

    if context_batch_size <= 0:
        raise ValueError(
            "context_batch_size must be positive."
        )

    if max_sequence_length <= 0:
        raise ValueError(
            "max_sequence_length must be positive."
        )

    output_root = Path(output_root).resolve()

    artifact_directory = (
        output_root / _safe_model_name(context_model_name)
    )

    artifact_directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    embeddings_file = (
        artifact_directory / "context_embeddings.npy"
    )

    metadata_file = artifact_directory / "images.json"
    manifest_file = artifact_directory / "manifest.json"

    records_fingerprint = _records_fingerprint(records)

    expected_manifest = {
        "schema_version": EMBEDDING_SCHEMA_VERSION,
        "embedding_type": "image_vlm_description",
        "context_model_name": context_model_name,
        "normalized": True,
        "max_sequence_length": max_sequence_length,
        "image_count": len(records),
        "records_fingerprint": records_fingerprint,
    }

    if not force:
        cached = _load_valid_cache(
            embeddings_file=embeddings_file,
            metadata_file=metadata_file,
            manifest_file=manifest_file,
            expected_manifest=expected_manifest,
            records=records,
        )

        if cached is not None:
            embeddings, metadata, manifest = cached

            return _build_result(
                records=records,
                embeddings=embeddings,
                metadata=metadata,
                manifest=manifest,
                artifact_directory=artifact_directory,
                cache_hit=True,
            )

    context_texts = [
        build_image_context_text(record)
        for record in records
    ]
    api_cache_root = DEFAULT_GEMINI_CACHE_ROOT
    embeddings, elapsed_seconds, resolved_device = _embed_image_texts(
        context_texts,
        model_name=context_model_name,
        text_type="passage",
        batch_size=context_batch_size,
        cache_root=api_cache_root,
        device=device,
        local_files_only=local_files_only,
        max_sequence_length=max_sequence_length,
        force=force,
    )
    _validate_embeddings(embeddings, expected_rows=len(records))

    metadata_records = []

    for position, record in enumerate(records):
        metadata = dict(record)

        metadata["embedding_position"] = position

        metadata["context_text"] = build_image_context_text(record)

        metadata_records.append(metadata)

    manifest = {
        **expected_manifest,
        "device": resolved_device,
        "context_dimension": int(embeddings.shape[1]),
        "context_dtype": str(embeddings.dtype),
        "context_batch_size": context_batch_size,
        "elapsed_seconds": round(elapsed_seconds, 4),
    }

    metadata_payload = {
        "schema_version": EMBEDDING_SCHEMA_VERSION,
        "embedding_type": "image_vlm_description",
        "images": metadata_records,
    }

    _write_npy_atomic(
        embeddings_file,
        embeddings,
    )

    _write_json_atomic(
        metadata_file,
        metadata_payload,
    )

    _write_json_atomic(
        manifest_file,
        manifest,
    )

    return _build_result(
        records=records,
        embeddings=embeddings,
        metadata=metadata_payload,
        manifest=manifest,
        artifact_directory=artifact_directory,
        cache_hit=False,
    )


def load_cached_image_embeddings(
    records: list[dict[str, Any]],
    *,
    output_root: str | Path = DEFAULT_OUTPUT_ROOT,
    context_model_name: str = DEFAULT_CONTEXT_MODEL,
    max_sequence_length: int = DEFAULT_MAX_SEQUENCE_LENGTH,
) -> dict[str, Any]:
    """Load exact cached VLM-description vectors without making API calls."""
    _validate_records(records)
    artifact_directory = Path(output_root).resolve() / _safe_model_name(context_model_name)
    embeddings_file = artifact_directory / "context_embeddings.npy"
    metadata_file = artifact_directory / "images.json"
    manifest_file = artifact_directory / "manifest.json"
    expected_manifest = {
        "schema_version": EMBEDDING_SCHEMA_VERSION,
        "embedding_type": "image_vlm_description",
        "context_model_name": context_model_name,
        "normalized": True,
        "max_sequence_length": max_sequence_length,
        "image_count": len(records),
        "records_fingerprint": _records_fingerprint(records),
    }
    cached = _load_valid_cache(
        embeddings_file=embeddings_file,
        metadata_file=metadata_file,
        manifest_file=manifest_file,
        expected_manifest=expected_manifest,
        records=records,
    )
    if cached is None:
        raise FileNotFoundError(
            "Matching cached image embeddings are missing or stale; no API request was made. "
            f"Expected them under {artifact_directory}. Run notebook 6 to create them."
        )
    embeddings, metadata, manifest = cached
    return _build_result(
        records=records,
        embeddings=embeddings,
        metadata=metadata,
        manifest=manifest,
        artifact_directory=artifact_directory,
        cache_hit=True,
    )



def get_or_create_incremental_image_embeddings(
    records: list[dict[str, Any]],
    *,
    output_root: str | Path,
    context_model_name: str = DEFAULT_CONTEXT_MODEL,
    cache_root: str | Path | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Embed VLM descriptions per source PDF, reusing stable document caches.

    Existing full-corpus image caches are split into per-PDF cache entries on the
    first run. Later runs only embed a PDF whose visual-description batch changed.
    The vectors continue to represent only the VLM descriptions.
    """
    _validate_records(records)
    import importlib
    embedding_module = importlib.import_module("scripts.5_embeddings")
    output_root = Path(output_root).resolve()
    cache_root = Path(cache_root or DEFAULT_GEMINI_CACHE_ROOT / "per_document_images").resolve()
    artifact_directory = output_root / _safe_model_name(context_model_name)
    artifact_directory.mkdir(parents=True, exist_ok=True)

    context_texts = [build_image_context_text(record) for record in records]
    seeded_by_id: dict[str, np.ndarray] = {}
    if not force:
        try:
            previous = load_cached_image_embeddings(
                records,
                output_root=output_root,
                context_model_name=context_model_name,
            )
            seeded_by_id = {
                str(record["image_id"]): previous["context_embeddings"][position]
                for position, record in enumerate(records)
            }
        except (FileNotFoundError, ValueError):
            # A new/changed PDF changes the aggregate fingerprint. Reuse vectors
            # from individual unchanged records when they are still available.
            saved_metadata = _read_json(artifact_directory / "images.json") or {}
            saved_manifest = _read_json(artifact_directory / "manifest.json") or {}
            saved_file = artifact_directory / "context_embeddings.npy"
            saved_records = saved_metadata.get("images", [])
            if (
                saved_manifest.get("context_model_name") == context_model_name
                and saved_manifest.get("embedding_type") == "image_vlm_description"
                and saved_file.is_file()
            ):
                try:
                    saved_vectors = np.load(saved_file, allow_pickle=False)
                    _validate_embeddings(saved_vectors, expected_rows=len(saved_records))
                    for position, saved in enumerate(saved_records):
                        image_id = str(saved.get("image_id") or "")
                        current_position = next(
                            (i for i, item in enumerate(records) if str(item["image_id"]) == image_id),
                            None,
                        )
                        if current_position is not None and str(saved.get("context_text") or "") == context_texts[current_position]:
                            seeded_by_id[image_id] = saved_vectors[position]
                except (OSError, ValueError):
                    seeded_by_id = {}

    grouped_positions: dict[str, list[int]] = {}
    for position, record in enumerate(records):
        group = str(record.get("source_path") or record.get("file_name") or "")
        grouped_positions.setdefault(group, []).append(position)

    result_vectors = np.empty((len(records), 0), dtype=np.float32)
    cache_hits = 0
    api_documents: list[str] = []
    elapsed_seconds = 0.0
    gemini_client = None
    for source_path, positions in grouped_positions.items():
        group_texts = [context_texts[position] for position in positions]
        try:
            cached = embedding_module.load_cached_gemini_embeddings(
                group_texts,
                text_type="passage",
                model_name=context_model_name,
                cache_root=cache_root,
            )
            group_vectors = cached["embeddings"]
            cache_hits += 1
        except (FileNotFoundError, ValueError):
            image_ids = [str(records[position]["image_id"]) for position in positions]
            if not force and all(image_id in seeded_by_id for image_id in image_ids):
                group_vectors = np.stack([seeded_by_id[image_id] for image_id in image_ids]).astype(np.float32)
                embedding_module.store_cached_gemini_embeddings(
                    group_texts, group_vectors, text_type="passage",
                    model_name=context_model_name, cache_root=cache_root,
                )
                cache_hits += 1
            else:
                if gemini_client is None:
                    gemini_client = embedding_module.create_gemini_client(PROJECT_ROOT / ".env")
                embedded = embedding_module.get_or_create_gemini_embeddings(
                    group_texts, client=gemini_client, text_type="passage",
                    model_name=context_model_name, cache_root=cache_root,
                    batch_size=DEFAULT_CONTEXT_BATCH_SIZE, force=force,
                )
                group_vectors = embedded["embeddings"]
                elapsed_seconds += float(embedded.get("elapsed_seconds", 0.0))
                api_documents.append(str(records[positions[0]].get("file_name") or source_path))
        if result_vectors.shape[1] == 0:
            result_vectors = np.zeros((len(records), group_vectors.shape[1]), dtype=np.float32)
        if group_vectors.shape[1] != result_vectors.shape[1]:
            raise ValueError("Image embedding dimensions differ between document caches.")
        result_vectors[positions] = group_vectors

    _validate_embeddings(result_vectors, expected_rows=len(records))
    metadata_records = []
    for position, record in enumerate(records):
        metadata = dict(record)
        metadata["embedding_position"] = position
        metadata["context_text"] = context_texts[position]
        metadata_records.append(metadata)
    manifest = {
        "schema_version": EMBEDDING_SCHEMA_VERSION,
        "embedding_type": "image_vlm_description",
        "context_model_name": context_model_name,
        "normalized": True,
        "max_sequence_length": DEFAULT_MAX_SEQUENCE_LENGTH,
        "image_count": len(records),
        "records_fingerprint": _records_fingerprint(records),
        "device": "gemini_api",
        "context_dimension": int(result_vectors.shape[1]),
        "context_dtype": str(result_vectors.dtype),
        "context_batch_size": DEFAULT_CONTEXT_BATCH_SIZE,
        "elapsed_seconds": round(elapsed_seconds, 4),
    }
    metadata_payload = {
        "schema_version": EMBEDDING_SCHEMA_VERSION,
        "embedding_type": "image_vlm_description",
        "images": metadata_records,
    }
    _write_npy_atomic(artifact_directory / "context_embeddings.npy", result_vectors)
    _write_json_atomic(artifact_directory / "images.json", metadata_payload)
    _write_json_atomic(artifact_directory / "manifest.json", manifest)
    result = _build_result(
        records=records,
        embeddings=result_vectors,
        metadata=metadata_payload,
        manifest=manifest,
        artifact_directory=artifact_directory,
        cache_hit=not api_documents,
    )
    result["document_cache_hits"] = cache_hits
    result["api_documents"] = api_documents
    return result


def encode_image_query(
    query: str,
    *,
    model: SentenceTransformer,
    context_model_name: str = DEFAULT_CONTEXT_MODEL,
) -> np.ndarray:
    """Embed an image-retrieval query using the selected text model."""

    query = _clean_text(query)

    if not query:
        raise ValueError("The query cannot be empty.")

    prepared_query = (
        f"query: {query}"
        if "e5" in context_model_name.casefold()
        else query
    )

    embedding = np.asarray(
        model.encode(
            [prepared_query],
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        ),
        dtype=np.float32,
    )

    _validate_embeddings(
        embedding,
        expected_rows=1,
    )

    return embedding[0]


def _load_valid_cache(
    *,
    embeddings_file: Path,
    metadata_file: Path,
    manifest_file: Path,
    expected_manifest: dict[str, Any],
    records: list[dict[str, Any]],
):
    manifest = _read_json(manifest_file)
    metadata = _read_json(metadata_file)

    if (
        not manifest
        or not metadata
        or not embeddings_file.is_file()
    ):
        return None

    if not all(
        manifest.get(key) == value
        for key, value in expected_manifest.items()
    ):
        return None

    saved_images = metadata.get("images", [])

    saved_ids = [
        image.get("image_id")
        for image in saved_images
    ]

    expected_ids = [
        record["image_id"]
        for record in records
    ]

    if saved_ids != expected_ids:
        return None

    try:
        embeddings = np.load(
            embeddings_file,
            allow_pickle=False,
        )

        _validate_embeddings(
            embeddings,
            expected_rows=len(records),
        )

    except (OSError, ValueError):
        return None

    if (
        manifest.get("context_dimension")
        != embeddings.shape[1]
    ):
        return None

    return embeddings, metadata, manifest


def _records_fingerprint(records: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for record in records:
        values = [
            record["image_id"],
            record.get("digest", record.get("image_sha256", "")),
            record.get("file_name", ""),
            record.get("visual_description", ""),
        ]
        for value in values:
            digest.update(str(value).encode("utf-8"))
            digest.update(b"\0")
    return digest.hexdigest()


def _validate_records(records: list[dict[str, Any]]) -> None:
    if not isinstance(records, list):
        raise TypeError("records must be a list.")
    if not records:
        raise ValueError("records cannot be empty.")
    required_fields = {"image_id", "image_path", "file_name", "page_numbers", "visual_description"}
    image_ids = []
    for record in records:
        missing = required_fields - set(record)
        if missing:
            raise ValueError("Image record is missing fields: " + ", ".join(sorted(missing)))
        image_ids.append(record["image_id"])
        if not str(record.get("visual_description") or "").strip():
            raise ValueError(
                f"Image {record['image_id']} has no VLM description. "
                "Run scripts/8_image_context.py first."
            )
    if len(image_ids) != len(set(image_ids)):
        raise ValueError("Image IDs must be unique.")

def _validate_embeddings(
    embeddings: np.ndarray,
    *,
    expected_rows: int,
) -> None:
    if embeddings.ndim != 2:
        raise ValueError(
            f"Embeddings must be 2D: {embeddings.shape}"
        )

    if embeddings.shape[0] != expected_rows:
        raise ValueError(
            "Embedding row count does not match image count."
        )

    if embeddings.shape[1] == 0:
        raise ValueError(
            "Embedding dimension cannot be empty."
        )

    if not np.isfinite(embeddings).all():
        raise ValueError(
            "Embeddings contain NaN or infinite values."
        )


def _build_result(
    *,
    records: list[dict[str, Any]],
    embeddings: np.ndarray,
    metadata: dict[str, Any],
    manifest: dict[str, Any],
    artifact_directory: Path,
    cache_hit: bool,
) -> dict[str, Any]:
    return {
        "records": records,
        "context_embeddings": embeddings,
        "metadata": metadata,
        "manifest": manifest,
        "artifact_directory": artifact_directory,
        "context_embeddings_file": (
            artifact_directory / "context_embeddings.npy"
        ),
        "metadata_file": artifact_directory / "images.json",
        "manifest_file": artifact_directory / "manifest.json",
        "cache_hit": cache_hit,
    }


def _document_label(file_name: str) -> str:
    stem = Path(str(file_name)).stem
    label = re.sub(r"[_\-]+", " ", stem)
    return re.sub(r"\s+", " ", label).strip()


def _clean_text(text: Any) -> str:
    text = str(text or "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _safe_model_name(model_name: str) -> str:
    safe = re.sub(
        r"[^a-zA-Z0-9._-]+",
        "__",
        model_name,
    ).strip("._")

    return safe or "model"


def _release_memory() -> None:
    gc.collect()

    if torch.cuda.is_available():
        torch.cuda.empty_cache()

        try:
            torch.cuda.ipc_collect()
        except RuntimeError:
            pass


def _read_required_json(path: Path) -> dict[str, Any]:
    payload = _read_json(path)

    if payload is None:
        raise FileNotFoundError(
            f"Required JSON file is missing or invalid: {path}"
        )

    return payload


def _read_json(path: Path) -> dict[str, Any] | None:
    if not Path(path).is_file():
        return None

    try:
        with Path(path).open(
            encoding="utf-8"
        ) as file:
            return json.load(file)

    except (OSError, json.JSONDecodeError):
        return None


def _write_json_atomic(
    path: Path,
    payload: dict[str, Any],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    temporary = path.with_suffix(
        path.suffix + ".tmp"
    )

    with temporary.open(
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(
            payload,
            file,
            ensure_ascii=False,
            indent=2,
        )

    temporary.replace(path)


def _write_npy_atomic(
    path: Path,
    embeddings: np.ndarray,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    temporary = path.with_suffix(
        path.suffix + ".tmp"
    )

    with temporary.open("wb") as file:
        np.save(
            file,
            embeddings,
            allow_pickle=False,
        )

    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Create cached textual context embeddings "
            "for extracted document images."
        )
    )

    parser.add_argument(
        "--processed-root",
        type=Path,
        default=DEFAULT_PROCESSED_ROOT,
    )

    parser.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
    )

    parser.add_argument(
        "--device",
        default=None,
        help="Embedding device, such as cuda or cpu.",
    )

    parser.add_argument(
        "--context-batch-size",
        type=int,
        default=DEFAULT_CONTEXT_BATCH_SIZE,
    )

    parser.add_argument(
        "--stages",
        action="store_true",
        help="Also build cached retrieve-and-rerank stage embeddings.",
    )

    parser.add_argument(
        "--allow-downloads",
        action="store_true",
        help=(
            "Allow model downloads when the selected "
            "model is not already cached."
        ),
    )

    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Regenerate embeddings instead of using "
            "a valid cache."
        ),
    )

    args = parser.parse_args()

    records = load_image_records(
        args.processed_root
    )

    result = get_or_create_image_embeddings(
        records,
        output_root=args.output_root,
        device=args.device,
        context_batch_size=args.context_batch_size,
        local_files_only=not args.allow_downloads,
        force=args.force,
    )

    print("\nImage context embeddings complete")
    print("Images:", len(records))
    print(
        "Model:",
        result["manifest"]["context_model_name"],
    )
    print(
        "Embedding shape:",
        result["context_embeddings"].shape,
    )
    print("Embedded text: VLM visual descriptions only")
    print("Cache hit:", result["cache_hit"])
    print("Artifacts:", result["artifact_directory"])

    if args.stages:
        stage_result = get_or_create_image_stage_embeddings(
            records,
            device=args.device,
            context_batch_size=args.context_batch_size,
            local_files_only=not args.allow_downloads,
            force=args.force,
        )
        print("\nImage stage embeddings complete")
        print("Cache hit:", stage_result["cache_hit"])
        print("Artifacts:", stage_result["artifact_directory"])


if __name__ == "__main__":
    main()
    
