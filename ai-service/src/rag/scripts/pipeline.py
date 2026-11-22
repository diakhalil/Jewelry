"""Incremental Jewelry RAG pipeline: extract, chunk, embed, and index PDFs."""

import argparse
import gc
import hashlib
import importlib
import json
import os
import re
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[3]
RAG_SRC_DIR = PROJECT_ROOT / "src" / "rag"
if str(RAG_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(RAG_SRC_DIR))

loader_module = importlib.import_module("scripts.1_loader")
extraction_module = importlib.import_module("scripts.2_extraction")
chunking_module = importlib.import_module("scripts.4_chunking")
images_module = importlib.import_module("scripts.3_images")
embeddings_module = importlib.import_module("scripts.5_embeddings")
indexing_module = importlib.import_module("scripts.6_indexing")
image_context_module = importlib.import_module("scripts.8_image_context")
image_embeddings_module = importlib.import_module("scripts.10_image_embeddings")
vlm_module = importlib.import_module("scripts.vlm")

load_document = loader_module.load_document
extract_pdf = extraction_module.extract_pdf
chunk_extracted_documents = chunking_module.chunk_extracted_documents
extract_document_images = images_module.extract_document_images
get_or_create_chunk_embeddings = embeddings_module.get_or_create_chunk_embeddings
load_embedding_model = embeddings_module.load_embedding_model
index_text_chunks = indexing_module.index_text_chunks
index_image_contexts = indexing_module.index_image_contexts
build_document_image_contexts = image_context_module.build_document_image_contexts
load_image_records = image_embeddings_module.load_image_records

DEFAULT_DOCUMENTS_DIR = PROJECT_ROOT / "rag_documents"
DEFAULT_ARTIFACT_ROOT = PROJECT_ROOT / "data" / "rag" / "processed_documents"
DEFAULT_EMBEDDING_ROOT = PROJECT_ROOT / "data" / "rag" / "embeddings"
DEFAULT_IMAGE_EMBEDDING_ROOT = DEFAULT_EMBEDDING_ROOT / "image_vlm_descriptions"
DEFAULT_CHUNKING_STRATEGY = "semantic"
DEFAULT_CHUNKING_OPTIONS = {
    "similarity_threshold": 0.65,
    "max_chunk_size": 800,
    "unit_prefix": "passage: ",
}
PIPELINE_SCHEMA_VERSION = 1
IMAGE_SCHEMA_VERSION = 2
DEFAULT_EMBEDDING_MODEL = "gemini-embedding-2"
DEFAULT_CHUNKING_EMBEDDING_MODEL = "intfloat/multilingual-e5-base"
DEFAULT_QDRANT_URL = indexing_module.DEFAULT_QDRANT_URL
DEFAULT_TEXT_COLLECTION = indexing_module.DEFAULT_COLLECTION_NAME
DEFAULT_IMAGE_COLLECTION = indexing_module.IMAGE_COLLECTION_NAME


def discover_pdf_files(
    paths: str | Path | Iterable[str | Path],
    recursive: bool = False,
) -> list[Path]:
    """Resolve unique PDF files from file and directory inputs."""
    if isinstance(paths, (str, Path)):
        paths = [paths]

    discovered = []
    for raw_path in paths:
        path = Path(raw_path).resolve()
        if not path.exists():
            raise FileNotFoundError(f"Document input was not found: {path}")
        if path.is_file():
            if path.suffix.lower() != ".pdf":
                raise ValueError(f"Expected a PDF file, received: {path}")
            discovered.append(path)
        else:
            pattern = "**/*.pdf" if recursive else "*.pdf"
            discovered.extend(sorted(path.glob(pattern)))

    unique_files = []
    seen = set()
    for path in discovered:
        resolved = path.resolve()
        key = str(resolved).casefold()
        if key not in seen:
            seen.add(key)
            unique_files.append(resolved)

    if not unique_files:
        raise ValueError("No PDF documents were found in the supplied inputs.")
    return unique_files


