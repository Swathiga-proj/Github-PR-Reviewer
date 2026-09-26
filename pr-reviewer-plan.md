# GitHub PR Reviewer — Implementation Plan

## Confirmed Design Decisions

| Decision | Choice |
|---|---|
| Lens weighting | All three lenses weighted equally in risk score |
| Webhook HMAC | `X-Hub-Signature-256` validation enabled using `GITHUB_WEBHOOK_SECRET` |
| LLM model | `ibm/granite-3-8b-instruct` via watsonx.ai — instruction-tuned for code analysis and structured JSON output |

---

## Top-Level Overview

Build an automated PR review system that listens for GitHub `pull_request` `opened` events via a FastAPI webhook. When triggered, it fetches the PR diff and runs **three analysis lenses in parallel** using **watsonx.ai** as the LLM backend. The three lenses are:

1. **Regression Lens + RAG** — detects changes that could break existing behaviour, grounded by **`PROJECT_RULES.md`** (8 rule categories: API design, code structure, dependencies, async/performance, error handling, security, testing, documentation) and past PR history, both indexed in **pgvector**.
2. **API Contract Lens** — validates that API changes comply with contracts found in repo-committed specs or an external registry fallback.
3. **Backend Pitfall Lens** — detects common backend anti-patterns including N+1 queries, missing auth/authz, insecure defaults, poor error handling, and dependency/version drift.

Results from all three lenses are fed to a **Combiner** that produces a **Risk Score** (based on critical finding counts) and a single polished review. The review is posted back to GitHub as:
- A **PR Review** with inline line annotations where applicable.
- A **top-level summary comment** with the Risk Score and consolidated findings.

The GitHub Fine-Grained API key is already created. This is a greenfield Python project — no existing application code exists yet.

---

## Sub-Tasks

---

### Sub-Task 1 — Project Scaffolding & Configuration

**Intent**
Establish the project's directory structure, dependency manifest, and configuration layer so every subsequent sub-task has a stable foundation to build on.

**Expected Outcomes**
- `pyproject.toml` (or `requirements.txt`) with all required dependencies declared.
- A `config.py` or `settings.py` using `pydantic-settings` to load secrets from environment variables (GitHub token, watsonx.ai API key/project ID, PostgreSQL DSN, external API registry URL).
- A `.env.example` documenting all required env vars.
- A top-level `app/` package with sub-packages: `webhook/`, `lenses/`, `rag/`, `combiner/`, `github_client/`.
- `docker-compose.yml` that spins up PostgreSQL with the pgvector extension.

**Todo List**
- [ ] Create `pyproject.toml` with dependencies: `fastapi`, `uvicorn`, `httpx`, `asyncpg`, `pgvector`, `pydantic-settings`, `ibm-watsonx-ai`, `python-dotenv`, `pytest`, `pytest-asyncio`.
- [ ] Create `app/__init__.py` and sub-package `__init__.py` files for `webhook`, `lenses`, `rag`, `combiner`, `github_client`.
- [ ] Create `app/config.py` using `pydantic-settings` with fields for all secrets and config values.
- [ ] Create `.env.example` listing all required environment variables.
- [ ] Create `docker-compose.yml` with a `postgres` service using the `pgvector/pgvector:pg16` image.
- [ ] Create `README.md` with setup and run instructions.

**Relevant Context**
- No existing application code — pure greenfield.
- GitHub Fine-Grained API key already exists; it must be loaded from env, never hardcoded.

**Status** — `[x] done`

---

### Sub-Task 2 — GitHub Client

**Intent**
Provide a thin async client wrapping the GitHub REST API so every other component can fetch PR data and post review comments without duplicating HTTP logic.

**Expected Outcomes**
- `app/github_client/client.py` with async methods:
  - `get_pr_diff(owner, repo, pull_number) -> str` — returns the unified diff.
  - `get_pr_files(owner, repo, pull_number) -> list[dict]` — returns changed files with patch data.
  - `list_past_prs(owner, repo, state="closed") -> list[dict]` — used by the RAG bootstrap.
  - `post_pr_review(owner, repo, pull_number, review_body, comments) -> None` — posts an inline review.
  - `post_issue_comment(owner, repo, pull_number, body) -> None` — posts a top-level summary comment.
