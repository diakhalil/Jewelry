"""Local and Gemini API text embedding utilities with reusable caches."""

import hashlib
import json
import os
import re
from pathlib import Path
from time import perf_counter, sleep
from typing import Any

import numpy as np
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_MODEL_NAME = "BAAI/bge-m3"
DEFAULT_EMBEDDING_ROOT = PROJECT_ROOT / "data" / "rag" / "embeddings"
DEFAULT_BATCH_SIZE = 1
DEFAULT_MAX_SEQUENCE_LENGTH = 512
EMBEDDING_SCHEMA_VERSION = 1
DEFAULT_GEMINI_MODEL_NAME = "gemini-embedding-001"
DEFAULT_GEMINI_BATCH_SIZE = 100
MAX_GEMINI_RATE_LIMIT_RETRIES = 5


def load_embedding_model(
    model_name: str = DEFAULT_MODEL_NAME,
    *,
    device: str | None = None,
    local_files_only: bool = False,
    max_sequence_length: int = DEFAULT_MAX_SEQUENCE_LENGTH,
    model_kwargs: dict[str, Any] | None = None,
):
    """Load a Sentence Transformers model only when embeddings are required."""
    from sentence_transformers import SentenceTransformer

    if max_sequence_length <= 0:
        raise ValueError("max_sequence_length must be greater than zero.")

    model = SentenceTransformer(
        model_name,
        device=device,
        local_files_only=local_files_only,
        model_kwargs=model_kwargs,
    )
    model.max_seq_length = max_sequence_length
    return model


def prepare_texts(
    texts: list[str],
    model_name: str,
    text_type: str,
) -> list[str]:
    """Apply model-specific passage or query prefixes."""
    if text_type not in {"passage", "query"}:
        raise ValueError("text_type must be 'passage' or 'query'.")

    cleaned = [str(text).strip() for text in texts]
    if any(not text for text in cleaned):
        raise ValueError("Embedding input contains empty text.")

    if "e5" in model_name.casefold():
        prefix = "passage: " if text_type == "passage" else "query: "
        return [prefix + text for text in cleaned]

    return cleaned


def embed_texts(
    texts: list[str],
    model,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    show_progress_bar: bool = True,
) -> np.ndarray:
    """Create normalized float32 embeddings and validate their shape."""
    if batch_size <= 0:
        raise ValueError("batch_size must be greater than zero.")
    if not texts:
        raise ValueError("At least one text is required for embedding.")

    embeddings = np.asarray(
        model.encode(
            texts,
            batch_size=batch_size,
            show_progress_bar=show_progress_bar,
            normalize_embeddings=True,
        ),
        dtype=np.float32,
    )
    _validate_embeddings(embeddings, expected_rows=len(texts))
    return embeddings


