-- Migration 000: enable pgvector extension
-- This file is auto-run by docker-compose on first container start.
-- The full embeddings table DDL is created by 001_create_embeddings_table.sql (Sub-Task 4).

CREATE EXTENSION IF NOT EXISTS vector;
