"""Validate supported RAG source files and return their basic metadata."""

from pathlib import Path
from typing import Any

SUPPORTED_DOCUMENT_TYPES = {
    ".pdf": "pdf",
}


def detect_document_type(path: str | Path) -> str:
    """Validate a file path and return its supported document type."""
    path = Path(path).expanduser().resolve()

    if not path.exists():
        raise FileNotFoundError(f"Document was not found: {path}")
    if not path.is_file():
        raise ValueError(f"Document path is not a file: {path}")

    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_DOCUMENT_TYPES:
        supported = ", ".join(sorted(SUPPORTED_DOCUMENT_TYPES))
        raise ValueError(
            f"Unsupported document type: {suffix or '[no extension]'}. "
            f"Supported types: {supported}"
        )

    return SUPPORTED_DOCUMENT_TYPES[suffix]


def load_document(path: str | Path) -> dict[str, Any]:
    """Return validated source metadata for the PDF extraction pipeline.

    Text extraction is handled by ``scripts.2_extraction``; this function only
    validates the source and supplies metadata shared by later pipeline stages.
    """
    path = Path(path).expanduser().resolve()
    document_type = detect_document_type(path)

    return {
        "file_name": path.name,
        "source_path": str(path),
        "file_type": document_type,
        "file_size_bytes": path.stat().st_size,
    }
