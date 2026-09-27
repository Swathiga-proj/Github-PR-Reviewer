"""
Bootstrap script — one-time ingestion of project rules and past PR history into pgvector.

Usage
-----
python scripts/bootstrap_rag.py \\
    --rules-file PROJECT_RULES.md \\
    --github-owner <org-or-user> \\
    --github-repo  <repo-name>

Optional flags
    --max-prs  N     Maximum number of past closed PRs to ingest (default: 100)
    --dry-run        Print chunks without writing to the database
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
from pathlib import Path

# Allow running from repo root without installing the package
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.github_client.client import GithubClient
from app.rag.store import close_pool, upsert_document

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s — %(message)s",
)
logger = logging.getLogger(__name__)

import tiktoken  # pip install tiktoken

# Granite doesn't publish a tiktoken-compatible tokenizer, but cl100k_base
# gives a close-enough approximation for chunking purposes.
_ENCODER = tiktoken.get_encoding("cl100k_base")

def count_tokens(text: str) -> int:
    return len(_ENCODER.encode(text))
# ---------------------------------------------------------------------------
# Chunking helpers
# ---------------------------------------------------------------------------

# Matches numbered section headings like "1. API Design Rules"
_SECTION_RE = re.compile(r"^(\d+\.\s+.+)$", re.MULTILINE)

MAX_TOKENS_PER_CHUNK = 300  # leave headroom under the 512 limit for title/filename text

def split_patch_into_chunks(patch: str, max_tokens: int = MAX_TOKENS_PER_CHUNK) -> list[str]:
    """Split a diff patch into chunks that fit under the embedding model's token limit."""
    lines = patch.splitlines(keepends=True)
    chunks: list[str] = []
    current: list[str] = []
    current_tokens = 0

    for line in lines:
        line_tokens = count_tokens(line)
        if current_tokens + line_tokens > max_tokens and current:
            chunks.append("".join(current))
            current, current_tokens = [], 0
        current.append(line)
        current_tokens += line_tokens

    if current:
        chunks.append("".join(current))

    return chunks
def safe_chunk_text(doc_id_prefix: str, text: str, metadata: dict, max_tokens: int = MAX_TOKENS_PER_CHUNK) -> list[dict]:
    if count_tokens(text) <= max_tokens:
        return [{"doc_id": doc_id_prefix, "text": text, "metadata": metadata}]
    pieces = split_patch_into_chunks(text, max_tokens=max_tokens)
    return [
        {"doc_id": f"{doc_id_prefix}:{i}", "text": p, "metadata": {**metadata, "part": i}}
        for i, p in enumerate(pieces)
    ]
def chunk_rules(rules_path: Path) -> list[dict]:
    """
    Split PROJECT_RULES.md into one chunk per numbered section.
    Returns a list of dicts: {doc_id, text, metadata}.
    """
    text = rules_path.read_text(encoding="utf-8")
    boundaries = [m.start() for m in _SECTION_RE.finditer(text)]
    chunks = []
    for i, start in enumerate(boundaries):
        end = boundaries[i + 1] if i + 1 < len(boundaries) else len(text)
        section_text = text[start:end].strip()
        # Extract section title (first line)
        title = section_text.splitlines()[0].strip()
        # Derive a short slug for the doc_id (e.g. "1. API Design Rules" → "1")
        section_num = title.split(".")[0].strip()
        chunks.append(
            {
                "doc_id": f"rules:{section_num}",
                "text": section_text,
                "metadata": {
                    "source": "project_rules",
                    "section": title,
                    "rules_file": rules_path.name,
                },
            }
        )
    logger.info("Parsed %d rule sections from %s", len(chunks), rules_path.name)
    return chunks