def prepare_documents_for_rag(
    paths: str | Path | Iterable[str | Path],
    *,
    recursive: bool = False,
    artifact_root: str | Path = DEFAULT_ARTIFACT_ROOT,
    chunking_strategy: str = DEFAULT_CHUNKING_STRATEGY,
    chunking_options: dict[str, Any] | None = None,
    chunking_model: Any | None = None,
    extraction_options: dict[str, Any] | None = None,
    extract_images: bool = True,
    include_image_bytes: bool = False,
    force: bool = False,
    embedding_device: str | None = None,
    embedding_local_files_only: bool = False,
) -> dict[str, Any]:
    """Prepare PDFs, reusing valid text, chunk, and image artifacts."""
    pdf_files = discover_pdf_files(paths, recursive=recursive)
    artifact_root = Path(artifact_root).resolve()
    artifact_root.mkdir(parents=True, exist_ok=True)
    extraction_options = dict(extraction_options or {})
    chunking_options = (
        dict(DEFAULT_CHUNKING_OPTIONS)
        if chunking_options is None
        else dict(chunking_options)
    )
    # Never persist the embedding model object in chunk cache metadata.
    runtime_chunking_options = {
        key: value
        for key, value in chunking_options.items()
        if key != "model"
    }

    owns_chunking_model = False
    active_chunking_model = chunking_model or chunking_options.get("model")

    def get_chunking_model():
        nonlocal active_chunking_model, owns_chunking_model
        if active_chunking_model is None:
            active_chunking_model = load_embedding_model(
                DEFAULT_CHUNKING_EMBEDDING_MODEL,
                device=embedding_device,
                local_files_only=embedding_local_files_only,
            )
            owns_chunking_model = True
        return active_chunking_model

    document_infos = []
    extracted_documents = []
    extracted_images = []
    chunk_frames = []
    artifact_status = []

    try:
        for path in pdf_files:
            source_sha256 = _sha256_file(path)
            document_dir = _document_artifact_dir(path, artifact_root)
            document_dir.mkdir(parents=True, exist_ok=True)
            document_infos.append(load_document(path))

            document, text_cached = _load_or_extract_text(
                path,
                document_dir,
                source_sha256,
                extraction_options,
                force,
            )
            extracted_documents.append(document)

            chunks, chunks_cached = _load_or_create_chunks(
                document,
                document_dir,
                source_sha256,
                chunking_strategy,
                dict(runtime_chunking_options),
                force,
                model_factory=(
                    get_chunking_model if chunking_strategy == "semantic" else None
                ),
            )
            chunk_frames.append(chunks)

            images_cached = None
            if extract_images:
                image_result, images_cached = _load_or_extract_images(
                    path,
                    document_dir,
                    source_sha256,
                    include_image_bytes,
                    force,
                )
                extracted_images.append(image_result)

            artifact_status.append({
                "file_name": path.name,
                "source_path": str(path),
                "artifact_directory": str(document_dir),
                "text_cache_hit": text_cached,
                "chunks_cache_hit": chunks_cached,
                "images_cache_hit": images_cached,
            })
    finally:
        if owns_chunking_model:
            _release_embedding_memory()

    all_chunks = pd.concat(chunk_frames, ignore_index=True)
    _validate_pipeline_output(extracted_documents, all_chunks)

    return {
        "source_files": pdf_files,
        "document_infos": document_infos,
        "extracted_documents": extracted_documents,
        "extracted_images": extracted_images,
        "chunks": all_chunks,
        "chunking_strategy": chunking_strategy,
        "chunking_options": runtime_chunking_options,
        "artifact_root": artifact_root,
        "artifact_status": pd.DataFrame(artifact_status),
    }


def prepare_document_for_rag(path: str | Path, **kwargs: Any) -> dict[str, Any]:
    """Prepare one PDF with the same pipeline used for document batches."""
    return prepare_documents_for_rag(Path(path).resolve(), **kwargs)


