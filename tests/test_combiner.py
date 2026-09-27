"""
Unit tests for app.combiner.combiner.

Covers:
- compute_risk_score  — scoring formula, cap, edge cases
- build_summary_comment — structure, badge, table, findings, footer
- build_inline_comments — file+line filtering, body format, rule note
- run_review_pipeline  — happy path + error paths (fetch fail, lens fail)
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.lenses.models import Finding, LensResult, Severity
from app.combiner.combiner import (
    build_inline_comments,
    build_summary_comment,
    compute_risk_score,
    run_review_pipeline,
)

# ---------------------------------------------------------------------------
# Fixtures — pre-built LensResult objects
# ---------------------------------------------------------------------------

def _make_result(
    name: str,
    criticals: int = 0,
    warnings: int = 0,
    infos: int = 0,
    with_location: bool = False,
    rule_section: str | None = None,
) -> LensResult:
    findings = []
    for i in range(criticals):
        findings.append(Finding(
            severity=Severity.CRITICAL,
            message=f"Critical issue {i+1}",
            file="app/main.py" if with_location else None,
            line=(i + 1) if with_location else None,
            rule_section=rule_section,
        ))
    for i in range(warnings):
        findings.append(Finding(
            severity=Severity.WARNING,
            message=f"Warning {i+1}",
            file="app/routes.py" if with_location else None,
            line=(10 + i) if with_location else None,
        ))
    for _ in range(infos):
        findings.append(Finding(severity=Severity.INFO, message="Minor note"))
    passed = criticals == 0
    return LensResult(lens_name=name, passed=passed, findings=findings)


# ---------------------------------------------------------------------------
# compute_risk_score
# ---------------------------------------------------------------------------

def test_risk_score_zero_findings():
    results = [
        _make_result("Regression Lens"),
        _make_result("API Contract Lens"),
        _make_result("Backend Pitfall Lens"),
    ]
    assert compute_risk_score(results) == 0


def test_risk_score_single_critical():
    results = [_make_result("Regression Lens", criticals=1)]
    assert compute_risk_score(results) == 20


def test_risk_score_single_warning():
    results = [_make_result("API Contract Lens", warnings=1)]
    assert compute_risk_score(results) == 5


def test_risk_score_mixed():
    # 2 critical (×20) + 3 warnings (×5) = 40 + 15 = 55
    results = [
        _make_result("Regression Lens", criticals=1, warnings=1),
        _make_result("API Contract Lens", criticals=1, warnings=2),
    ]
    assert compute_risk_score(results) == 55


def test_risk_score_capped_at_100():
    # 6 criticals × 20 = 120 → capped at 100
    results = [_make_result("Backend Pitfall Lens", criticals=6)]
    assert compute_risk_score(results) == 100


def test_risk_score_infos_not_counted():
    results = [_make_result("Regression Lens", infos=10)]
    assert compute_risk_score(results) == 0


def test_risk_score_equally_weighted():
    """Each lens contributes equally — same weights regardless of lens name."""
    r1 = _make_result("Lens A", criticals=1)
    r2 = _make_result("Lens B", criticals=1)
    r3 = _make_result("Lens C", criticals=1)
    assert compute_risk_score([r1, r2, r3]) == 60


# ---------------------------------------------------------------------------
# build_summary_comment
# ---------------------------------------------------------------------------

def test_summary_comment_low_risk():
    results = [_make_result("Regression Lens")]
    comment = build_summary_comment(results, 0)
    assert "🟢" in comment
    assert "Low" in comment
    assert "0/100" in comment


def test_summary_comment_medium_risk():
    results = [_make_result("Regression Lens", warnings=4)]
    comment = build_summary_comment(results, 20)
    assert "🟢" in comment  # 20 is still Low


def test_summary_comment_high_risk():
    results = [_make_result("Regression Lens", criticals=3)]
    comment = build_summary_comment(results, 60)
    assert "🟡" in comment
    assert "Medium" in comment


def test_summary_comment_critical_risk():
    results = [_make_result("Regression Lens", criticals=4)]
    comment = build_summary_comment(results, 80)
    assert "🔴" in comment
    assert "High" in comment


def test_summary_comment_contains_lens_table():
    results = [
        _make_result("Regression Lens", criticals=1),
        _make_result("API Contract Lens"),
        _make_result("Backend Pitfall Lens", warnings=2),
    ]
    comment = build_summary_comment(results, 30)
    assert "Regression Lens" in comment
    assert "API Contract Lens" in comment
    assert "Backend Pitfall Lens" in comment
    assert "❌ Fail" in comment
    assert "✅ Pass" in comment


def test_summary_comment_top_findings_critical_first():
    results = [
        _make_result("Regression Lens", criticals=1, warnings=1),
    ]
    comment = build_summary_comment(results, 25)
    assert "Top Findings" in comment
    # Critical (🔴) should appear before Warning (🟡)
    assert comment.index("🔴") < comment.index("🟡")


def test_summary_comment_rule_section_shown():
    result = LensResult(
        lens_name="Regression Lens",
        passed=False,
        findings=[
            Finding(
                severity=Severity.CRITICAL,
                message="Raw DB model returned",
                file="app/routes.py",
                line=5,
                rule_section="1. API Design Rules",
            )
        ],
    )
    comment = build_summary_comment([result], 20)
    assert "1. API Design Rules" in comment


def test_summary_comment_footer_all_pass():
    results = [_make_result("Regression Lens")]
    comment = build_summary_comment(results, 0)
    assert "All lenses passed" in comment


def test_summary_comment_footer_some_fail():
    results = [_make_result("Regression Lens", criticals=1)]
    comment = build_summary_comment(results, 20)
    assert "One or more lenses flagged issues" in comment


def test_summary_comment_no_findings_section_when_clean():
    results = [_make_result("Regression Lens")]
    comment = build_summary_comment(results, 0)
    assert "Top Findings" not in comment


# ---------------------------------------------------------------------------
# build_inline_comments
# ---------------------------------------------------------------------------

def test_inline_comments_skips_no_location():
    result = _make_result("Regression Lens", criticals=2, warnings=1)  # no location
    assert build_inline_comments([result]) == []


def test_inline_comments_includes_file_and_line():
    result = _make_result("Regression Lens", criticals=1, with_location=True)
    comments = build_inline_comments([result])
    assert len(comments) == 1
    assert comments[0]["path"] == "app/main.py"
    assert comments[0]["position"] == 1


def test_inline_comments_body_contains_lens_name():
    result = _make_result("Backend Pitfall Lens", warnings=1, with_location=True)
    comments = build_inline_comments([result])
    assert "Backend Pitfall Lens" in comments[0]["body"]


def test_inline_comments_rule_section_in_body():
    result = LensResult(
        lens_name="Regression Lens",
        passed=False,
        findings=[
            Finding(
                severity=Severity.CRITICAL,
                message="Missing auth",
                file="app/routes.py",
                line=42,
                rule_section="6. Security Rules",
            )
        ],
    )
    comments = build_inline_comments([result])
    assert len(comments) == 1
    assert "6. Security Rules" in comments[0]["body"]
    assert comments[0]["position"] == 42


def test_inline_comments_mixed_location():
    """Only findings with both file AND line are included."""
    result = LensResult(
        lens_name="API Contract Lens",
        passed=False,
        findings=[
            Finding(severity=Severity.CRITICAL, message="No file", file=None, line=None),
            Finding(severity=Severity.WARNING, message="Has file, no line", file="app/api.py", line=None),
            Finding(severity=Severity.CRITICAL, message="Both", file="app/api.py", line=10),
        ],
    )
    comments = build_inline_comments([result])
    assert len(comments) == 1
    assert comments[0]["path"] == "app/api.py"


# ---------------------------------------------------------------------------
# run_review_pipeline — integration (all external calls mocked)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pipeline_happy_path():
    """Full pipeline runs without error and posts review + comment."""
    clean_result = _make_result("Regression Lens")
    mock_gh = AsyncMock()
    mock_gh.__aenter__ = AsyncMock(return_value=mock_gh)
    mock_gh.__aexit__ = AsyncMock(return_value=False)
    mock_gh.get_pr_diff = AsyncMock(return_value="diff --git a/foo.py")
    mock_gh.get_pr_files = AsyncMock(return_value=[])
    mock_gh.post_pr_review = AsyncMock()
    mock_gh.post_issue_comment = AsyncMock()

    with (
        patch("app.combiner.combiner.GithubClient", return_value=mock_gh),
        patch("app.combiner.combiner.regression_lens.run", AsyncMock(return_value=clean_result)),
        patch("app.combiner.combiner.api_contract_lens.run", AsyncMock(return_value=clean_result)),
        patch("app.combiner.combiner.backend_pitfall_lens.run", AsyncMock(return_value=clean_result)),
    ):
        await run_review_pipeline("org", "repo", 1)

    mock_gh.post_pr_review.assert_called_once()
    mock_gh.post_issue_comment.assert_not_called()  # removed duplicate post


@pytest.mark.asyncio
async def test_pipeline_fetch_failure_does_not_raise():
    """If GitHub fetch fails, pipeline logs and returns without crashing."""
    mock_gh = AsyncMock()
    mock_gh.__aenter__ = AsyncMock(return_value=mock_gh)
    mock_gh.__aexit__ = AsyncMock(return_value=False)
    mock_gh.get_pr_diff = AsyncMock(side_effect=Exception("GitHub down"))
    mock_gh.get_pr_files = AsyncMock(return_value=[])

    with patch("app.combiner.combiner.GithubClient", return_value=mock_gh):
        # Should not raise
        await run_review_pipeline("org", "repo", 99)

    mock_gh.post_pr_review.assert_not_called()


@pytest.mark.asyncio
async def test_pipeline_lens_failure_does_not_raise():
    """If a lens raises, pipeline logs and returns without crashing."""
    mock_gh = AsyncMock()
    mock_gh.__aenter__ = AsyncMock(return_value=mock_gh)
    mock_gh.__aexit__ = AsyncMock(return_value=False)
    mock_gh.get_pr_diff = AsyncMock(return_value="diff text")
    mock_gh.get_pr_files = AsyncMock(return_value=[])

    with (
        patch("app.combiner.combiner.GithubClient", return_value=mock_gh),
        patch("app.combiner.combiner.regression_lens.run", AsyncMock(side_effect=Exception("LLM error"))),
        patch("app.combiner.combiner.api_contract_lens.run", AsyncMock(side_effect=Exception("LLM error"))),
        patch("app.combiner.combiner.backend_pitfall_lens.run", AsyncMock(side_effect=Exception("LLM error"))),
    ):
        await run_review_pipeline("org", "repo", 5)

    mock_gh.post_pr_review.assert_not_called()


@pytest.mark.asyncio
async def test_pipeline_post_failure_does_not_raise():
    """If GitHub post fails, pipeline logs and returns without crashing."""
    clean_result = _make_result("Regression Lens")
    mock_gh = AsyncMock()
    mock_gh.__aenter__ = AsyncMock(return_value=mock_gh)
    mock_gh.__aexit__ = AsyncMock(return_value=False)
    mock_gh.get_pr_diff = AsyncMock(return_value="diff text")
    mock_gh.get_pr_files = AsyncMock(return_value=[])
    mock_gh.post_pr_review = AsyncMock(side_effect=Exception("403 Forbidden"))
    mock_gh.post_issue_comment = AsyncMock()

    with (
        patch("app.combiner.combiner.GithubClient", return_value=mock_gh),
        patch("app.combiner.combiner.regression_lens.run", AsyncMock(return_value=clean_result)),
        patch("app.combiner.combiner.api_contract_lens.run", AsyncMock(return_value=clean_result)),
        patch("app.combiner.combiner.backend_pitfall_lens.run", AsyncMock(return_value=clean_result)),
    ):
        await run_review_pipeline("org", "repo", 7)
    # No unhandled exception — just logged
