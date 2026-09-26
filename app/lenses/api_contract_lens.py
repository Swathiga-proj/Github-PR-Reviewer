"""
API Contract Lens — validates PR changes against OpenAPI/Swagger contracts.

Strategy:
1. Check changed files for an OpenAPI/Swagger spec (*.yaml / *.json with openapi/swagger keys).
2. If none found in the diff, query the external API registry (if configured).
3. Send the diff + any contract context to Granite for contract violation analysis.
"""
from __future__ import annotations

import json
import logging
import re

import httpx

from app.config import get_settings
from app.lenses.models import Finding, LensResult, Severity
from app.lenses.watsonx_client import generate

logger = logging.getLogger(__name__)

LENS_NAME = "API Contract Lens"

_OPENAPI_FILENAMES = re.compile(
    r"(openapi|swagger|api[-_]spec|api[-_]contract)\.(ya?ml|json)$",
    re.IGNORECASE,
)

_SYSTEM_PROMPT = """\
You are an API contract reviewer. You will be given a pull request diff and optionally
the relevant OpenAPI/Swagger contract or API registry entries.

Your task: identify any API contract violations in the diff and return ONLY a JSON array.
Each finding must be a JSON object with these exact keys:
  "severity" : one of "critical", "warning", "info"
  "message"  : concise description of the contract violation
  "file"     : filename (string or null)
  "line"     : line number in the diff (integer or null)
  "rule_section" : null  (not applicable for API Contract lens)

Classify as "critical" if:
- An existing API field or endpoint is removed or renamed without versioning.
- A required field becomes optional or vice versa.
- Response schema changes break backward compatibility.

Classify as "warning" if:
- A new required field is added to an existing request body.
- HTTP status codes change without documentation update.
- Deprecation markers are missing.

Return [] if no contract issues are found.
Do NOT include any explanation outside the JSON array.
"""


def _extract_spec_from_files(files: list[dict]) -> str | None:
    """Return the patch content of the first recognised OpenAPI spec file, or None."""
    for f in files:
        filename = f.get("filename", "")
        if _OPENAPI_FILENAMES.search(filename):
            patch = f.get("patch", "")
            if patch:
                logger.info("API Contract lens: found spec file %s", filename)
                return f"File: {filename}\n{patch}"
    return None


async def _fetch_from_registry(registry_url: str) -> str | None:
    """Fetch API contract info from an external registry. Returns text or None."""
    if not registry_url:
        return None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(registry_url)
            if resp.is_success:
                logger.info("API Contract lens: fetched registry from %s", registry_url)
                return resp.text[:3000]
            logger.warning(
                "API Contract lens: registry returned %d", resp.status_code
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("API Contract lens: registry fetch failed — %s", exc)
    return None


def _build_prompt(diff: str, contract_context: str | None) -> str:
    ctx_section = (
        f"## API Contract / Spec\n\n{contract_context}\n\n"
        if contract_context
        else "## API Contract / Spec\n\nNone found in diff or registry.\n\n"
    )
    return (
        f"{_SYSTEM_PROMPT}\n\n"
        f"{ctx_section}"
        f"## Pull Request Diff\n\n```diff\n{diff[:6000]}\n```\n\n"
        f"## Findings (JSON array only):"
    )


def _parse_findings(raw: str) -> list[Finding]:
    match = re.search(r"\[.*\]", raw, re.DOTALL)
    if not match:
        logger.warning("API Contract lens: no JSON array found in LLM response")
        return []
    try:
        items = json.loads(match.group())
    except json.JSONDecodeError as exc:
        logger.warning("API Contract lens: failed to parse JSON — %s", exc)
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
            logger.warning("API Contract lens: skipping malformed finding — %s", exc)
    return findings


async def run(diff: str, files: list[dict]) -> LensResult:
    """
    Run the API Contract Lens against a PR diff.

    1. Look for a spec file in the changed files.
    2. Fall back to the external registry if configured and no spec found.
    3. Prompt Granite with diff + contract context.
    """
    contract_context = _extract_spec_from_files(files)

    if contract_context is None:
        registry_url = get_settings().external_api_registry_url
        contract_context = await _fetch_from_registry(registry_url)

    prompt = _build_prompt(diff, contract_context)
    raw_response = await generate(prompt)

    findings = _parse_findings(raw_response)
    passed = all(f.severity != Severity.CRITICAL for f in findings)

    logger.info(
        "API Contract lens: %d findings (%d critical)",
        len(findings),
        sum(1 for f in findings if f.severity == Severity.CRITICAL),
    )
    return LensResult(lens_name=LENS_NAME, passed=passed, findings=findings)
