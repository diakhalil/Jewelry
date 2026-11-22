"""Idempotent Qdrant indexing for embedded PDF text chunks."""

import uuid
from collections.abc import Iterable
from pathlib import Path
import os

import numpy as np
import pandas as pd
from qdrant_client import QdrantClient, models
from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[3]
# Read server settings from ai-service/.env, while allowing shell variables to
# override them for CI, notebooks, and hosted Qdrant deployments.
load_dotenv(PROJECT_ROOT / ".env", override=False)
DEFAULT_QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333").strip()
DEFAULT_QDRANT_API_KEY = os.getenv("QDRANT_API_KEY", "").strip() or None
DEFAULT_QDRANT_PATH = PROJECT_ROOT / "data" / "rag" / "qdrant"
DEFAULT_COLLECTION_NAME = "jewelry_text_gemini_embedding_2_v1"
POINT_NAMESPACE = uuid.UUID("ea981375-7b13-4c99-af35-e2c15fda0299")
IMAGE_COLLECTION_NAME = "jewelry_images_gemini_embedding_2_v1"

IMAGE_POINT_NAMESPACE = uuid.UUID(
    "68496ca0-906e-4fab-9682-7467875c68be"
)

FILTERABLE_FIELDS = ("file_name", "source_path")
IMAGE_STAGE1_COLLECTION_NAME = "gatherly_document_images_stage1_e5_v1"
IMAGE_STAGE1_POINT_NAMESPACE = uuid.UUID(
    "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
)


def connect_to_qdrant(
    url: str | None = DEFAULT_QDRANT_URL,
    *,
    path: str | Path | None = None,
) -> QdrantClient:
    """Open persistent local Qdrant, or connect to a Qdrant server URL."""
    if path is not None:
        path = Path(path).resolve()
        path.mkdir(parents=True, exist_ok=True)
        client = QdrantClient(path=str(path))
        client.get_collections()
        return client
    if not url:
        raise ValueError("Provide either a local Qdrant path or a server URL.")
    client = QdrantClient(
        url=url,
        api_key=DEFAULT_QDRANT_API_KEY,
        timeout=60,
    )
    try:
        client.get_collections()
    except Exception as error:
        raise ConnectionError(
            f"Cannot connect to Qdrant at {url}. Start it with "
            "'docker compose up -d qdrant'."
        ) from error
    return client


def index_text_chunks(
    chunks: pd.DataFrame,
    embeddings: np.ndarray,
    *,
    url: str = DEFAULT_QDRANT_URL,
    qdrant_path: str | Path | None = None,
    client: QdrantClient | None = None,
    create_payload_indexes: bool = True,
    collection_name: str = DEFAULT_COLLECTION_NAME,
    batch_size: int = 128,
) -> dict:
    """Create/validate the collection and synchronize supplied documents."""
    chunks = chunks.reset_index(drop=True).copy()
    embeddings = np.asarray(embeddings, dtype=np.float32)
    _validate_inputs(chunks, embeddings, batch_size)

    client = client if client is not None else connect_to_qdrant(url, path=qdrant_path)
    collection_created = _ensure_collection(
        client, collection_name, int(embeddings.shape[1])
    )
    if create_payload_indexes:
        _ensure_payload_indexes(client, collection_name)

    source_paths = chunks["source_path"].drop_duplicates().astype(str).tolist()
    previous_ids = {
        source_path: _point_ids_for_source(client, collection_name, source_path)
        for source_path in source_paths
    }

    desired_ids_by_source = {
        source_path: {
            str(uuid.uuid5(POINT_NAMESPACE, str(chunk_id)))
            for chunk_id in chunks.loc[
                chunks["source_path"].astype(str) == source_path, "chunk_id"
            ]
        }
        for source_path in source_paths
    }
    changed_sources = {
        source_path for source_path in source_paths
        if previous_ids[source_path] != desired_ids_by_source[source_path]
    }
    upload_mask = chunks["source_path"].astype(str).isin(changed_sources)
    upload_count = int(upload_mask.sum())
    if upload_count:
        client.upload_points(
            collection_name=collection_name,
            points=_generate_points(chunks.loc[upload_mask].reset_index(drop=True), embeddings[upload_mask.to_numpy()]),
            batch_size=batch_size,
            parallel=1,
            max_retries=3,
            wait=True,
        )

    # Delete obsolete chunks only after replacement points have uploaded.
    desired_ids = set().union(*desired_ids_by_source.values()) if desired_ids_by_source else set()
    stale_ids = set().union(*previous_ids.values()) - desired_ids
    if stale_ids:
        client.delete(
            collection_name=collection_name,
            points_selector=models.PointIdsList(points=sorted(stale_ids)),
            wait=True,
        )

    indexed_count = client.count(
        collection_name=collection_name,
        count_filter=models.Filter(
            should=[models.FieldCondition(
                key="source_path", match=models.MatchValue(value=path)
            ) for path in source_paths]
        ),
        exact=True,
    ).count
    if indexed_count != len(chunks):
        raise RuntimeError(
            f"Qdrant indexed {indexed_count} supplied-document points; "
            f"expected {len(chunks)}."
        )

    total_count = client.count(collection_name=collection_name, exact=True).count
    return {
        "client": client,
        "url": url,
        "path": str(Path(qdrant_path).resolve()) if qdrant_path is not None else None,
        "collection_name": collection_name,
        "collection_created": collection_created,
        "indexed_count": indexed_count,
        "total_count": total_count,
        "vector_size": int(embeddings.shape[1]),
        "uploaded_count": upload_count,
        "unchanged_document_count": len(source_paths) - len(changed_sources),
    }