def get_or_create_chunk_embeddings(
    chunks: pd.DataFrame,
    *,
    model_name: str = DEFAULT_MODEL_NAME,
    embedding_root: str | Path = DEFAULT_EMBEDDING_ROOT,
    model=None,
    device: str | None = None,
    local_files_only: bool = True,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_sequence_length: int = DEFAULT_MAX_SEQUENCE_LENGTH,
    model_kwargs: dict[str, Any] | None = None,
    force: bool = False,
    show_progress_bar: bool = True,
) -> dict[str, Any]:
    """Load valid cached chunk embeddings or create them idempotently."""
    _validate_chunks(chunks)
    chunks = chunks.reset_index(drop=True).copy()
    artifact_dir = Path(embedding_root).resolve() / _safe_model_name(model_name)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    embeddings_file = artifact_dir / "embeddings.npy"
    chunks_file = artifact_dir / "chunks.json"
    manifest_file = artifact_dir / "manifest.json"

    chunk_fingerprint = _chunk_fingerprint(chunks)
    expected_manifest = {
        "schema_version": EMBEDDING_SCHEMA_VERSION,
        "model_name": model_name,
        "normalized": True,
        "text_type": "passage",
        "max_sequence_length": max_sequence_length,
        "chunk_count": len(chunks),
        "chunk_fingerprint": chunk_fingerprint,
    }

    if not force:
        cached = _load_valid_cache(
            manifest_file,
            embeddings_file,
            chunks_file,
            expected_manifest,
            chunks,
        )
        if cached is not None:
            embeddings, manifest = cached
            return _build_result(
                chunks, embeddings, manifest, artifact_dir, cache_hit=True
            )

    if model is None:
        model = load_embedding_model(
            model_name,
            device=device,
            local_files_only=local_files_only,
            max_sequence_length=max_sequence_length,
            model_kwargs=model_kwargs,
        )

    prepared = prepare_texts(
        chunks["text"].tolist(), model_name=model_name, text_type="passage"
    )
    started = perf_counter()
    embeddings = embed_texts(
        prepared,
        model,
        batch_size=batch_size,
        show_progress_bar=show_progress_bar,
    )
    elapsed_seconds = perf_counter() - started

    manifest = {
        **expected_manifest,
        "embedding_dimension": int(embeddings.shape[1]),
        "embedding_dtype": str(embeddings.dtype),
        "batch_size": batch_size,
        "elapsed_seconds": round(elapsed_seconds, 4),
    }
    _write_npy_atomic(embeddings_file, embeddings)
    _write_json_atomic(
        chunks_file,
        {"chunk_ids": chunks["chunk_id"].tolist()},
    )
    _write_json_atomic(manifest_file, manifest)
    return _build_result(
        chunks, embeddings, manifest, artifact_dir, cache_hit=False
    )


def embed_queries(
    queries: list[str],
    *,
    model,
    model_name: str = DEFAULT_MODEL_NAME,
    batch_size: int = DEFAULT_BATCH_SIZE,
    show_progress_bar: bool = False,
) -> np.ndarray:
    """Embed retrieval queries with the correct model-specific preparation."""
    prepared = prepare_texts(
        queries, model_name=model_name, text_type="query"
    )
    return embed_texts(
        prepared,
        model,
        batch_size=batch_size,
        show_progress_bar=show_progress_bar,
    )


def cosine_search(
    query_embeddings: np.ndarray,
    chunk_embeddings: np.ndarray,
    *,
    top_k: int = 5,
) -> tuple[np.ndarray, np.ndarray]:
    """Return cosine scores and row positions for normalized embeddings."""
    query_embeddings = np.asarray(query_embeddings, dtype=np.float32)
    chunk_embeddings = np.asarray(chunk_embeddings, dtype=np.float32)
    _validate_embeddings(query_embeddings)
    _validate_embeddings(chunk_embeddings)

    if query_embeddings.shape[1] != chunk_embeddings.shape[1]:
        raise ValueError("Query and chunk embedding dimensions do not match.")
    if top_k <= 0:
        raise ValueError("top_k must be greater than zero.")

    top_k = min(top_k, chunk_embeddings.shape[0])
    similarities = query_embeddings @ chunk_embeddings.T
    positions = np.argsort(-similarities, axis=1)[:, :top_k]
    scores = np.take_along_axis(similarities, positions, axis=1)
    return scores, positions


def load_gemini_api_key(env_file: str | Path | None = None) -> str:
    """Read GEMINI_API_KEY from a local .env file or process environment."""
    if env_file is not None:
        path = Path(env_file)
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    continue
                name, value = stripped.split("=", 1)
                if name.strip() == "GEMINI_API_KEY":
                    value = value.strip().strip("\"'")
                    if value:
                        os.environ["GEMINI_API_KEY"] = value
                        return value
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if key:
        return key
    raise EnvironmentError(
        "GEMINI_API_KEY is not set. Add it to ai-service/.env or the environment."
    )


def create_gemini_client(env_file: str | Path | None = None):
    """Create the Google Gen AI client; reread .env on every call."""
    from google import genai

    return genai.Client(api_key=load_gemini_api_key(env_file))


