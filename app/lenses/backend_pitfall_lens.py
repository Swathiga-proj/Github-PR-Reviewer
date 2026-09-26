"""
Backend Pitfall Lens — detects common backend anti-patterns in a PR diff.

Detects five categories:
  1. N+1 query patterns
  2. Missing authentication / authorisation
  3. Insecure defaults (CORS *, debug=True, hardcoded secrets, etc.)
  4. Poor error handling (bare except, exposed internals, missing logging)
  5. Dependency / version drift (pinned to vulnerable or unpinned versions)
"""
from __future__ import annotations

import json
import logging
import re

from app.lenses.models import Finding, LensResult, Severity
from app.lenses.watsonx_client import generate

logger = logging.getLogger(__name__)

LENS_NAME = "Backend Pitfall Lens"

_SYSTEM_PROMPT = """\
You are a backend security and reliability reviewer. Analyse the pull request diff below
and identify backend anti-patterns across these five categories:

1. N+1 Queries — ORM loops that execute a query per iteration, missing select_related/prefetch_related.
2. Missing Auth/AuthZ — endpoints or functions that lack authentication or authorisation checks.
3. Insecure Defaults — CORS set to *, DEBUG=True in non-test code, hardcoded secrets or tokens,
   disabled SSL verification, overly permissive file permissions.
4. Poor Error Handling — bare `except:` clauses, internal exception messages exposed to clients,
   missing logging, swallowed exceptions, no request ID in error responses.
5. Dependency/Version Drift — unpinned dependency versions (e.g. `requests` with no pin),
   known-vulnerable version ranges, mixing direct and transitive dependency overrides.

Return ONLY a JSON array of findings. Each finding is a JSON object with:
  "severity"     : "critical" | "warning" | "info"
  "message"      : concise description of the issue
  "file"         : filename string or null
  "line"         : integer line number in the diff or null
  "rule_section" : null  (not used by this lens)

Severity guide:
  critical — security vulnerability, data loss risk, or guaranteed production failure.
  warning  — likely bug, reliability issue, or best-practice violation.
  info     — minor suggestion or style improvement.

Return [] if no issues are found.
Do NOT include any text outside the JSON array.
"""


def _build_prompt(diff: str) -> str:
    return (
        f"{_SYSTEM_PROMPT}\n\n"
        f"## Pull Request Diff\n\n```diff\n{diff[:6000]}\n```\n\n"
        f"## Findings (JSON array only):"
    )


def _parse_findings(raw: str) -> list[Finding]:
    match = re.search(r"\[.*\]", raw, re.DOTALL)
    if not match:
        logger.warning("Backend Pitfall lens: no JSON array found in LLM response")
        return []
    try:
        items = json.loads(match.group())
    except json.JSONDecodeError as exc:
        logger.warning("Backend Pitfall lens: failed to parse JSON — %s", exc)
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
                    rule_section=None,
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Backend Pitfall lens: skipping malformed finding — %s", exc)
    return findings


async def run(diff: str, files: list[dict]) -> LensResult:
    """
    Run the Backend Pitfall Lens against a PR diff.
    Prompts Granite to detect anti-patterns across all five categories.
    """
    prompt = _build_prompt(diff)
    raw_response = await generate(prompt)

    findings = _parse_findings(raw_response)
    passed = all(f.severity != Severity.CRITICAL for f in findings)

    logger.info(
        "Backend Pitfall lens: %d findings (%d critical)",
        len(findings),
        sum(1 for f in findings if f.severity == Severity.CRITICAL),
    )
    return LensResult(lens_name=LENS_NAME, passed=passed, findings=findings)