- All calls use the Fine-Grained token from `config.py`.

**Todo List**
- [ ] Implement `GithubClient` class in `app/github_client/client.py` using `httpx.AsyncClient`.
- [ ] Implement `get_pr_diff`, `get_pr_files`, `list_past_prs`, `post_pr_review`, `post_issue_comment`.
- [ ] Raise descriptive exceptions on non-2xx responses.
- [ ] Write unit tests in `tests/test_github_client.py` using `httpx` mock transport.

**Relevant Context**
- GitHub REST API base: `https://api.github.com`
- Auth header: `Authorization: Bearer <token>`
- Accept header for diff: `application/vnd.github.v3.diff`
- Fine-Grained token is already created.

**Status** — `[x] done`

---

### Sub-Task 3 — FastAPI Webhook Endpoint

**Intent**
Create the entry point that receives GitHub `pull_request` webhook events, validates the payload, and dispatches the review pipeline.

**Expected Outcomes**
- `app/webhook/router.py` with a `POST /webhook` endpoint.
- Validates the `X-GitHub-Event` header — only processes `pull_request` events with `action == "opened"`.
- Verifies `X-Hub-Signature-256` HMAC using `GITHUB_WEBHOOK_SECRET` from config; returns `403` on mismatch.
- Extracts `owner`, `repo`, `pull_number` from the payload.
- Calls the review pipeline (Sub-Task 6) as a background task so GitHub's 10-second timeout is not breached.
- Returns `202 Accepted` immediately.
- `app/main.py` wires the router and starts the FastAPI app.

**Todo List**
- [ ] Create `app/main.py` with `FastAPI` app instance and router inclusion.
- [ ] Create `app/webhook/router.py` with `POST /webhook` handler.
- [ ] Parse and validate the GitHub webhook payload (Pydantic model).
- [ ] Implement HMAC-SHA256 signature verification using `GITHUB_WEBHOOK_SECRET`.
- [ ] Dispatch review pipeline via `BackgroundTasks`.
- [ ] Write integration test in `tests/test_webhook.py` using FastAPI `TestClient`.

**Relevant Context**
- GitHub sends `X-GitHub-Event: pull_request` and a JSON body.
- The `action` field must be `"opened"` to trigger the pipeline.
- `BackgroundTasks` from FastAPI is sufficient; no Celery/queue needed.

**Status** — `[x] done`

---

### Sub-Task 4 — RAG Store (pgvector + watsonx.ai Embeddings)

**Intent**
Build the vector store layer and the one-time bootstrap script that ingests project rule docs and past PR history so the Regression Lens can retrieve relevant context at review time.

**Expected Outcomes**
- `app/rag/store.py` with:
  - `embed(text: str) -> list[float]` — calls watsonx.ai embedding model.
  - `upsert_document(doc_id, text, metadata)` — embeds and stores in pgvector.
  - `search(query: str, top_k: int) -> list[dict]` — returns top-k similar chunks.
- `scripts/bootstrap_rag.py` — standalone script that:
  - Reads and chunks **`PROJECT_RULES.md`** from the repo root (8 rule categories, each ingested as a separate chunk with `source=project_rules` metadata).
  - Fetches past closed PRs via `GithubClient.list_past_prs` and indexes their diffs and descriptions with `source=past_pr` metadata.
- Database migration SQL (`db/migrations/001_create_embeddings_table.sql`) that creates the `embeddings` table with a `vector` column.