def prepare_gemini_texts(
    texts: list[str], *, model_name: str, text_type: str
) -> tuple[list[str], str | None]:
    """Format text and task metadata for supported Gemini embedding models."""
    if text_type not in {"passage", "query"}:
        raise ValueError("text_type must be 'passage' or 'query'.")
    cleaned = [str(text).strip() for text in texts]
    if not cleaned or any(not text for text in cleaned):
        raise ValueError("Embedding input must contain non-empty texts.")

    if model_name == "gemini-embedding-001":
        task_type = "RETRIEVAL_DOCUMENT" if text_type == "passage" else "RETRIEVAL_QUERY"
        return cleaned, task_type
    if model_name == "gemini-embedding-2":
        if text_type == "passage":
            return [f"title: none | text: {text}" for text in cleaned], None
        return [f"task: search result | query: {text}" for text in cleaned], None
    raise ValueError(
        f"Unsupported Gemini embedding model {model_name!r}; use "
        "'gemini-embedding-001' or 'gemini-embedding-2'."
    )


def embed_gemini_texts(
    texts: list[str],
    *,
    client,
    model_name: str = DEFAULT_GEMINI_MODEL_NAME,
    text_type: str,
    batch_size: int = DEFAULT_GEMINI_BATCH_SIZE,
) -> np.ndarray:
    """Create normalized Gemini embeddings for passages or retrieval queries."""
    prepared, task_type = prepare_gemini_texts(
        texts, model_name=model_name, text_type=text_type
    )
    if batch_size <= 0:
        raise ValueError("batch_size must be greater than zero.")
    # Embedding 2 aggregates a list into one vector, so request one item at a time.
    effective_batch_size = 1 if model_name == "gemini-embedding-2" else batch_size
    vectors = []
    batch_count = (len(prepared) + effective_batch_size - 1) // effective_batch_size
    for batch_index, start in enumerate(range(0, len(prepared), effective_batch_size)):
        batch = prepared[start : start + effective_batch_size]
        vectors.extend(
            _embed_gemini_batch(batch, client, model_name, task_type)
        )
        if batch_index % 10 == 0 or start + len(batch) == len(prepared):
            print(f"{model_name} embedding progress: {start + len(batch)}/{len(prepared)} texts")
    embeddings = np.asarray(vectors, dtype=np.float32)
    return _normalize_embeddings(embeddings, len(prepared))