def _load_or_extract_text(
    path, document_dir, source_sha256, extraction_options, force,
):
    cache_file = document_dir / "extracted_text.json"
    expected = {
        "schema_version": PIPELINE_SCHEMA_VERSION,
        "source_sha256": source_sha256,
        "extraction_options": extraction_options,
    }
    cached = None if force else _read_json(cache_file)
    if cached and _metadata_matches(cached, expected) and "document" in cached:
        return cached["document"], True

    document = extract_pdf(path, **extraction_options)
    _write_json_atomic(cache_file, {**expected, "document": document})
    return document, False


def _load_or_create_chunks(
    document, document_dir, source_sha256, strategy, options, force,
    model_factory=None,
):
    cache_file = document_dir / "chunks.json"
    cache_options = {
        key: value for key, value in options.items() if key != "model"
    }
    expected = {
        "schema_version": PIPELINE_SCHEMA_VERSION,
        "source_sha256": source_sha256,
        "chunking_strategy": strategy,
        "chunking_options": cache_options,
    }
    cached = None if force else _read_json(cache_file)
    if cached and _metadata_matches(cached, expected) and "chunks" in cached:
        return pd.DataFrame(cached["chunks"]), True

    if strategy == "semantic" and options.get("model") is None and model_factory:
        options["model"] = model_factory()
    chunks = chunk_extracted_documents([document], strategy=strategy, **options)
    _write_json_atomic(cache_file, {
        **expected,
        "chunks": chunks.to_dict(orient="records"),
    })
    return chunks, False


def _load_or_extract_images(
    path, document_dir, source_sha256, include_bytes, force,
):
    images_dir = document_dir / "images"
    manifest_file = images_dir / "manifest.json"
    expected = {
        "schema_version": IMAGE_SCHEMA_VERSION,
        "source_sha256": source_sha256,
    }
    manifest = None if force else _read_json(manifest_file)

    if manifest and _metadata_matches(manifest, expected):
        image_files_valid = all(
            (images_dir / image["stored_file_name"]).is_file()
            and (images_dir / image["stored_file_name"]).stat().st_size > 0
            for image in manifest.get("images", [])
        )
        if image_files_valid:
            return _materialize_image_result(
                manifest, images_dir, include_bytes
            ), True

    images_dir.mkdir(parents=True, exist_ok=True)
    extracted = extract_document_images(path, include_bytes=True)
    manifest_images = []

    for index, image in enumerate(extracted["images"], start=1):
        extension = re.sub(r"[^a-zA-Z0-9]", "", image["extension"]) or "bin"
        stored_name = (
            f"image_{index:04d}_{image['digest'][:16]}.{extension.lower()}"
        )
        _write_bytes_atomic(images_dir / stored_name, image["image_bytes"])
        metadata = {
            key: value
            for key, value in image.items()
            if key != "image_bytes"
        }
        metadata["stored_file_name"] = stored_name
        manifest_images.append(metadata)

    manifest = {
        **expected,
        "file_name": extracted["file_name"],
        "source_path": extracted["source_path"],
        "unique_image_count": len(manifest_images),
        "images": manifest_images,
        "unextractable_placements": extracted["unextractable_placements"],
        "unextractable_placement_count": extracted[
            "unextractable_placement_count"
        ],
    }
    _write_json_atomic(manifest_file, manifest)
    stored_names = {image["stored_file_name"] for image in manifest_images}
    for old_image_file in images_dir.glob("image_*"):
        if old_image_file.name not in stored_names and old_image_file.is_file():
            old_image_file.unlink()
    return _materialize_image_result(manifest, images_dir, include_bytes), False


def _materialize_image_result(manifest, images_dir, include_bytes):
    result = dict(manifest)
    result["manifest_path"] = str(images_dir / "manifest.json")
    result["images"] = []
    for stored in manifest.get("images", []):
        image = dict(stored)
        image_path = images_dir / image["stored_file_name"]
        image["image_path"] = str(image_path)
        if include_bytes:
            image["image_bytes"] = image_path.read_bytes()
        result["images"].append(image)
    return result


