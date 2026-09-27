"""
Regression Lens — detects rule violations and regression risks in a PR diff.

Uses RAG to retrieve relevant PROJECT_RULES.md sections and similar past PR
history, then asks Granite to reason over the diff + context.
"""
from __future__ import annotations

import json
import logging
import re

from app.lenses.models import Finding, LensResult, Severity
from app.lenses.watsonx_client import generate
from app.rag.store import search

logger = logging.getLogger(__name__)

LENS_NAME = "Regression Lens"
RAG_TOP_K = 6

_SYSTEM_PROMPT = """\
You are a senior code reviewer enforcing project coding standards and preventing regressions.
You will be given:
1. A pull request diff.
2. Retrieved context from the project rule book and similar past PRs.

Your task: analyse the diff and return ONLY a JSON array of findings.
Each finding must be a JSON object with these exact keys:
  "severity"     : one of "critical", "warning", "info"
  "message"      : concise explanation of the issue
  "file"         : filename (string or null if not specific to one file)
  "line"         : line number in the diff (integer or null)
  "rule_section" : the violated project rule section title (string or null)

Rules:
- "critical" = violates a project rule or is very likely to cause a regression.
- "warning"  = potential problem that needs attention.
- "info"     = minor suggestion.
- If the diff looks clean, return an empty array: []
- Do NOT include any explanation outside the JSON array.
"""


def _build_prompt(diff: str, context_chunks: list[dict]) -> str:
    ctx_text = "\n\n---\n\n".join(
        f"[Context {i + 1} | source={c['metadata'].get('source', '?')} "
        f"section={c['metadata'].get('section', c['metadata'].get('pr_number', '?'))}]\n"
        f"{c['content']}"
        for i, c in enumerate(context_chunks)
    )
    return (
        f"{_SYSTEM_PROMPT}\n\n"
        f"## Retrieved Context\n\n{ctx_text}\n\n"
        f"## Pull Request Diff\n\n```diff\n{diff[:6000]}\n```\n\n"
        f"## Findings (JSON array only):"
    )


def _parse_findings(raw: str) -> list[Finding]:
    """Extract the JSON array from the LLM response and parse into Finding objects."""
    # Grab the first [...] block in the response
    match = re.search(r"\[.*\]", raw, re.DOTALL)
    if not match:
        logger.warning("Regression lens: no JSON array found in LLM response")
        return []
    try:
        items = json.loads(match.group())
    except json.JSONDecodeError as exc:
        logger.warning("Regression lens: failed to parse JSON — %s", exc)
        return []

    findings = []
    for item in items:
        try:
            findings.append(
                Finding(
                    severity=item.get("severity", "info"),
                    message=item.get("message", ""),
                    file=item.get("file"),
                    line=item.get("line"),
                    rule_section=item.get("rule_section"),
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Regression lens: skipping malformed finding — %s", exc)
    return findings


async def run(diff: str, files: list[dict]) -> LensResult:
    """
    Run the Regression Lens against a PR diff.

    1. Embed the diff and retrieve relevant RAG context.
    2. Build a grounded prompt and call Granite.
    3. Parse structured findings.
    """
    # Truncate to ~400 chars to stay within the embed model's 512-token limit
    query = diff[:400]
    context_chunks = await search(query, top_k=RAG_TOP_K)
    logger.info("Regression lens: retrieved %d RAG chunks", len(context_chunks))

    prompt = _build_prompt(diff, context_chunks)
    raw_response = await generate(prompt)

    findings = _parse_findings(raw_response)
    passed = all(f.severity != Severity.CRITICAL for f in findings)

    logger.info(
        "Regression lens: %d findings (%d critical)",
        len(findings),
        sum(1 for f in findings if f.severity == Severity.CRITICAL),
    )
    return LensResult(lens_name=LENS_NAME, passed=passed, findings=findings)