def index_image_contexts(
    image_records: list[dict],
    embeddings: np.ndarray,
    *,
    url: str = DEFAULT_QDRANT_URL,
    qdrant_path: str | Path | None = None,
    client: QdrantClient | None = None,
    create_payload_indexes: bool = True,
    collection_name: str = IMAGE_COLLECTION_NAME,
    batch_size: int = 128,
) -> dict:
    """Index image-context vectors in a separate Qdrant collection."""

    images = pd.DataFrame(image_records).reset_index(drop=True)
    embeddings = np.asarray(embeddings, dtype=np.float32)

    _validate_image_inputs(
        images,
        embeddings,
        batch_size,
    )

    client = client if client is not None else connect_to_qdrant(url, path=qdrant_path)

    collection_created = _ensure_collection(
        client,
        collection_name,
        int(embeddings.shape[1]),
    )

    if create_payload_indexes:
        _ensure_payload_indexes(client, collection_name)

    source_paths = (
        images["source_path"]
        .drop_duplicates()
        .astype(str)
        .tolist()
    )

    previous_ids = {
        source_path: _point_ids_for_source(
            client,
            collection_name,
            source_path,
        )
        for source_path in source_paths
    }

    desired_ids_by_source = {
        source_path: {
            str(uuid.uuid5(IMAGE_POINT_NAMESPACE, str(image_id)))
            for image_id in images.loc[
                images["source_path"].astype(str) == source_path, "image_id"
            ]
        }
        for source_path in source_paths
    }
    changed_sources = {
        source_path for source_path in source_paths
        if previous_ids[source_path] != desired_ids_by_source[source_path]
    }
    upload_mask = images["source_path"].astype(str).isin(changed_sources)
    upload_count = int(upload_mask.sum())
    if upload_count:
        client.upload_points(
            collection_name=collection_name,
            points=_generate_image_points(
                images.loc[upload_mask].reset_index(drop=True),
                embeddings[upload_mask.to_numpy()],
            ),
            batch_size=batch_size,
            parallel=1,
            max_retries=3,
            wait=True,
        )

    desired_ids = set().union(*desired_ids_by_source.values()) if desired_ids_by_source else set()

    previous_point_ids = set()

    for point_ids in previous_ids.values():
        previous_point_ids.update(point_ids)

    stale_ids = previous_point_ids - desired_ids

    if stale_ids:
        client.delete(
            collection_name=collection_name,
            points_selector=models.PointIdsList(
                points=sorted(stale_ids)
            ),
            wait=True,
        )

    indexed_count = client.count(
        collection_name=collection_name,
        count_filter=models.Filter(
            should=[
                models.FieldCondition(
                    key="source_path",
                    match=models.MatchValue(value=source_path),
                )
                for source_path in source_paths
            ]
        ),
        exact=True,
    ).count

    if indexed_count != len(images):
        raise RuntimeError(
            f"Qdrant indexed {indexed_count} image points; "
            f"expected {len(images)}."
        )

    total_count = client.count(
        collection_name=collection_name,
        exact=True,
    ).count

    return {
        "client": client,
        "url": url,
        "path": str(Path(qdrant_path).resolve()) if qdrant_path is not None else None,
        "collection_name": collection_name,
        "collection_created": collection_created,
        "indexed_count": indexed_count,
        "total_count": total_count,
        "vector_size": int(embeddings.shape[1]),
        "uploaded_count": upload_count,
        "unchanged_document_count": len(source_paths) - len(changed_sources),
    }

def _validate_inputs(chunks, embeddings, batch_size):
    required = {"chunk_id", "text", "file_name", "source_path", "page_number"}
    missing = required - set(chunks.columns)
    if missing:
        raise ValueError("Chunks are missing columns: " + ", ".join(sorted(missing)))
    if chunks.empty or embeddings.ndim != 2:
        raise ValueError("Chunks must be non-empty and embeddings must be 2D.")
    if len(chunks) != embeddings.shape[0]:
        raise ValueError("Chunk and embedding row counts do not match.")
    if chunks["chunk_id"].duplicated().any():
        raise ValueError("Chunk IDs must be unique.")
    if not np.isfinite(embeddings).all():
        raise ValueError("Embeddings contain NaN or infinite values.")
    if batch_size <= 0:
        raise ValueError("batch_size must be greater than zero.")