def _document_artifact_dir(path: Path, artifact_root: Path) -> Path:
    safe_name = re.sub(r"[^\w.-]+", "_", path.stem, flags=re.UNICODE).strip("._")
    safe_name = safe_name or "document"
    path_hash = hashlib.sha256(
        str(path.resolve()).casefold().encode("utf-8")
    ).hexdigest()[:8]
    return artifact_root / f"{safe_name}__{path_hash}"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _metadata_matches(payload, expected) -> bool:
    return all(payload.get(key) == value for key, value in expected.items())


def _read_json(path: Path):
    if not path.is_file():
        return None
    try:
        with path.open(encoding="utf-8") as file:
            return json.load(file)
    except (json.JSONDecodeError, OSError):
        return None


def _write_json_atomic(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)
    temporary.replace(path)


def _write_bytes_atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(content)
    temporary.replace(path)


def _validate_pipeline_output(extracted_documents, chunks) -> None:
    if chunks.empty:
        raise ValueError("The selected documents produced no text chunks.")
    if chunks["chunk_id"].duplicated().any():
        raise ValueError("Duplicate chunk IDs were generated.")
    if chunks["text"].str.strip().eq("").any():
        raise ValueError("At least one generated chunk has empty text.")
    if chunks["page_number"].lt(1).any():
        raise ValueError("At least one chunk has an invalid page number.")

    expected_sources = {
        str(Path(document["source_path"]).resolve()).casefold()
        for document in extracted_documents
        if any(page.get("final_text", "").strip() for page in document["pages"])
    }
    chunk_sources = {
        str(Path(value).resolve()).casefold()
        for value in chunks["source_path"].unique()
    }
    if expected_sources - chunk_sources:
        raise ValueError("At least one extracted document is missing chunks.")


