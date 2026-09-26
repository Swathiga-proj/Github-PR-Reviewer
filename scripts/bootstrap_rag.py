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

# ---------------------------------------------------------------------------
# Chunking helpers
# ---------------------------------------------------------------------------

# Matches numbered section headings like "1. API Design Rules"
_SECTION_RE = re.compile(r"^(\d+\.\s+.+)$", re.MULTILINE)


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


def chunk_pr(pr: dict, repo_full_name: str) -> dict:
    """
    Build a single chunk for a closed PR (title + body + diff summary).
    The diff itself is not fetched here to keep bootstrap fast; only metadata
    fields available in the list response are used.
    """
    number = pr.get("number", 0)
    title = pr.get("title", "")
    body = (pr.get("body") or "").strip()
    text = f"PR #{number}: {title}\n\n{body}" if body else f"PR #{number}: {title}"
    return {
        "doc_id": f"pr:{repo_full_name}:{number}",
        "text": text,
        "metadata": {
            "source": "past_pr",
            "pr_number": number,
            "repo": repo_full_name,
            "state": pr.get("state", "closed"),
        },
    }


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
    logger.info(
        "Fetching up to %d closed PRs from %s/%s…",
        max_prs,
        github_owner,
        github_repo,
    )
    async with GithubClient() as gh:
        prs = await gh.list_past_prs(
            github_owner, github_repo,
            per_page=per_page,
            max_pages=max_pages,
        )

    repo_full = f"{github_owner}/{github_repo}"
    pr_chunks = [chunk_pr(pr, repo_full) for pr in prs[:max_prs]]
    logger.info("Ingesting %d PR chunks…", len(pr_chunks))

    for chunk in pr_chunks:
        if dry_run:
            logger.info("[dry-run] Would upsert %s: %s…", chunk["doc_id"], chunk["text"][:80])
        else:
            await upsert_document(chunk["doc_id"], chunk["text"], chunk["metadata"])

    if not dry_run:
        await close_pool()

    logger.info("Bootstrap complete. Rules: %d, PRs: %d", len(rule_chunks), len(pr_chunks))


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