def _validate_image_inputs(
    images: pd.DataFrame,
    embeddings: np.ndarray,
    batch_size: int,
) -> None:
    required = {
        "image_id",
        "image_path",
        "file_name",
        "source_path",
        "page_numbers",
        "context_text",
    }

    missing = required - set(images.columns)

    if missing:
        raise ValueError(
            "Image records are missing columns: "
            + ", ".join(sorted(missing))
        )

    if images.empty:
        raise ValueError(
            "Image records cannot be empty."
        )

    if embeddings.ndim != 2:
        raise ValueError(
            "Image embeddings must be a 2D matrix."
        )

    if len(images) != embeddings.shape[0]:
        raise ValueError(
            "Image record and embedding row counts "
            "do not match."
        )

    if images["image_id"].duplicated().any():
        raise ValueError(
            "Image IDs must be unique."
        )

    if embeddings.shape[1] == 0:
        raise ValueError(
            "Image embedding dimension cannot be empty."
        )

    if not np.isfinite(embeddings).all():
        raise ValueError(
            "Image embeddings contain NaN or infinite values."
        )

    if batch_size <= 0:
        raise ValueError(
            "batch_size must be greater than zero."
        )

def _ensure_collection(client, collection_name, vector_size):
    if client.collection_exists(collection_name):
        vectors = client.get_collection(collection_name).config.params.vectors
        if vectors.size != vector_size or vectors.distance != models.Distance.COSINE:
            raise ValueError(
                f"Collection '{collection_name}' is incompatible: expected "
                f"{vector_size}-dimensional cosine vectors. Use a new collection name."
            )
        return False

    client.create_collection(
        collection_name=collection_name,
        vectors_config=models.VectorParams(
            size=vector_size, distance=models.Distance.COSINE, on_disk=True
        ),
        on_disk_payload=True,
    )
    return True


def _ensure_payload_indexes(client, collection_name):
    existing = set(client.get_collection(collection_name).payload_schema)
    for field in FILTERABLE_FIELDS:
        if field not in existing:
            client.create_payload_index(
                collection_name=collection_name,
                field_name=field,
                field_schema=models.PayloadSchemaType.KEYWORD,
                wait=True,
            )


def _generate_points(chunks, embeddings) -> Iterable[models.PointStruct]:
    for position, row in chunks.iterrows():
        payload = {
            str(column): _json_safe(row[column])
            for column in chunks.columns
        }
        yield models.PointStruct(
            id=str(uuid.uuid5(POINT_NAMESPACE, str(row["chunk_id"]))),
            vector=embeddings[position].tolist(),
            payload=payload,
        )

def _generate_image_points(
    images: pd.DataFrame,
    embeddings: np.ndarray,
) -> Iterable[models.PointStruct]:
    """Generate deterministic Qdrant points for images."""

    for position, row in images.iterrows():
        payload = {
            str(column): _json_safe(row[column])
            for column in images.columns
        }

        payload["retrieval_type"] = "image"

        yield models.PointStruct(
            id=str(
                uuid.uuid5(
                    IMAGE_POINT_NAMESPACE,
                    str(row["image_id"]),
                )
            ),
            vector=embeddings[position].tolist(),
            payload=payload,
        )
        
def _json_safe(value):
    if value is None:
        return None
    if isinstance(value, np.generic):
        return _json_safe(value.item())
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value


def _point_ids_for_source(client, collection_name, source_path):
    point_ids = set()
    offset = None
    source_filter = models.Filter(
        must=[models.FieldCondition(
            key="source_path", match=models.MatchValue(value=source_path)
        )]
    )
    while True:
        points, offset = client.scroll(
            collection_name=collection_name,
            scroll_filter=source_filter,
            limit=256,
            offset=offset,
            with_payload=False,
            with_vectors=False,
        )
        point_ids.update(str(point.id) for point in points)
        if offset is None:
            return point_ids


def index_image_stage1(
    image_records: list[dict],
    embeddings: np.ndarray,
    *,
    url: str = DEFAULT_QDRANT_URL,
    collection_name: str = IMAGE_STAGE1_COLLECTION_NAME,
    batch_size: int = 128,
) -> dict:
    """Index stage-1 image vectors. Does not touch the full-context collection."""
    return index_image_contexts(
        image_records,
        embeddings,
        url=url,
        collection_name=collection_name,
        batch_size=batch_size,
    )
    
