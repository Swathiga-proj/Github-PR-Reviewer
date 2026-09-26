# GitHub PR Reviewer

Automated PR review system powered by **watsonx.ai** (`ibm/granite-3-8b-instruct`). When a PR is opened, a FastAPI webhook server runs three analysis lenses in parallel and posts a combined risk-scored review back to GitHub.

## Architecture

```
GitHub PR opened → POST /webhook (FastAPI)
                        │
              ┌─────────┼──────────┐
              ▼         ▼          ▼
      Regression   API Contract  Backend
      Lens + RAG      Lens       Pitfall
      (pgvector)                  Lens
              └─────────┼──────────┘
                        ▼
                    Combiner
              Risk Score + Markdown
                        │
           ┌────────────┴───────────┐
           ▼                        ▼
   PR Review (inline)   Issue Comment (summary)
```

## Prerequisites

- Python 3.11+
- Docker & Docker Compose
- IBM watsonx.ai account (API key + project ID)
- GitHub Fine-Grained Personal Access Token with read/write PR permissions

## Setup

### 1. Clone and install dependencies

```bash
git clone <repo-url>
cd github-pr-reviewer
pip install -e ".[dev]"
```

### 2. Configure environment variables

```bash
cp .env.example .env
# Edit .env and fill in all required values
```

Required variables:

| Variable | Description |
|---|---|
| `GITHUB_TOKEN` | Fine-Grained GitHub API token |
| `GITHUB_WEBHOOK_SECRET` | Shared HMAC secret for webhook verification |
| `WATSONX_API_KEY` | IBM watsonx.ai API key |
| `WATSONX_PROJECT_ID` | watsonx.ai project ID |
| `WATSONX_URL` | watsonx.ai service URL (default: `https://us-south.ml.cloud.ibm.com`) |
| `WATSONX_LLM_MODEL_ID` | LLM model (default: `ibm/granite-3-8b-instruct`) |
| `WATSONX_EMBED_MODEL_ID` | Embedding model (default: `ibm/slate-125m-english-rtrvr`) |
| `DATABASE_URL` | PostgreSQL DSN e.g. `postgresql://user:pass@localhost:5432/prreviewer` |
| `EXTERNAL_API_REGISTRY_URL` | Optional fallback API contract registry URL |

### 3. Start PostgreSQL with pgvector

```bash
docker compose up -d
```

This starts a `pgvector/pgvector:pg16` container and auto-runs the migration in `db/migrations/`.

### 4. Bootstrap the RAG vector store (one-time)

```bash
python scripts/bootstrap_rag.py \
  --rules-file PROJECT_RULES.md \
  --github-owner <org-or-user> \
  --github-repo <repo-name>
```

This ingests `PROJECT_RULES.md` (8 rule sections) and past closed PRs into pgvector.

### 5. Run the webhook server

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

### 6. Expose the server to GitHub (development)

Use [ngrok](https://ngrok.com/) or similar to expose the local server:

```bash
ngrok http 8000
```

Configure the GitHub webhook:
- **Payload URL**: `https://<your-ngrok-url>/webhook`
- **Content type**: `application/json`
- **Secret**: value of `GITHUB_WEBHOOK_SECRET`
- **Events**: Pull requests only

## Running Tests

```bash
pytest
```

## Project Structure

```
.
├── app/
│   ├── config.py              # pydantic-settings configuration
│   ├── main.py                # FastAPI app entry point
│   ├── webhook/               # Webhook receiver & HMAC verification
│   ├── github_client/         # Async GitHub REST API client
│   ├── rag/                   # pgvector store + watsonx.ai embeddings
│   ├── lenses/                # Regression, API Contract, Backend Pitfall lenses
│   └── combiner/              # Risk scorer + PR comment publisher
├── scripts/
│   └── bootstrap_rag.py       # One-time RAG ingestion script
├── db/
│   └── migrations/            # SQL migrations (auto-run by docker-compose)
├── tests/                     # pytest test suite
├── PROJECT_RULES.md           # Project coding rules indexed into RAG
├── docker-compose.yml
├── pyproject.toml
└── .env.example
```