def get_or_create_incremental_text_embeddings(
    chunks: pd.DataFrame,
    *,
    model_name: str = DEFAULT_EMBEDDING_MODEL,
    cache_root: str | Path = DEFAULT_EMBEDDING_ROOT,
    batch_size: int = 1,
    force: bool = False,
) -> dict[str, Any]:
    """Reuse Gemini vectors per PDF; only embed PDFs with a cache miss.

    On the first migration, seed per-PDF entries from notebook 5's complete
    Gemini cache using the saved chunk-ID order. Future additions then embed
    only new or changed PDF batches.
    """
    if not model_name.startswith("gemini-embedding-"):
        return get_or_create_chunk_embeddings(
            chunks,
            model_name=model_name,
            batch_size=batch_size,
            force=force,
        )
    if chunks.empty:
        raise ValueError("Cannot embed an empty chunk set.")

    chunks = chunks.reset_index(drop=True).copy()
    cache_root = Path(cache_root).resolve()
    document_cache_root = cache_root / "per_document_text"
    vectors = None
    seeded_by_chunk_id: dict[str, np.ndarray] = {}
    if not force:
        order_file = cache_root / "BAAI__bge-m3" / "chunks.json"
        if order_file.is_file():
            all_chunks = {}
            for chunk_file in sorted(DEFAULT_ARTIFACT_ROOT.glob("*/chunks.json")):
                data = _read_json(chunk_file) or {}
                all_chunks.update({str(row["chunk_id"]): row for row in data.get("chunks", [])})
            ordered_ids = json.loads(order_file.read_text(encoding="utf-8")).get("chunk_ids", [])
            if ordered_ids and all(chunk_id in all_chunks for chunk_id in ordered_ids):
                ordered_texts = [str(all_chunks[chunk_id]["text"]).strip() for chunk_id in ordered_ids]
                try:
                    old_pack = embeddings_module.load_cached_gemini_embeddings(
                        ordered_texts, text_type="passage", model_name=model_name,
                        cache_root=cache_root,
                    )
                    seeded_by_chunk_id = dict(zip(ordered_ids, old_pack["embeddings"]))
                except (FileNotFoundError, ValueError):
                    pass

    output_vectors = np.empty((len(chunks), 0), dtype=np.float32)
    client = None
    cache_hits = 0
    api_documents: list[str] = []
    source_groups = chunks.groupby(chunks["source_path"].astype(str), sort=False).indices
    for source_path, positions in source_groups.items():
        positions = np.asarray(positions, dtype=np.int64)
        group = chunks.iloc[positions]
        texts = group["text"].astype(str).str.strip().tolist()
        chunk_ids = group["chunk_id"].astype(str).tolist()
        try:
            pack = embeddings_module.load_cached_gemini_embeddings(
                texts, text_type="passage", model_name=model_name,
                cache_root=document_cache_root,
            )
            group_vectors = pack["embeddings"]
            cache_hits += 1
        except (FileNotFoundError, ValueError):
            if not force and all(chunk_id in seeded_by_chunk_id for chunk_id in chunk_ids):
                group_vectors = np.stack([seeded_by_chunk_id[chunk_id] for chunk_id in chunk_ids]).astype(np.float32)
                embeddings_module.store_cached_gemini_embeddings(
                    texts, group_vectors, text_type="passage", model_name=model_name,
                    cache_root=document_cache_root,
                )
                cache_hits += 1
            else:
                if client is None:
                    client = embeddings_module.create_gemini_client(PROJECT_ROOT / ".env")
                pack = embeddings_module.get_or_create_gemini_embeddings(
                    texts, client=client, text_type="passage", model_name=model_name,
                    cache_root=document_cache_root, batch_size=batch_size, force=force,
                )
                group_vectors = pack["embeddings"]
                api_documents.append(str(group.iloc[0]["file_name"]))
        if output_vectors.shape[1] == 0:
            output_vectors = np.zeros((len(chunks), group_vectors.shape[1]), dtype=np.float32)
        if group_vectors.shape[1] != output_vectors.shape[1]:
            raise ValueError("Text embedding dimensions differ between document caches.")
        output_vectors[positions] = group_vectors

    embeddings_module._validate_embeddings(output_vectors, expected_rows=len(chunks))
    return {
        "chunks": chunks,
        "embeddings": output_vectors,
        "manifest": {
            "model_name": model_name,
            "embedding_dimension": int(output_vectors.shape[1]),
            "chunk_count": len(chunks),
            "document_count": len(source_groups),
            "document_cache_hits": cache_hits,
            "api_documents": api_documents,
            "normalized": True,
        },
        "artifact_directory": document_cache_root,
        "embeddings_file": None,
        "cache_hit": not api_documents,
        "document_cache_hits": cache_hits,
        "api_documents": api_documents,
    }


