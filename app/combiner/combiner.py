"""
Combiner — orchestrates the three lenses and posts results to GitHub.

Public API
----------
run_review_pipeline(owner, repo, pull_number)   — called by the webhook background task
compute_risk_score(results)                     — 0-100 score, all lenses weighted equally
build_summary_comment(results, risk_score)      — polished Markdown top-level comment
build_inline_comments(results)                  — list of GitHub review comment dicts
"""
from __future__ import annotations

import asyncio
import logging

from app.github_client.client import GithubClient
from app.lenses import api_contract_lens, backend_pitfall_lens, regression_lens
from app.lenses.models import LensResult, Severity

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Risk scoring
# ---------------------------------------------------------------------------

# Points contributed per finding, equally across all lenses
_CRITICAL_WEIGHT = 20
_WARNING_WEIGHT = 5


def compute_risk_score(results: list[LensResult]) -> int:
    """
    Derive a 0-100 risk score from the combined lens results.

    Scoring (all lenses weighted equally):
      - Each critical finding adds 20 points.
      - Each warning finding adds 5 points.
    Score is capped at 100.
    """
    total = 0
    for result in results:
        total += result.critical_count * _CRITICAL_WEIGHT
        total += result.warning_count * _WARNING_WEIGHT
    return min(total, 100)


def _risk_badge(score: int) -> str:
    if score <= 30:
        return f"🟢 **Low** ({score}/100)"
    if score <= 60:
        return f"🟡 **Medium** ({score}/100)"
    return f"🔴 **High** ({score}/100)"


# ---------------------------------------------------------------------------
# Summary comment builder
# ---------------------------------------------------------------------------

def build_summary_comment(results: list[LensResult], risk_score: int) -> str:
    """
    Return a polished Markdown string for the top-level PR comment.

    Structure:
      - Risk score badge
      - Per-lens summary table
      - Top findings (critical first, then warnings)
    """
    lines: list[str] = []

    # Header
    lines.append("## 🔍 Automated PR Review")
    lines.append("")
    lines.append(f"**Risk Score:** {_risk_badge(risk_score)}")
    lines.append("")

    # Per-lens summary table
    lines.append("### Lens Summary")
    lines.append("")
    lines.append("| Lens | Status | Critical | Warnings |")
    lines.append("|---|---|---|---|")
    for r in results:
        status = "✅ Pass" if r.passed else "❌ Fail"
        lines.append(f"| {r.lens_name} | {status} | {r.critical_count} | {r.warning_count} |")
    lines.append("")

    # Top findings — critical first, then warnings, up to 10 total
    all_findings = [
        (r.lens_name, f)
        for r in results
        for f in r.findings
        if f.severity in (Severity.CRITICAL, Severity.WARNING)
    ]
    # Sort: critical before warning
    all_findings.sort(key=lambda x: (0 if x[1].severity == Severity.CRITICAL else 1))

    if all_findings:
        lines.append("### Top Findings")
        lines.append("")
        for lens_name, finding in all_findings[:10]:
            icon = "🔴" if finding.severity == Severity.CRITICAL else "🟡"
            location = ""
            if finding.file:
                location = f" — `{finding.file}`"
                if finding.line:
                    location += f":{finding.line}"
            rule = f" _(rule: {finding.rule_section})_" if finding.rule_section else ""
            lines.append(f"- {icon} **[{lens_name}]** {finding.message}{location}{rule}")
        lines.append("")

    # Footer
    overall = "✅ All lenses passed." if all(r.passed for r in results) else "⚠️ One or more lenses flagged issues."
    lines.append(f"---\n_{overall} Powered by IBM watsonx.ai (granite-3-8b-instruct)._")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Inline comment builder
# ---------------------------------------------------------------------------

def build_inline_comments(results: list[LensResult]) -> list[dict]:
    """
    Map findings that have both a file and a line number to GitHub
    PR review comment objects: {path, position, body}.

    `position` here is used as the diff line number (same as `line` in findings).
    GitHub requires this to be within the diff hunk; the LLM is instructed to
    return diff-relative line numbers.
    """
    comments: list[dict] = []
    for result in results:
        for finding in result.findings:
            if finding.file and finding.line:
                icon = "🔴" if finding.severity == Severity.CRITICAL else (
                    "🟡" if finding.severity == Severity.WARNING else "ℹ️"
                )
                rule_note = f"\n> _Rule: {finding.rule_section}_" if finding.rule_section else ""
                body = (
                    f"{icon} **{result.lens_name}** [{finding.severity.value}]\n\n"
                    f"{finding.message}{rule_note}"
                )
                comments.append({
                    "path": finding.file,
                    "position": finding.line,
                    "body": body,
                })
    return comments


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

async def run_review_pipeline(owner: str, repo: str, pull_number: int) -> None:
    """
    Orchestrate the full PR review:
      1. Fetch diff and file list from GitHub.
      2. Run all three lenses in parallel with asyncio.gather.
      3. Compute risk score and build output.
      4. Post inline review + top-level summary comment to GitHub.
    """
    logger.info("Starting review pipeline for %s/%s PR #%d", owner, repo, pull_number)

    async with GithubClient() as gh:
        # Step 1 — fetch PR data
        try:
            diff, files = await asyncio.gather(
                gh.get_pr_diff(owner, repo, pull_number),
                gh.get_pr_files(owner, repo, pull_number),
            )
        except Exception as exc:
            logger.error("Failed to fetch PR data: %s", exc)
            return

        # Step 2 — run three lenses sequentially to respect Lite plan rate limits
        # Switch back to asyncio.gather when on a paid watsonx.ai plan
        results: list[LensResult] = []
        try:
            for coro in [
                regression_lens.run(diff, files),
                api_contract_lens.run(diff, files),
                backend_pitfall_lens.run(diff, files),
            ]:
                results.append(await coro)
        except Exception as exc:
            logger.error("Lens execution failed: %s", exc)
            return

        # Step 3 — combine
        risk_score = compute_risk_score(results)
        summary = build_summary_comment(results, risk_score)
        inline_comments = build_inline_comments(results)

        logger.info(
            "Review complete — risk score: %d, inline comments: %d",
            risk_score,
            len(inline_comments),
        )

        # Step 4 — post to GitHub as a PR review (single post, no duplicate issue comment)
        # Try with inline comments first; fall back to body-only if positions are invalid.
        try:
            await gh.post_pr_review(
                owner, repo, pull_number,
                review_body=summary,
                comments=inline_comments,
            )
            logger.info("Posted PR review with %d inline comments", len(inline_comments))
        except Exception as exc:
            logger.warning(
                "PR review with inline comments failed (%s) — retrying without inline comments", exc
            )
            try:
                await gh.post_pr_review(
                    owner, repo, pull_number,
                    review_body=summary,
                    comments=[],
                )
                logger.info("Posted PR review (no inline comments)")
            except Exception as exc2:
                logger.error("Failed to post PR review: %s", exc2)