def load_cached_gemini_embeddings(
    texts: list[str],
    *,
    text_type: str,
    model_name: str = DEFAULT_GEMINI_MODEL_NAME,
    cache_root: str | Path = DEFAULT_EMBEDDING_ROOT,
) -> dict[str, Any]:
    """Load an exact Gemini embedding cache entry without making an API call.

    Unlike ``get_or_create_gemini_embeddings``, this cache-only variant raises
    when the exact input batch is not cached. Use it in evaluation notebooks
    where a cache miss must not silently trigger a billable embedding request.
    """
    cleaned = [str(text).strip() for text in texts]
    if not cleaned or any(not text for text in cleaned):
        raise ValueError("Embedding input must contain non-empty texts.")
    if text_type not in {"passage", "query"}:
        raise ValueError("text_type must be 'passage' or 'query'.")

    digest = hashlib.sha256(
        json.dumps(cleaned, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    artifact_dir = Path(cache_root).resolve() / _safe_model_name(model_name)
    embeddings_file = artifact_dir / f"{text_type}_{digest}.npy"
    metadata_file = artifact_dir / f"{text_type}_{digest}.json"
    expected = {
        "model_name": model_name,
        "text_type": text_type,
        "text_count": len(cleaned),
        "input_sha256": digest,
        "normalized": True,
    }
    if not embeddings_file.is_file() or not metadata_file.is_file():
        raise FileNotFoundError(
            "Exact cached Gemini embeddings are missing; no API request was made. "
            f"Expected cache entry for {len(cleaned)} {text_type} texts at {artifact_dir}. "
            "Run the relevant embedding notebook first to create it."
        )
    metadata = _read_json(metadata_file)
    if not metadata or any(metadata.get(key) != value for key, value in expected.items()):
        raise ValueError(f"Gemini embedding cache metadata does not match: {metadata_file}")
    embeddings = np.load(embeddings_file, allow_pickle=False)
    _validate_embeddings(embeddings, expected_rows=len(cleaned))
    return {
        "embeddings": embeddings,
        "cache_hit": True,
        "elapsed_seconds": 0.0,
        "embeddings_file": embeddings_file,
        "manifest": metadata,
    }


def store_cached_gemini_embeddings(
    texts: list[str],
    embeddings: np.ndarray,
    *,
    text_type: str,
    model_name: str = DEFAULT_GEMINI_MODEL_NAME,
    cache_root: str | Path = DEFAULT_EMBEDDING_ROOT,
    batch_size: int = 1,
) -> dict[str, Any]:
    """Persist an already-computed Gemini vector batch under its exact input key."""
    cleaned = [str(text).strip() for text in texts]
    if not cleaned or any(not text for text in cleaned):
        raise ValueError("Embedding input must contain non-empty texts.")
    if text_type not in {"passage", "query"}:
        raise ValueError("text_type must be 'passage' or 'query'.")
    values = np.asarray(embeddings, dtype=np.float32)
    _validate_embeddings(values, expected_rows=len(cleaned))
    digest = hashlib.sha256(
        json.dumps(cleaned, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    artifact_dir = Path(cache_root).resolve() / _safe_model_name(model_name)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    embeddings_file = artifact_dir / f"{text_type}_{digest}.npy"
    metadata_file = artifact_dir / f"{text_type}_{digest}.json"
    metadata = {
        "model_name": model_name,
        "text_type": text_type,
        "text_count": len(cleaned),
        "input_sha256": digest,
        "normalized": True,
        "embedding_dimension": int(values.shape[1]),
        "elapsed_seconds": 0.0,
        "batch_size": batch_size,
    }
    _write_npy_atomic(embeddings_file, values)
    _write_json_atomic(metadata_file, metadata)
    return {"embeddings": values, "cache_hit": False, "embeddings_file": embeddings_file, "manifest": metadata}


def get_or_create_gemini_embeddings(
    texts: list[str],
    *,
    client,
    text_type: str,
    model_name: str = DEFAULT_GEMINI_MODEL_NAME,
    cache_root: str | Path = DEFAULT_EMBEDDING_ROOT,
    batch_size: int = DEFAULT_GEMINI_BATCH_SIZE,
    force: bool = False,
) -> dict[str, Any]:
    """Reuse completed Gemini embeddings and checkpoint each successful API batch."""
    cleaned = [str(text).strip() for text in texts]
    if not cleaned or any(not text for text in cleaned):
        raise ValueError("Embedding input must contain non-empty texts.")
    if text_type not in {"passage", "query"}:
        raise ValueError("text_type must be 'passage' or 'query'.")
    if batch_size <= 0:
        raise ValueError("batch_size must be greater than zero.")

    digest = hashlib.sha256(
        json.dumps(cleaned, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    artifact_dir = Path(cache_root).resolve() / _safe_model_name(model_name)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    embeddings_file = artifact_dir / f"{text_type}_{digest}.npy"
    metadata_file = artifact_dir / f"{text_type}_{digest}.json"
    expected = {
        "model_name": model_name,
        "text_type": text_type,
        "text_count": len(cleaned),
        "input_sha256": digest,
        "normalized": True,
    }
    if not force and embeddings_file.is_file() and metadata_file.is_file():
        try:
            metadata = _read_json(metadata_file)
            cached_embeddings = np.load(embeddings_file, allow_pickle=False)
            if metadata and all(metadata.get(key) == value for key, value in expected.items()):
                _validate_embeddings(cached_embeddings, expected_rows=len(cleaned))
                return {
                    "embeddings": cached_embeddings,
                    "cache_hit": True,
                    "elapsed_seconds": 0.0,
                    "embeddings_file": embeddings_file,
                    "manifest": metadata,
                }
        except (OSError, ValueError):
            pass

    prepared, task_type = prepare_gemini_texts(
        cleaned, model_name=model_name, text_type=text_type
    )
    effective_batch_size = 1 if model_name == "gemini-embedding-2" else batch_size
    checkpoint_dir = artifact_dir / f"{text_type}_{digest}_b{effective_batch_size}.partial"
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    batch_count = (len(prepared) + effective_batch_size - 1) // effective_batch_size
    vectors = []
    started = perf_counter()
    for batch_index, start in enumerate(range(0, len(prepared), effective_batch_size)):
        batch = prepared[start : start + effective_batch_size]
        checkpoint_file = checkpoint_dir / f"batch_{batch_index:06d}.npy"
        batch_vectors = None
        if not force and checkpoint_file.is_file():
            try:
                cached_batch = np.load(checkpoint_file, allow_pickle=False)
                _validate_embeddings(cached_batch, expected_rows=len(batch))
                batch_vectors = cached_batch
            except (OSError, ValueError):
                checkpoint_file.unlink(missing_ok=True)
        if batch_vectors is None:
            batch_vectors = np.asarray(
                _embed_gemini_batch(batch, client, model_name, task_type),
                dtype=np.float32,
            )
            _write_npy_atomic(checkpoint_file, batch_vectors)
            print(
                f"{model_name} checkpoint saved: batch {batch_index + 1}/"
                f"{batch_count} ({start + len(batch)}/{len(prepared)} texts)"
            )
        else:
            print(f"Using {model_name} checkpoint: batch {batch_index + 1}/{batch_count}")
        vectors.append(batch_vectors)

    embeddings = np.concatenate(vectors, axis=0)
    _validate_embeddings(embeddings, expected_rows=len(cleaned))
    elapsed_seconds = perf_counter() - started
    metadata = {
        **expected,
        "embedding_dimension": int(embeddings.shape[1]),
        "elapsed_seconds": round(elapsed_seconds, 4),
        "batch_size": effective_batch_size,
    }
    _write_npy_atomic(embeddings_file, embeddings)
    _write_json_atomic(metadata_file, metadata)
    for checkpoint_file in checkpoint_dir.iterdir():
        checkpoint_file.unlink(missing_ok=True)
    checkpoint_dir.rmdir()
    return {
        "embeddings": embeddings,
        "cache_hit": False,
        "elapsed_seconds": elapsed_seconds,
        "embeddings_file": embeddings_file,
        "manifest": metadata,
    }


def _load_valid_cache(
    manifest_file, embeddings_file, chunks_file, expected, chunks,
):
    manifest = _read_json(manifest_file)
    saved_chunks = _read_json(chunks_file)
    if not manifest or not saved_chunks or not embeddings_file.is_file():
        return None
    if not all(manifest.get(key) == value for key, value in expected.items()):
        return None
    if saved_chunks.get("chunk_ids") != chunks["chunk_id"].tolist():
        return None

    try:
        embeddings = np.load(embeddings_file, allow_pickle=False)
        _validate_embeddings(embeddings, expected_rows=len(chunks))
    except (OSError, ValueError):
        return None
    if manifest.get("embedding_dimension") != embeddings.shape[1]:
        return None
    return embeddings, manifest


def _validate_chunks(chunks: pd.DataFrame) -> None:
    if not isinstance(chunks, pd.DataFrame):
        raise TypeError("chunks must be a pandas DataFrame.")
    required = {"chunk_id", "text"}
    missing = required - set(chunks.columns)
    if missing:
        raise ValueError("Chunks are missing columns: " + ", ".join(sorted(missing)))
    if chunks.empty:
        raise ValueError("The chunk DataFrame is empty.")
    if chunks["chunk_id"].duplicated().any():
        raise ValueError("Chunk IDs must be unique.")
    if chunks["text"].astype(str).str.strip().eq("").any():
        raise ValueError("Chunks contain empty text.")


def _validate_embeddings(
    embeddings: np.ndarray,
    expected_rows: int | None = None,
) -> None:
    if embeddings.ndim != 2:
        raise ValueError(f"Embeddings must be 2D; received {embeddings.shape}.")
    if expected_rows is not None and embeddings.shape[0] != expected_rows:
        raise ValueError("Embedding rows do not match the expected text count.")
    if embeddings.shape[0] == 0 or embeddings.shape[1] == 0:
        raise ValueError("Embeddings cannot have an empty dimension.")
    if not np.isfinite(embeddings).all():
        raise ValueError("Embeddings contain NaN or infinite values.")


def _chunk_fingerprint(chunks: pd.DataFrame) -> str:
    digest = hashlib.sha256()
    for row in chunks[["chunk_id", "text"]].itertuples(index=False):
        digest.update(str(row.chunk_id).encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(row.text).encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def _safe_model_name(model_name: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9._-]+", "__", model_name).strip("._")
    return safe or "embedding_model"


def _build_result(chunks, embeddings, manifest, artifact_dir, cache_hit):
    return {
        "chunks": chunks,
        "embeddings": embeddings,
        "manifest": manifest,
        "artifact_directory": artifact_dir,
        "embeddings_file": artifact_dir / "embeddings.npy",
        "chunks_file": artifact_dir / "chunks.json",
        "manifest_file": artifact_dir / "manifest.json",
        "cache_hit": cache_hit,
    }


def _read_json(path: Path):
    if not path.is_file():
        return None
    try:
        with path.open(encoding="utf-8") as file:
            return json.load(file)
    except (OSError, json.JSONDecodeError):
        return None


def _write_json_atomic(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)
    temporary.replace(path)


def _write_npy_atomic(path: Path, embeddings: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as file:
        np.save(file, embeddings, allow_pickle=False)
    temporary.replace(path)


def _normalize_embeddings(embeddings: np.ndarray, expected_rows: int) -> np.ndarray:
    _validate_embeddings(embeddings, expected_rows=expected_rows)
    norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
    if np.any(norms == 0):
        raise ValueError("Embedding provider returned a zero-length vector.")
    return embeddings / norms


def _embed_gemini_batch(batch, client, model_name, task_type):
    from google.genai import types

    for attempt in range(MAX_GEMINI_RATE_LIMIT_RETRIES + 1):
        try:
            arguments = {"model": model_name, "contents": batch}
            if task_type is not None:
                arguments["config"] = types.EmbedContentConfig(task_type=task_type)
            response = client.models.embed_content(**arguments)
            break
        except Exception as error:
            message = str(error).casefold()
            if ("429" not in message and "resource_exhausted" not in message) or attempt == MAX_GEMINI_RATE_LIMIT_RETRIES:
                raise
            delay = _gemini_retry_delay(error, attempt)
            print(
                f"Gemini rate limit reached; waiting {delay:.0f}s "
                f"before retry {attempt + 1}/{MAX_GEMINI_RATE_LIMIT_RETRIES}."
            )
            sleep(delay)

    vectors = [item.values for item in (response.embeddings or [])]
    if len(vectors) != len(batch):
        raise RuntimeError(
            f"Gemini returned {len(vectors)} embeddings for {len(batch)} input texts."
        )
    return _normalize_embeddings(np.asarray(vectors, dtype=np.float32), len(batch))


def _gemini_retry_delay(error: Exception, attempt: int) -> float:
    message = str(error)
    retry_in = re.search(r"retry in\s+(\d+(?:\.\d+)?)s", message, re.IGNORECASE)
    if retry_in:
        return float(retry_in.group(1)) + 2.0
    retry_delay = re.search(r"retryDelay['\"]?\s*:\s*['\"](\d+)s", message)
    if retry_delay:
        return float(retry_delay.group(1)) + 2.0
    return min(60.0 * (2**attempt), 300.0)
