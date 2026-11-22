"""Describe extracted theme-PDF images with a vision-language model.

Run directly from the project root:
    py src/rag/scripts/8_image_context.py

This stage does not perform OCR or create embeddings. It creates one
``image_context.json`` beside each processed theme document's artifacts.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path, PureWindowsPath

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
RAG_SRC_DIR = PROJECT_ROOT / "src" / "rag"
if str(RAG_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(RAG_SRC_DIR))

from scripts.vlm import create_vlm_from_environment


DEFAULT_DOCUMENTS_DIR = PROJECT_ROOT / "rag_documents"
DEFAULT_ARTIFACT_ROOT = PROJECT_ROOT / "data" / "rag" / "processed_documents"
CONTEXT_SCHEMA_VERSION = 4
DESCRIPTION_CACHE_SCHEMA_VERSION = 1
def resolve_extracted_image_path(
    record: dict,
    document_dir: str | Path,
) -> Path:
    """Resolve an extracted image without rewriting cached JSON.

    ``image_path`` is often a Windows absolute path. In Docker that string
    does not exist; the file still lives next to ``image_context.json``.
    """
    document_dir = Path(document_dir)
    stored = str(record.get("stored_file_name") or "").strip()
    raw = str(record.get("image_path") or "").strip()

    if not stored and raw:
        stored = PureWindowsPath(raw).name
        if not stored:
            stored = Path(raw.replace("\\", "/")).name

    candidates: list[Path] = []
    if stored:
        candidates.append(document_dir / "images" / stored)
        candidates.append(document_dir / stored)
    if raw:
        candidates.append(Path(raw))

    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        try:
            if candidate.is_file():
                return candidate.resolve()
        except OSError:
            continue

    if stored:
        return (document_dir / "images" / stored).resolve()
    if raw:
        return Path(raw)
    raise FileNotFoundError(
        f"No image path for record {record.get('image_id')}"
    )

def load_image_context_records(
    artifact_root: str | Path = DEFAULT_ARTIFACT_ROOT,
) -> list[dict]:
    """Load image_context.json records (no OCR required)."""
    artifact_root = Path(artifact_root).resolve()
    if not artifact_root.is_dir():
        raise FileNotFoundError(
            f"Processed-document directory not found: {artifact_root}"
        )

    context_files = sorted(artifact_root.glob("*/image_context.json"))
    if not context_files:
        raise ValueError(
            "No image_context.json files were found. "
            "Run 8_image_context.py or the pipeline image stage first."
        )

    records: list[dict] = []
    for context_file in context_files:
        payload = _read_required_json(context_file)
        for image in payload.get("images", []):
            record = dict(image)
            record["context_file"] = str(context_file.resolve())
            record["processed_document_directory"] = str(
                context_file.parent.resolve()
            )
            record["image_path"] = str(
                resolve_extracted_image_path(
                    record,
                    context_file.parent,
                )
            )
            records.append(record)

    if not records:
        raise ValueError("image_context.json files contain no image records.")

    image_ids = [record["image_id"] for record in records]
    if len(image_ids) != len(set(image_ids)):
        raise ValueError("Duplicate image IDs were found.")

    records.sort(
        key=lambda record: (
            str(record.get("file_name", "")).casefold(),
            min(record.get("page_numbers") or [0]),
            str(record["image_id"]),
        )
    )
    return records


def build_pdf_image_contexts(
    documents_dir: str | Path,
    *,
    artifact_root: str | Path = DEFAULT_ARTIFACT_ROOT,
    vlm=None,
    force: bool = False,
) -> dict:
    """Describe extracted PDF images and reuse per-image description caches."""
    documents_dir = Path(documents_dir).resolve()
    artifact_root = Path(artifact_root).resolve()
    if not documents_dir.is_dir():
        raise FileNotFoundError(f"PDF directory was not found: {documents_dir}")
    pdf_paths = sorted(documents_dir.rglob("*.pdf"))
    if not pdf_paths:
        raise ValueError(f"No PDF files were found in {documents_dir}.")
    artifact_dirs = _artifact_directories_by_source(artifact_root)
    documents = []
    for pdf_path in pdf_paths:
        print(f"\nProcessing document: {pdf_path.name}", flush=True)
        document_dir = artifact_dirs.get(str(pdf_path.resolve()).casefold())
        if document_dir is None:
            raise FileNotFoundError(
                f"Image artifacts are missing for {pdf_path.name}. "
                "Run the image preparation pipeline first."
            )
        summary = build_document_image_contexts(
            document_dir, vlm=vlm, force=force
        )
        documents.append(summary)
        print(
            f"Finished {pdf_path.name}: {summary['described_image_count']}/"
            f"{summary['image_count']} images described; "
            f"{summary['missing_description_count']} missing.",
            flush=True,
        )
    return {
        "documents_directory": str(documents_dir),
        "document_count": len(documents),
        "image_count": sum(item["image_count"] for item in documents),
        "described_image_count": sum(item["described_image_count"] for item in documents),
        "missing_description_count": sum(item["missing_description_count"] for item in documents),
        "cache_hits": sum(bool(item["cache_hit"]) for item in documents),
        "documents": documents,
    }


def build_document_image_contexts(
    document_dir: str | Path,
    *,
    vlm=None,
    force: bool = False,
) -> dict:
    """Build compact image records: PDF name, page metadata, and VLM description."""
    document_dir = Path(document_dir).resolve()
    manifest_path = document_dir / "images" / "manifest.json"
    output_path = document_dir / "image_context.json"
    manifest = _read_required_json(manifest_path)
    images = manifest.get("images", [])

    expected = {
        "schema_version": CONTEXT_SCHEMA_VERSION,
        "source_sha256": manifest["source_sha256"],
        "vlm_enabled": vlm is not None,
        "vlm_model": getattr(vlm, "model", vlm.__class__.__name__) if vlm is not None else None,
        "vlm_prompt_version": getattr(vlm, "prompt_version", None) if vlm is not None else None,
    }
    cached = None if force else _read_json(output_path)
    reused_descriptions = {}
    if cached and cached.get("source_sha256") == manifest["source_sha256"]:
        same_model = (
            vlm is None
            or (
                cached.get("vlm_model") == expected["vlm_model"]
                and cached.get("vlm_prompt_version") == expected["vlm_prompt_version"]
            )
        )
        if same_model:
            reused_descriptions.update({
                str(item.get("image_id")): str(item.get("visual_description") or "").strip()
                for item in cached.get("images", [])
                if str(item.get("visual_description") or "").strip()
            })

    images_dir = manifest_path.parent
    description_cache_path = images_dir / "visual_descriptions.json"
    cache_identity = {
        "schema_version": DESCRIPTION_CACHE_SCHEMA_VERSION,
        "source_sha256": manifest["source_sha256"],
        "vlm_model": expected["vlm_model"],
        "vlm_prompt_version": expected["vlm_prompt_version"],
    }
    checkpoint = None if force else _read_json(description_cache_path)
    if vlm is not None:
        if checkpoint and all(checkpoint.get(key) == value for key, value in cache_identity.items()):
            reused_descriptions.update({
                str(image_id): str(description).strip()
                for image_id, description in checkpoint.get("descriptions", {}).items()
                if str(description).strip()
            })
        checkpoint = {**cache_identity, "descriptions": reused_descriptions}
        _write_json_atomic(description_cache_path, checkpoint)

    if cached and not force and all(cached.get(key) == value for key, value in expected.items()):
        cached_images = cached.get("images", [])
        if len(cached_images) == len(images) and (
            vlm is None
            or all(str(item.get("visual_description") or "").strip() for item in cached_images)
        ):
            return _document_summary(cached, output_path, cache_hit=True)

    records = []
    for index, image in enumerate(images, start=1):
        image_id = str(image["image_id"])
        description = reused_descriptions.get(image_id, "")
        print(f"  Image {index}/{len(images)}: {image['stored_file_name']}", flush=True)
        image_path = (images_dir / image["stored_file_name"]).resolve()
        if vlm is not None and not description:
            description = _describe_image_with_vlm(image_path, vlm)
            reused_descriptions[image_id] = description
            checkpoint["descriptions"] = reused_descriptions
            _write_json_atomic(description_cache_path, checkpoint)
            print("  VLM description checkpointed.", flush=True)
        records.append({
            "image_id": image_id,
            "image_path": str(image_path),
            "stored_file_name": image["stored_file_name"],
            "file_name": manifest["file_name"],
            "source_path": manifest["source_path"],
            "page_numbers": [int(page) for page in image.get("pages", [])],
            "is_split_across_pages": bool(image.get("is_split_across_pages")),
            "width": int(image["width"]),
            "height": int(image["height"]),
            "digest": image.get("digest", ""),
            "visual_description": description,
        })

    payload = {
        **expected,
        "file_name": manifest["file_name"],
        "source_path": manifest["source_path"],
        "image_count": len(records),
        "images": records,
    }
    _write_json_atomic(output_path, payload)
    return _document_summary(payload, output_path, cache_hit=False)


def _describe_image_with_vlm(image_path, vlm):
    """Return one non-empty VLM description, raising on request failures."""
    description = str(vlm.describe_image(image_path=image_path) or "").strip()
    if not description:
        raise RuntimeError(f"VLM returned an empty description for {Path(image_path).name}.")
    return description


def _artifact_directories_by_source(artifact_root):
    result = {}
    if not artifact_root.is_dir():
        return result
    for manifest_path in artifact_root.glob("*/images/manifest.json"):
        manifest = _read_json(manifest_path)
        if manifest and manifest.get("source_path"):
            key = str(Path(manifest["source_path"]).resolve()).casefold()
            result[key] = manifest_path.parents[1]
    return result


def _document_summary(payload, output_path, *, cache_hit):
    described = sum(bool(str(image.get("visual_description") or "").strip()) for image in payload["images"])
    image_count = int(payload["image_count"])
    return {
        "file_name": payload["file_name"],
        "source_path": payload["source_path"],
        "image_count": image_count,
        "described_image_count": described,
        "missing_description_count": image_count - described,
        "output_path": str(output_path),
        "cache_hit": cache_hit,
    }


def _read_required_json(path):
    payload = _read_json(path)
    if payload is None:
        raise FileNotFoundError(f"Required artifact is missing or invalid: {path}")
    return payload


def _read_json(path):
    if not path.is_file():
        return None
    try:
        with path.open(encoding="utf-8") as file:
            return json.load(file)
    except (OSError, json.JSONDecodeError):
        return None


def _write_json_atomic(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(
        description="Describe jewelry PDF images with a VLM and save resumable image context."
    )
    parser.add_argument("documents_dir", nargs="?", type=Path, default=DEFAULT_DOCUMENTS_DIR)
    parser.add_argument("--artifact-root", type=Path, default=DEFAULT_ARTIFACT_ROOT)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--backend", choices=("gemini", "openrouter", "ollama"))
    args = parser.parse_args()
    documents_dir = args.documents_dir.resolve()
    pdf_paths = sorted(documents_dir.rglob("*.pdf"))
    if not pdf_paths:
        raise FileNotFoundError(f"No PDFs were found under {documents_dir}.")

    # Ensure image manifests and the text/chunk context artifacts exist first.
    # The selected semantic settings match notebooks 3 and 4, so existing
    # extraction and chunk caches are reused.
    from scripts import pipeline as pipeline_module

    preparation = pipeline_module.prepare_documents_for_rag(
        pdf_paths,
        artifact_root=args.artifact_root,
        chunking_strategy="semantic",
        chunking_options={
            "similarity_threshold": 0.65,
            "max_chunk_size": 800,
            "unit_prefix": "passage: ",
        },
        extraction_options={"use_ocr_fallback": True},
        extract_images=True,
        force=False,
        embedding_local_files_only=True,
    )
    status = preparation["artifact_status"]
    print(
        "Prepared PDFs:", len(pdf_paths),
        "text cache hits:", int(status["text_cache_hit"].sum()),
        "chunk cache hits:", int(status["chunks_cache_hit"].sum()),
        "image cache hits:", int(status["images_cache_hit"].sum()),
        flush=True,
    )
    vlm = create_vlm_from_environment(args.backend)
    result = build_pdf_image_contexts(
        documents_dir,
        artifact_root=args.artifact_root,
        vlm=vlm,
        force=args.force,
    )
    print("\nImage-context association complete")
    print("Documents:", result["document_count"])
    print("Images:", result["image_count"])
    print("Described images:", result["described_image_count"])
    print("Missing descriptions:", result["missing_description_count"])
    print("Document cache hits:", result["cache_hits"])


if __name__ == "__main__":
    main()
