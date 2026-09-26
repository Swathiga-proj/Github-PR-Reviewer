-- Migration 001: create embeddings table for pgvector RAG store
-- Requires the vector extension (enabled by 000_enable_pgvector.sql).

CREATE TABLE IF NOT EXISTS embeddings (
    id          TEXT PRIMARY KEY,           -- stable doc_id (e.g. "rules:3" or "pr:42:chunk:0")
    content     TEXT        NOT NULL,       -- raw text chunk
    metadata    JSONB       NOT NULL DEFAULT '{}'::jsonb,
    embedding   vector(768) NOT NULL,       -- dimension must match the embed model output
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ANN index for fast cosine-similarity search
CREATE INDEX IF NOT EXISTS embeddings_embedding_idx
    ON embeddings
    USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 50);