def run_pipeline(
    paths: str | Path | Iterable[str | Path] | None = None,
    *,
    create_embeddings: bool = True,
    embedding_model: str = DEFAULT_EMBEDDING_MODEL,
    embedding_device: str | None = None,
    embedding_batch_size: int = 1,
    embedding_local_files_only: bool = False,
    force_embeddings: bool = False,
    create_index: bool = True,
    qdrant_url: str | None = DEFAULT_QDRANT_URL,
    qdrant_path: str | Path | None = None,
    qdrant_collection: str = DEFAULT_TEXT_COLLECTION,
    create_image_embeddings: bool = True,
    create_image_index: bool = True,
    build_image_contexts: bool = True,
    image_collection: str = DEFAULT_IMAGE_COLLECTION,
    **kwargs: Any,
) -> dict[str, Any]:
    """Incrementally process all supplied PDFs and persist text/image vectors."""
    qdrant_url = qdrant_url or DEFAULT_QDRANT_URL
    if paths is None:
        paths = DEFAULT_DOCUMENTS_DIR
    prepare_kwargs = dict(kwargs)
    prepare_kwargs.setdefault("embedding_device", embedding_device)
    prepare_kwargs.setdefault("embedding_local_files_only", embedding_local_files_only)
    prepare_kwargs.setdefault("extraction_options", {"use_ocr_fallback": True})
    recursive = bool(prepare_kwargs.pop("recursive", True))
    result = prepare_documents_for_rag(paths, recursive=recursive, **prepare_kwargs)
    result.update({
        "text_embeddings": None,
        "qdrant_index": None,
        "image_contexts": [],
        "image_embeddings": None,
        "image_qdrant_index": None,
    })

    if create_embeddings:
        try:
            result["text_embeddings"] = get_or_create_incremental_text_embeddings(
                result["chunks"],
                model_name=embedding_model,
                cache_root=DEFAULT_EMBEDDING_ROOT,
                batch_size=embedding_batch_size,
                force=force_embeddings,
            )
        finally:
            _release_embedding_memory()

    image_records: list[dict[str, Any]] = []
    if kwargs.get("extract_images", True) and create_image_embeddings and build_image_contexts:
        image_counts = {
            str(info["source_path"]).casefold(): int(info.get("unique_image_count", 0))
            for info in result["extracted_images"]
        }
        if any(image_counts.values()):
            vlm = _create_cache_compatible_vlm(result["artifact_status"])
            for status in result["artifact_status"].to_dict(orient="records"):
                if image_counts.get(str(status["source_path"]).casefold(), 0) == 0:
                    continue
                result["image_contexts"].append(
                    build_document_image_contexts(status["artifact_directory"], vlm=vlm,
                                                  force=bool(kwargs.get("force", False)))
                )
            current_sources = {str(path.resolve()).casefold() for path in result["source_files"]}
            image_records = [
                record for record in load_image_records(result["artifact_root"])
                if str(Path(record.get("source_path", "")).resolve()).casefold() in current_sources
            ]

    if image_records:
        try:
            result["image_embeddings"] = image_embeddings_module.get_or_create_incremental_image_embeddings(
                image_records,
                output_root=DEFAULT_IMAGE_EMBEDDING_ROOT,
                context_model_name=image_embeddings_module.DEFAULT_CONTEXT_MODEL,
                force=force_embeddings,
            )
        finally:
            _release_embedding_memory()

    qdrant_client = None
    try:
        if create_index and (
            (create_embeddings and result["text_embeddings"] is not None)
            or (create_image_index and result["image_embeddings"] is not None)
        ):
            qdrant_client = indexing_module.connect_to_qdrant(
                url=qdrant_url,
                path=qdrant_path if qdrant_url is None else None,
            )
        if qdrant_client is not None and create_embeddings:
            index_result = index_text_chunks(
                result["chunks"], result["text_embeddings"]["embeddings"],
                client=qdrant_client, url=qdrant_url or indexing_module.DEFAULT_QDRANT_URL,
                qdrant_path=qdrant_path, create_payload_indexes=qdrant_url is not None,
                collection_name=qdrant_collection,
            )
            index_result.pop("client", None)
            result["qdrant_index"] = index_result
        if qdrant_client is not None and create_image_index and result["image_embeddings"] is not None:
            image_result = index_image_contexts(
                result["image_embeddings"]["metadata"]["images"],
                result["image_embeddings"]["context_embeddings"],
                client=qdrant_client, url=qdrant_url or indexing_module.DEFAULT_QDRANT_URL,
                qdrant_path=qdrant_path, create_payload_indexes=qdrant_url is not None,
                collection_name=image_collection,
            )
            image_result.pop("client", None)
            result["image_qdrant_index"] = image_result
    finally:
        if qdrant_client is not None:
            qdrant_client.close()

    return result


def _create_cache_compatible_vlm(artifact_status: pd.DataFrame):
    """Reuse the VLM model behind existing descriptions unless explicitly configured."""
    configured_model = os.getenv("GEMINI_VLM_MODEL")
    backend = os.getenv("VLM_BACKEND", "gemini").strip().casefold()
    if backend == "gemini" and not configured_model:
        cached_models = set()
        for artifact_dir in artifact_status.get("artifact_directory", []):
            payload = _read_json(Path(artifact_dir) / "image_context.json") or {}
            if any(str(row.get("visual_description") or "").strip() for row in payload.get("images", [])):
                model = str(payload.get("vlm_model") or "").strip()
                if model:
                    cached_models.add(model)
        if len(cached_models) == 1:
            return vlm_module.GeminiVLM(api_model=next(iter(cached_models)))
    return vlm_module.create_vlm_from_environment()