**Todo List**
- [ ] Create `db/migrations/001_create_embeddings_table.sql` with `CREATE TABLE embeddings` using pgvector column type.
- [ ] Implement `app/rag/store.py` with `embed`, `upsert_document`, `search` using `asyncpg` and `pgvector`.
- [ ] Implement `scripts/bootstrap_rag.py`:
  - Parse `PROJECT_RULES.md` — split by numbered section heading (e.g. `1. API Design Rules`) so each of the 8 rule categories becomes one chunk.
  - Fetch past closed PRs via `GithubClient.list_past_prs` and index diffs + PR descriptions.
- [ ] Write unit tests for `search` using a test PostgreSQL instance or mock.

**Relevant Context**
- watsonx.ai embedding model: use the model ID from `config.py` (e.g. `ibm/slate-125m-english-rtrvr`).
- pgvector SQL: `CREATE EXTENSION IF NOT EXISTS vector;` must run first.
- Chunking strategy for `PROJECT_RULES.md`: split on numbered section headings — produces 8 clean, semantically distinct chunks (API Design, Code Structure, Dependency & Config, Async & Performance, Error Handling & Logging, Security, Testing, Documentation).
- Each chunk stored with metadata: `{"source": "project_rules", "section": "<section title>"}`.
- Past PR chunks stored with metadata: `{"source": "past_pr", "pr_number": <n>, "repo": "<owner/repo>"}`.

**Status** — `[x] done`

---

### Sub-Task 5 — Three Lenses (Parallel Analysis)

**Intent**
Implement the three analysis lenses as independent async functions that each accept the PR diff/files and return a structured `LensResult` (pass/fail + list of findings with severity and optional file/line reference).

**Expected Outcomes**
- `app/lenses/models.py` — `Finding` and `LensResult` Pydantic models.
- `app/lenses/regression_lens.py` — `run(diff, files) -> LensResult`:
  - Retrieves top-k chunks from RAG store matching the diff — results will include relevant `project_rules` sections and similar `past_pr` diffs.
  - Prompts `ibm/granite-3-8b-instruct` with the diff + retrieved context to identify:
    - Violations of any of the 8 rule categories in `PROJECT_RULES.md` (e.g. raw DB model returned, blocking call inside async, bare `except:`, missing auth dependency).
    - Changes that historically caused regressions based on past PR evidence.
  - Returns structured JSON findings with `severity`, `message`, `file`, `line`, and `rule_section` (which of the 8 categories was violated).
- `app/lenses/api_contract_lens.py` — `run(diff, files, repo_path) -> LensResult`:
  - Checks for OpenAPI/Swagger spec files in the changed files.
  - Falls back to querying an external API registry if no spec is found in the repo.
  - Prompts watsonx.ai to identify contract violations.
- `app/lenses/backend_pitfall_lens.py` — `run(diff, files) -> LensResult`:
  - Prompts watsonx.ai to detect: N+1 queries, missing auth/authz, insecure defaults, poor error handling, dependency/version drift.
- All three lenses use a shared `app/lenses/watsonx_client.py` wrapper for LLM calls.

**Todo List**
- [ ] Create `app/lenses/models.py` with `Finding(severity, message, file, line, rule_section=None)` and `LensResult(lens_name, passed, findings)` — `rule_section` is populated by the Regression Lens to reference the violated `PROJECT_RULES.md` category.
- [ ] Create `app/lenses/watsonx_client.py` — thin async wrapper around `ibm-watsonx-ai` that sends a prompt and returns the text response.
- [ ] Implement `app/lenses/regression_lens.py` with RAG retrieval + watsonx.ai prompt.
- [ ] Implement `app/lenses/api_contract_lens.py` with repo spec lookup + external registry fallback + watsonx.ai prompt.
- [ ] Implement `app/lenses/backend_pitfall_lens.py` with pitfall detection prompt covering all five categories.
- [ ] Write unit tests for each lens using mocked watsonx.ai and RAG responses.

**Relevant Context**
- All three lenses must be `async` so they can be gathered with `asyncio.gather`.
- Severity levels: `critical`, `warning`, `info`.
- The prompt for each lens should include the diff, any retrieved context, and explicit instructions to return structured JSON findings.

**Status** — `[x] done`

---

