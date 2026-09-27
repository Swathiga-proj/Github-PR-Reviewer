"""
Unit tests for app.rag.store and scripts.bootstrap_rag.

All external dependencies (asyncpg pool, watsonx.ai embed) are mocked.
No live database or API credentials required.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FAKE_VECTOR = [0.1] * 768
FAKE_VECTOR_STR = str(FAKE_VECTOR)


def _make_pool(fetch_rows=None, execute_ok=True):
    """Return a mock asyncpg Pool."""
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=fetch_rows or [])
    pool.execute = AsyncMock(return_value="INSERT 1" if execute_ok else None)
    pool.close = AsyncMock()
    return pool


# ---------------------------------------------------------------------------
# app.rag.store — embed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_embed_returns_vector():
    mock_client = MagicMock()
    # v2 format: plain list of vectors
    mock_client.embed_documents.return_value = [FAKE_VECTOR]
    with patch("app.rag.store._get_embeddings_client", return_value=mock_client):
        from app.rag.store import embed
        result = await embed("some text")
    assert result == FAKE_VECTOR
    mock_client.embed_documents.assert_called_once_with(texts=["some text"])


@pytest.mark.asyncio
async def test_embed_returns_vector_v1_format():
    """Also handles legacy v1 dict response format."""
    mock_client = MagicMock()
    mock_client.embed_documents.return_value = {
        "results": [{"embedding": FAKE_VECTOR}]
    }
    with patch("app.rag.store._get_embeddings_client", return_value=mock_client):
        from app.rag.store import embed
        result = await embed("some text")
    assert result == FAKE_VECTOR


# ---------------------------------------------------------------------------
# app.rag.store — upsert_document
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_upsert_document_calls_pool():
    pool = _make_pool()
    mock_client = MagicMock()
    mock_client.embed_documents.return_value = {
        "results": [{"embedding": FAKE_VECTOR}]
    }
    with (
        patch("app.rag.store._get_embeddings_client", return_value=mock_client),
        patch("app.rag.store._get_pool", AsyncMock(return_value=pool)),
    ):
        from app.rag import store
        # Reset module-level pool so _get_pool mock is used
        store._pool = None
        await store.upsert_document(
            "rules:1",
            "API Design Rules…",
            {"source": "project_rules", "section": "1. API Design Rules"},
        )
    pool.execute.assert_called_once()
    call_args = pool.execute.call_args[0]
    # call_args = (sql, doc_id, text, metadata_json, vector_str)
    assert "rules:1" in call_args          # doc_id present somewhere
    assert "API Design Rules" in call_args[2]  # text is the 3rd positional arg


# ---------------------------------------------------------------------------
# app.rag.store — search
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_search_returns_results():
    fake_rows = [
        {
            "id": "rules:1",
            "content": "API Design Rules…",
            "metadata": json.dumps({"source": "project_rules", "section": "1. API Design Rules"}),
            "score": 0.92,
        },
        {
            "id": "pr:org/repo:5",
            "content": "PR #5: Fix N+1 query",
            "metadata": json.dumps({"source": "past_pr", "pr_number": 5}),
            "score": 0.78,
        },
    ]
    pool = _make_pool(fetch_rows=fake_rows)
    mock_client = MagicMock()
    mock_client.embed_documents.return_value = {
        "results": [{"embedding": FAKE_VECTOR}]
    }
    with (
        patch("app.rag.store._get_embeddings_client", return_value=mock_client),
        patch("app.rag.store._get_pool", AsyncMock(return_value=pool)),
    ):
        from app.rag import store
        store._pool = None
        results = await store.search("missing auth dependency", top_k=2)

    assert len(results) == 2
    assert results[0]["id"] == "rules:1"
    assert results[0]["score"] == 0.92
    assert results[0]["metadata"]["source"] == "project_rules"
    assert results[1]["id"] == "pr:org/repo:5"


@pytest.mark.asyncio
async def test_search_passes_top_k_to_pool():
    pool = _make_pool(fetch_rows=[])
    mock_client = MagicMock()
    mock_client.embed_documents.return_value = {
        "results": [{"embedding": FAKE_VECTOR}]
    }
    with (
        patch("app.rag.store._get_embeddings_client", return_value=mock_client),
        patch("app.rag.store._get_pool", AsyncMock(return_value=pool)),
    ):
        from app.rag import store
        store._pool = None
        await store.search("query", top_k=7)

    call_args = pool.fetch.call_args[0]
    assert 7 in call_args  # top_k passed as positional arg


# ---------------------------------------------------------------------------
# scripts.bootstrap_rag — chunk_rules
# ---------------------------------------------------------------------------

def test_chunk_rules_produces_8_sections(tmp_path):
    rules = Path("PROJECT_RULES.md").read_text(encoding="utf-8")
    rules_file = tmp_path / "PROJECT_RULES.md"
    rules_file.write_text(rules, encoding="utf-8")

    from scripts.bootstrap_rag import chunk_rules
    chunks = chunk_rules(rules_file)

    assert len(chunks) == 8
    assert chunks[0]["doc_id"] == "rules:1"
    assert chunks[0]["metadata"]["source"] == "project_rules"
    assert "API Design" in chunks[0]["metadata"]["section"]
    assert chunks[-1]["doc_id"] == "rules:8"
    assert "Documentation" in chunks[-1]["metadata"]["section"]


def test_chunk_rules_section_text_not_empty(tmp_path):
    rules_file = tmp_path / "RULES.md"
    rules_file.write_text(
        "1. First Rule\nDo this.\n\n2. Second Rule\nDo that.\n",
        encoding="utf-8",
    )
    from scripts.bootstrap_rag import chunk_rules
    chunks = chunk_rules(rules_file)
    assert len(chunks) == 2
    assert "Do this" in chunks[0]["text"]
    assert "Do that" in chunks[1]["text"]


# ---------------------------------------------------------------------------
# scripts.bootstrap_rag — chunk_pr
# ---------------------------------------------------------------------------

def test_chunk_pr_with_body():
    from scripts.bootstrap_rag import chunk_pr
    pr = {"number": 42, "title": "Fix N+1", "body": "Added select_related.", "state": "closed"}
    chunk = chunk_pr(pr, "org/repo")
    assert chunk["doc_id"] == "pr:org/repo:42"
    assert "Fix N+1" in chunk["text"]
    assert "select_related" in chunk["text"]
    assert chunk["metadata"]["source"] == "past_pr"
    assert chunk["metadata"]["pr_number"] == 42


def test_chunk_pr_without_body():
    from scripts.bootstrap_rag import chunk_pr
    pr = {"number": 10, "title": "Bump deps", "body": None, "state": "closed"}
    chunk = chunk_pr(pr, "org/repo")
    assert "Bump deps" in chunk["text"]
    # No double newline when body is absent
    assert "\n\nNone" not in chunk["text"]