def _release_embedding_memory() -> None:
    """Release unused Python and CUDA memory after the embedding stage."""
    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            torch.cuda.ipc_collect()
    except (ImportError, RuntimeError):
        pass


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Incrementally embed Jewelry PDFs and index text and images in persistent Qdrant."
    )
    parser.add_argument("paths", nargs="*", type=Path, help="PDF files/folders; defaults to rag_documents.")
    parser.add_argument("--force", action="store_true", help="Rebuild extraction, VLM descriptions, and embeddings for supplied PDFs.")
    parser.add_argument("--no-images", action="store_true", help="Skip image extraction, VLM descriptions, and image indexing.")
    parser.add_argument("--no-embeddings", action="store_true", help="Extract and chunk only; do not create text vectors.")
    parser.add_argument("--no-index", action="store_true", help="Create/reuse vectors without writing to Qdrant.")
    parser.add_argument("--device", default=None, help="Device for semantic chunking models, e.g. cuda or cpu.")
    parser.add_argument(
        "--qdrant-url",
        default=DEFAULT_QDRANT_URL,
        help="Qdrant server URL (or set QDRANT_URL in ai-service/.env).",
    )
    parser.add_argument("--collection", default=DEFAULT_TEXT_COLLECTION, help="Text-vector collection.")
    parser.add_argument("--image-collection", default=DEFAULT_IMAGE_COLLECTION, help="Image-vector collection.")
    args = parser.parse_args()
    source_paths = args.paths if args.paths else [DEFAULT_DOCUMENTS_DIR]
    result = run_pipeline(
        source_paths,
        recursive=True,
        force=args.force,
        extract_images=not args.no_images,
        create_embeddings=not args.no_embeddings,
        embedding_device=args.device,
        embedding_batch_size=1,
        force_embeddings=args.force,
        create_index=not args.no_index,
        qdrant_url=args.qdrant_url,
        qdrant_path=None,
        qdrant_collection=args.collection,
        image_collection=args.image_collection,
    )
    print("\nJewelry RAG pipeline complete")
    print("PDFs prepared:", len(result["source_files"]))
    print("Text chunks:", len(result["chunks"]))
    print("Text embedding model:", DEFAULT_EMBEDDING_MODEL if result["text_embeddings"] is not None else "skipped")
    if result["text_embeddings"] is not None:
        info = result["text_embeddings"]
        print("Text embedding cache hits:", info["document_cache_hits"], "documents; API-embedded:", info["api_documents"] or "none")
    print("Images described:", sum(int(item.get("described_image_count", 0)) for item in result["image_contexts"]))
    if result["image_embeddings"] is not None:
        info = result["image_embeddings"]
        print("Image embedding model:", info["manifest"]["context_model_name"])
        print("Image cache hits:", info["document_cache_hits"], "documents; API-embedded:", info["api_documents"] or "none")
    if result["qdrant_index"] is not None:
        print("Qdrant text collection:", result["qdrant_index"]["collection_name"])
        print("Text points uploaded:", result["qdrant_index"]["uploaded_count"], "unchanged PDFs:", result["qdrant_index"]["unchanged_document_count"])
        print("Total text points:", result["qdrant_index"]["total_count"])
    if result["image_qdrant_index"] is not None:
        print("Qdrant image collection:", result["image_qdrant_index"]["collection_name"])
        print("Image points uploaded:", result["image_qdrant_index"]["uploaded_count"], "unchanged PDFs:", result["image_qdrant_index"]["unchanged_document_count"])
        print("Total image points:", result["image_qdrant_index"]["total_count"])
    print("Qdrant server:", args.qdrant_url)
    print("\nPer-PDF extraction/chunk cache:")
    print(result["artifact_status"][
        ["file_name", "text_cache_hit", "chunks_cache_hit", "images_cache_hit"]
    ].to_string(index=False))


if __name__ == "__main__":
    main()