# def chunk_pr(pr: dict, repo_full_name: str) -> dict:
#     """
#     Build a single chunk for a closed PR (title + body + diff summary).
#     The diff itself is not fetched here to keep bootstrap fast; only metadata
#     fields available in the list response are used.
#     """
#     number = pr.get("number", 0)
#     title = pr.get("title", "")
#     body = (pr.get("body") or "").strip()
#     text = f"PR #{number}: {title}\n\n{body}" if body else f"PR #{number}: {title}"
#     return {
#         "doc_id": f"pr:{repo_full_name}:{number}",
#         "text": text,
#         "metadata": {
#             "source": "past_pr",
#             "pr_number": number,
#             "repo": repo_full_name,
#             "state": pr.get("state", "closed"),
#         },
#     }
async def chunk_pr_with_diff(
    gh: GithubClient,
    pr: dict,
    repo_full_name: str,
    owner: str,
    repo: str,
) -> list[dict]:
    
    number = pr.get("number", 0)
    title = pr.get("title", "")
    body = (pr.get("body") or "").strip()
    base_meta = {
        "source": "past_pr",
        "pr_number": number,
        "repo": repo_full_name,
        "state": pr.get("state", "closed"),
    }

    chunks = []

    # 1. Summary chunk (title + body)
    summary_text = f"PR #{number}: {title}\n\n{body}" if body else f"PR #{number}: {title}"
    chunks.extend(
        safe_chunk_text(
            f"pr:{repo_full_name}:{number}:summary",
            summary_text,
            {**base_meta, "type": "summary"},
        )
    )

    # 2. Fetch the actual files + patches
    try:
        files = await gh.get_pr_files(owner, repo, number)   
    except Exception as e:
        logger.warning("Could not fetch files for PR #%s: %s", number, e)
        return chunks

    for f in files:
        filename = f.get("filename", "")
        patch = f.get("patch") or ""
        status = f.get("status", "")

        if not filename.endswith(".py") or not patch:
            continue

        header = f"PR #{number}: {title}\nFile: {filename} ({status})\n\n"
        header_tokens = count_tokens(header)
        patch_chunks = split_patch_into_chunks(
            patch, max_tokens=MAX_TOKENS_PER_CHUNK - header_tokens
        )

        for i, patch_piece in enumerate(patch_chunks):
            text = header + patch_piece
            chunks.append({
                "doc_id": f"pr:{repo_full_name}:{number}:{filename}:{i}",
                "text": text,
                "metadata": {
                    **base_meta,
                    "type": "diff",
                    "filename": filename,
                    "status": status,
                    "part": i,
                    "total_parts": len(patch_chunks),
                },
            })
        return chunks
# ---------------------------------------------------------------------------
# Main ingestion logic
# ---------------------------------------------------------------------------

async def ingest(
    rules_path: Path,
    github_owner: str,
    github_repo: str,
    max_prs: int,
    dry_run: bool,
) -> None:
    # --- 1. Project rules ----------------------------------------------------
    rule_chunks = chunk_rules(rules_path)
    for chunk in rule_chunks:
        if dry_run:
            logger.info("[dry-run] Would upsert %s: %s…", chunk["doc_id"], chunk["text"][:80])
        else:
            logger.info("Upserting rule chunk: %s", chunk["doc_id"])
            await upsert_document(chunk["doc_id"], chunk["text"], chunk["metadata"])

    # --- 2. Past PRs ---------------------------------------------------------
    per_page = min(max_prs, 100)
    max_pages = max(1, -(-max_prs // per_page))  # ceiling division
    repo_full = f"{github_owner}/{github_repo}"  # ← moved up, defined before use

    logger.info(
        "Fetching up to %d closed PRs from %s/%s…",
        max_prs,
        github_owner,
        github_repo,
    )

    pr_chunk_count = 0
    async with GithubClient() as gh:
        prs = await gh.list_past_prs(
            github_owner, github_repo,
            per_page=per_page,
            max_pages=max_pages,
        )

        for pr in prs[:max_prs]:
            chunks = await chunk_pr_with_diff(
                gh, pr, repo_full, github_owner, github_repo
            )
            for chunk in chunks:
                if dry_run:
                    logger.info("[dry-run] %s → %s…", chunk["doc_id"], chunk["text"][:70])
                else:
                    await upsert_document(chunk["doc_id"], chunk["text"], chunk["metadata"])
                pr_chunk_count += 1

    if not dry_run:
        await close_pool()

    logger.info("Bootstrap complete. Rules: %d, PR chunks: %d", len(rule_chunks), pr_chunk_count)

# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Bootstrap RAG vector store")
    parser.add_argument(
        "--rules-file",
        type=Path,
        default=Path("PROJECT_RULES.md"),
        help="Path to project rules Markdown file (default: PROJECT_RULES.md)",
    )
    parser.add_argument("--github-owner", required=True, help="GitHub org or user")
    parser.add_argument("--github-repo", required=True, help="GitHub repository name")
    parser.add_argument(
        "--max-prs",
        type=int,
        default=100,
        help="Maximum number of past closed PRs to ingest (default: 100)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print chunks without writing to the database",
    )
    args = parser.parse_args()

    if not args.rules_file.exists():
        logger.error("Rules file not found: %s", args.rules_file)
        sys.exit(1)

    asyncio.run(
        ingest(
            rules_path=args.rules_file,
            github_owner=args.github_owner,
            github_repo=args.github_repo,
            max_prs=args.max_prs,
            dry_run=args.dry_run,
        )
    )


if __name__ == "__main__":
    main()