### Sub-Task 6 — Combiner, Risk Scorer & PR Comment Publisher

**Intent**
Aggregate the three `LensResult` objects into a single Risk Score and two GitHub outputs: an inline PR review with per-line annotations and a top-level summary comment.

**Expected Outcomes**
- `app/combiner/combiner.py`:
  - `run_review_pipeline(owner, repo, pull_number)` — async function that fetches the diff/files, runs the three lenses with `asyncio.gather`, calls the combiner, and posts results to GitHub.
  - `compute_risk_score(results: list[LensResult]) -> int` — counts critical findings; score 0-100 (scaled).
  - `build_summary_comment(results, risk_score) -> str` — returns polished Markdown for the top-level comment.
  - `build_inline_comments(results) -> list[dict]` — maps findings with file/line info to GitHub review comment objects.
- Uses `GithubClient.post_pr_review` for inline annotations and `GithubClient.post_issue_comment` for the summary.

**Todo List**
- [ ] Implement `run_review_pipeline` in `app/combiner/combiner.py` orchestrating diff fetch → parallel lenses → combine → post.
- [ ] Implement `compute_risk_score` — all three lenses weighted equally; critical findings add weight, warnings add lesser weight; score capped at 100.
- [ ] Implement `build_summary_comment` producing Markdown with risk badge, per-lens summary table, and top findings.
- [ ] Implement `build_inline_comments` mapping findings to GitHub's review comment format `{path, position, body}`.
- [ ] Wire `run_review_pipeline` into the webhook background task (Sub-Task 3).
- [ ] Write unit tests for `compute_risk_score` and `build_summary_comment`.

**Relevant Context**
- `asyncio.gather(*[regression_lens.run(...), api_contract_lens.run(...), backend_pitfall_lens.run(...)])` runs the three lenses truly in parallel.
- GitHub review comment `position` is the line number within the diff hunk, not the file line number.
- Risk badge in the summary: 🟢 Low (0-30) / 🟡 Medium (31-60) / 🔴 High (61-100).

**Status** — `[x] done`

---

## Architecture Diagram (reference)

```
GitHub PR opened
      │
      ▼
POST /webhook  (FastAPI)
      │  202 Accepted immediately
      │  BackgroundTask ──────────────────────────────────────┐
      │                                                        │
      ▼                                                        │
GithubClient.get_pr_diff / get_pr_files                       │
      │                                                        │
      ├─────────────────┬──────────────────┐                  │
      ▼                 ▼                  ▼                   │
Regression Lens   API Contract Lens  Backend Pitfall Lens     │
  + RAG retrieval   (repo spec /        (N+1, auth,           │
  (pgvector)         ext registry)       pitfalls, deps)      │
      │                 │                  │                   │
      └─────────────────┴──────────────────┘                  │
                        │                                      │
                        ▼                                      │
                   Combiner                                    │
              Risk Score + Markdown                            │
                        │                                      │
           ┌────────────┴────────────┐                        │
           ▼                         ▼                        │
   PR Review (inline)    Issue Comment (summary)              │
                                                              │
      ──────────────────────────────────────────────────────┘
```

## Environment Variables Required

| Variable | Purpose |
|---|---|
| `GITHUB_TOKEN` | Fine-Grained API key for GitHub |
| `GITHUB_WEBHOOK_SECRET` | (optional) HMAC secret to verify webhook payloads |
| `WATSONX_API_KEY` | IBM watsonx.ai API key |
| `WATSONX_PROJECT_ID` | watsonx.ai project ID |
| `WATSONX_URL` | watsonx.ai service URL |
| `WATSONX_EMBED_MODEL_ID` | Embedding model ID for RAG |
| `WATSONX_LLM_MODEL_ID` | LLM model ID for lens prompts — default `ibm/granite-3-8b-instruct` |
| `DATABASE_URL` | PostgreSQL DSN (with pgvector) |
| `EXTERNAL_API_REGISTRY_URL` | Fallback API contract registry URL |
