"""
Unit tests for the three lenses.

All external calls (watsonx.ai generate, RAG search, httpx registry) are mocked.
No live credentials required.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from app.lenses.models import Finding, LensResult, Severity

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

SAMPLE_DIFF = """\
diff --git a/app/routes/users.py b/app/routes/users.py
--- a/app/routes/users.py
+++ b/app/routes/users.py
@@ -10,6 +10,10 @@
+@router.get("/users")
+async def list_users(db: Session = Depends(get_db)):
+    return db.query(User).all()
"""

SAMPLE_FILES = [
    {"filename": "app/routes/users.py", "patch": "@@ -10 +10 @@\n+return db.query(User).all()"},
]

OPENAPI_FILES = [
    {
        "filename": "openapi.yaml",
        "patch": "-  /users/{id}:\n+  # removed endpoint",
    },
    {"filename": "app/main.py", "patch": "+app = FastAPI()"},
]


def _make_llm_response(findings: list[dict]) -> str:
    return json.dumps(findings)


def _critical(msg: str, file: str = "app/main.py", line: int = 5) -> dict:
    return {"severity": "critical", "message": msg, "file": file, "line": line, "rule_section": None}


def _warning(msg: str) -> dict:
    return {"severity": "warning", "message": msg, "file": None, "line": None, "rule_section": None}


# ---------------------------------------------------------------------------
# models.py — LensResult helpers
# ---------------------------------------------------------------------------

def test_lens_result_critical_count():
    result = LensResult(
        lens_name="Test",
        passed=False,
        findings=[
            Finding(severity=Severity.CRITICAL, message="A"),
            Finding(severity=Severity.WARNING, message="B"),
            Finding(severity=Severity.CRITICAL, message="C"),
        ],
    )
    assert result.critical_count == 2
    assert result.warning_count == 1


def test_lens_result_passed_empty():
    result = LensResult(lens_name="Test", passed=True, findings=[])
    assert result.passed is True
    assert result.critical_count == 0


# ---------------------------------------------------------------------------
# Regression Lens
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_regression_lens_clean_diff():
    """Empty findings array → passed=True."""
    with (
        patch("app.lenses.regression_lens.search", AsyncMock(return_value=[])),
        patch("app.lenses.regression_lens.generate", AsyncMock(return_value="[]")),
    ):
        from app.lenses import regression_lens
        result = await regression_lens.run(SAMPLE_DIFF, SAMPLE_FILES)
    assert result.passed is True
    assert result.findings == []
    assert result.lens_name == "Regression Lens"


@pytest.mark.asyncio
async def test_regression_lens_critical_finding():
    findings = [
        {
            "severity": "critical",
            "message": "Raw DB model returned — violates API Design Rule",
            "file": "app/routes/users.py",
            "line": 12,
            "rule_section": "1. API Design Rules",
        }
    ]
    with (
        patch("app.lenses.regression_lens.search", AsyncMock(return_value=[])),
        patch("app.lenses.regression_lens.generate", AsyncMock(return_value=json.dumps(findings))),
    ):
        from app.lenses import regression_lens
        result = await regression_lens.run(SAMPLE_DIFF, SAMPLE_FILES)
    assert result.passed is False
    assert result.critical_count == 1
    assert result.findings[0].rule_section == "1. API Design Rules"


@pytest.mark.asyncio
async def test_regression_lens_rag_context_used():
    """Verify search() is called with a non-empty query."""
    mock_search = AsyncMock(return_value=[])
    with (
        patch("app.lenses.regression_lens.search", mock_search),
        patch("app.lenses.regression_lens.generate", AsyncMock(return_value="[]")),
    ):
        from app.lenses import regression_lens
        await regression_lens.run(SAMPLE_DIFF, SAMPLE_FILES)
    mock_search.assert_called_once()
    call_query = mock_search.call_args[0][0]
    assert len(call_query) > 0


@pytest.mark.asyncio
async def test_regression_lens_malformed_json():
    """Non-JSON LLM response → empty findings, passed=True."""
    with (
        patch("app.lenses.regression_lens.search", AsyncMock(return_value=[])),
        patch("app.lenses.regression_lens.generate", AsyncMock(return_value="Sorry, I cannot help.")),
    ):
        from app.lenses import regression_lens
        result = await regression_lens.run(SAMPLE_DIFF, SAMPLE_FILES)
    assert result.findings == []
    assert result.passed is True


# ---------------------------------------------------------------------------
# API Contract Lens
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_api_contract_lens_finds_spec_in_files():
    """Spec detected in changed files → no registry fetch needed."""
    findings = [_critical("Endpoint /users/{id} removed without versioning", "openapi.yaml", 2)]
    with (
        patch("app.lenses.api_contract_lens.generate", AsyncMock(return_value=json.dumps(findings))),
        patch("app.lenses.api_contract_lens.get_settings") as mock_settings,
    ):
        mock_settings.return_value.external_api_registry_url = ""
        mock_settings.return_value.watsonx_api_key = "k"
        mock_settings.return_value.watsonx_url = "u"
        mock_settings.return_value.watsonx_project_id = "p"
        mock_settings.return_value.watsonx_llm_model_id = "m"
        from app.lenses import api_contract_lens
        result = await api_contract_lens.run(SAMPLE_DIFF, OPENAPI_FILES)
    assert result.passed is False
    assert result.critical_count == 1
    assert result.findings[0].file == "openapi.yaml"


@pytest.mark.asyncio
async def test_api_contract_lens_no_spec_no_registry():
    """No spec in files, registry URL empty → contract_context is None → clean result."""
    with (
        patch("app.lenses.api_contract_lens.generate", AsyncMock(return_value="[]")),
        patch("app.lenses.api_contract_lens.get_settings") as mock_settings,
    ):
        mock_settings.return_value.external_api_registry_url = ""
        from app.lenses import api_contract_lens
        result = await api_contract_lens.run(SAMPLE_DIFF, SAMPLE_FILES)
    assert result.passed is True
    assert result.findings == []


@pytest.mark.asyncio
async def test_api_contract_lens_registry_fallback(httpx_mock):
    """No spec in files, registry configured → fetch registry content."""
    httpx_mock.add_response(
        url="https://registry.example.com/specs",
        text='{"paths": {"/users": {}}}',
        status_code=200,
    )
    with (
        patch("app.lenses.api_contract_lens.generate", AsyncMock(return_value="[]")),
        patch("app.lenses.api_contract_lens.get_settings") as mock_settings,
    ):
        mock_settings.return_value.external_api_registry_url = "https://registry.example.com/specs"
        from app.lenses import api_contract_lens
        result = await api_contract_lens.run(SAMPLE_DIFF, SAMPLE_FILES)
    assert result.passed is True


@pytest.mark.asyncio
async def test_api_contract_lens_malformed_json():
    with (
        patch("app.lenses.api_contract_lens.generate", AsyncMock(return_value="not json")),
        patch("app.lenses.api_contract_lens.get_settings") as mock_settings,
    ):
        mock_settings.return_value.external_api_registry_url = ""
        from app.lenses import api_contract_lens
        result = await api_contract_lens.run(SAMPLE_DIFF, SAMPLE_FILES)
    assert result.findings == []


# ---------------------------------------------------------------------------
# Backend Pitfall Lens
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_backend_pitfall_lens_clean():
    with patch("app.lenses.backend_pitfall_lens.generate", AsyncMock(return_value="[]")):
        from app.lenses import backend_pitfall_lens
        result = await backend_pitfall_lens.run(SAMPLE_DIFF, SAMPLE_FILES)
    assert result.passed is True
    assert result.findings == []
    assert result.lens_name == "Backend Pitfall Lens"


@pytest.mark.asyncio
async def test_backend_pitfall_lens_multiple_categories():
    findings = [
        _critical("N+1 query detected — missing select_related", "app/routes/users.py", 12),
        _warning("Missing authentication dependency on /users endpoint"),
        {"severity": "critical", "message": "CORS set to *", "file": "app/main.py", "line": 3, "rule_section": None},
    ]
    with patch("app.lenses.backend_pitfall_lens.generate", AsyncMock(return_value=json.dumps(findings))):
        from app.lenses import backend_pitfall_lens
        result = await backend_pitfall_lens.run(SAMPLE_DIFF, SAMPLE_FILES)
    assert result.passed is False
    assert result.critical_count == 2
    assert result.warning_count == 1


@pytest.mark.asyncio
async def test_backend_pitfall_lens_only_warnings_passes():
    """Warnings alone do not fail the lens."""
    findings = [_warning("Consider adding request ID to error responses")]
    with patch("app.lenses.backend_pitfall_lens.generate", AsyncMock(return_value=json.dumps(findings))):
        from app.lenses import backend_pitfall_lens
        result = await backend_pitfall_lens.run(SAMPLE_DIFF, SAMPLE_FILES)
    assert result.passed is True
    assert result.warning_count == 1


@pytest.mark.asyncio
async def test_backend_pitfall_lens_malformed_json():
    with patch("app.lenses.backend_pitfall_lens.generate", AsyncMock(return_value="{broken")):
        from app.lenses import backend_pitfall_lens
        result = await backend_pitfall_lens.run(SAMPLE_DIFF, SAMPLE_FILES)
    assert result.findings == []
