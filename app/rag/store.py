"""
RAG vector store — pgvector + watsonx.ai embeddings.

Public API
----------
embed(text)                             -> list[float]
upsert_document(doc_id, text, metadata) -> None
search(query, top_k)                    -> list[dict]

All database calls are async (asyncpg).
The module maintains a single module-level connection pool created on first use.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import asyncpg
from ibm_watsonx_ai import Credentials
from ibm_watsonx_ai.foundation_models import Embeddings

from app.config import get_settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level connection pool (lazy initialised)
# ---------------------------------------------------------------------------

_pool: asyncpg.Pool | None = None


async def _get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        settings = get_settings()
        _pool = await asyncpg.create_pool(
            dsn=settings.database_url,
            min_size=1,
            max_size=5,
            command_timeout=30,
        )
        logger.info("asyncpg pool created")
    return _pool


async def close_pool() -> None:
    """Close the connection pool. Call on app shutdown."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


# ---------------------------------------------------------------------------
# watsonx.ai embedding
# ---------------------------------------------------------------------------

def _get_embeddings_client() -> Embeddings:
    settings = get_settings()
    return Embeddings(
        model_id=settings.watsonx_embed_model_id,
        credentials=Credentials(
            api_key=settings.watsonx_api_key,
            url=settings.watsonx_url,
        ),
        project_id=settings.watsonx_project_id,
    )


async def embed(text: str) -> list[float]:
    """
    Return the embedding vector for *text* using the configured watsonx.ai model.
    The call is synchronous inside the ibm-watsonx-ai SDK; we run it directly
    (FastAPI background tasks tolerate blocking calls at the task level).
    """
    client = _get_embeddings_client()
    response = client.embed_documents(texts=[text])
    # ibm-watsonx-ai returns: {"results": [{"embedding": [...]}]}
    return response["results"][0]["embedding"]


# ---------------------------------------------------------------------------
# Store & retrieve
# ---------------------------------------------------------------------------

async def upsert_document(
    doc_id: str,
    text: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    """
    Embed *text* and upsert into the embeddings table.
    On conflict (same doc_id) the existing row is updated.
    """
    vector = await embed(text)
    meta = metadata or {}
    pool = await _get_pool()
    await pool.execute(
        """
        INSERT INTO embeddings (id, content, metadata, embedding)
        VALUES ($1, $2, $3::jsonb, $4)
        ON CONFLICT (id) DO UPDATE
            SET content   = EXCLUDED.content,
                metadata  = EXCLUDED.metadata,
                embedding = EXCLUDED.embedding,
                created_at = now()
        """,
        doc_id,
        text,
        json.dumps(meta),
        str(vector),          # pgvector accepts '[x,y,z,...]' string
    )
    logger.debug("upserted doc_id=%s", doc_id)


async def search(query: str, top_k: int = 5) -> list[dict[str, Any]]:
    """
    Return the *top_k* most similar documents to *query*.

    Each result dict contains:
        id       : str
        content  : str
        metadata : dict
        score    : float   (cosine similarity, 0–1, higher = more similar)
    """
    vector = await embed(query)
    pool = await _get_pool()
    rows = await pool.fetch(
        """
        SELECT id, content, metadata,
               1 - (embedding <=> $1) AS score
        FROM   embeddings
        ORDER  BY embedding <=> $1
        LIMIT  $2
        """,
        str(vector),
        top_k,
    )
    return [
        {
            "id": row["id"],
            "content": row["content"],
            "metadata": json.loads(row["metadata"]),
            "score": float(row["score"]),
        }
        for row in rows
    ]
