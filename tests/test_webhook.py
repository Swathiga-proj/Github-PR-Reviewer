"""
Integration tests for the POST /webhook endpoint.

Tests cover:
- HMAC signature verification (missing, invalid, valid)
- Event filtering (non-PR events, non-opened actions)
- Valid pull_request opened → 202 + background task dispatched
- Malformed payload → 400
- Health check endpoint
"""
from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from app.main import app

WEBHOOK_SECRET = "test-secret"
OWNER = "test-org"
REPO = "test-repo"
PR_NUMBER = 7


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sign(body: bytes, secret: str = WEBHOOK_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _pr_payload(action: str = "opened") -> dict:
    return {
        "action": action,
        "number": PR_NUMBER,
        "pull_request": {
            "number": PR_NUMBER,
            "title": "Test PR",
            "user": {"login": OWNER},
            "head": {"sha": "abc123"},
        },
        "repository": {
            "name": REPO,
            "full_name": f"{OWNER}/{REPO}",
            "owner": {"login": OWNER},
        },
    }


def _post_webhook(
    client: TestClient,
    payload: dict,
    event: str = "pull_request",
    secret: str = WEBHOOK_SECRET,
    omit_signature: bool = False,
) -> object:
    body = json.dumps(payload).encode()
    headers = {"X-GitHub-Event": event}
    if not omit_signature:
        headers["X-Hub-Signature-256"] = _sign(body, secret)
    return client.post("/webhook", content=body, headers=headers)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def patch_settings(monkeypatch):
    """Override settings so tests don't need a real .env file."""
    monkeypatch.setenv("GITHUB_TOKEN", "gh-test-token")
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setenv("WATSONX_API_KEY", "wx-key")
    monkeypatch.setenv("WATSONX_PROJECT_ID", "wx-proj")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost/db")
    # Clear lru_cache so patched env vars take effect
    from app.config import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def stub_pipeline(monkeypatch):
    """Prevent the real pipeline from running during webhook tests."""
    async def _noop(owner, repo, pull_number):
        pass
    monkeypatch.setattr("app.combiner.combiner.run_review_pipeline", _noop)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

def test_health(client: TestClient):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# HMAC verification
# ---------------------------------------------------------------------------

def test_missing_signature_returns_403(client: TestClient):
    resp = _post_webhook(client, _pr_payload(), omit_signature=True)
    assert resp.status_code == 403


def test_wrong_signature_returns_403(client: TestClient):
    resp = _post_webhook(client, _pr_payload(), secret="wrong-secret")
    assert resp.status_code == 403


def test_valid_signature_passes(client: TestClient):
    resp = _post_webhook(client, _pr_payload())
    assert resp.status_code == 202


# ---------------------------------------------------------------------------
# Event filtering
# ---------------------------------------------------------------------------

def test_non_pr_event_ignored(client: TestClient):
    payload = {"zen": "Keep it logically awesome.", "hook_id": 1}
    resp = _post_webhook(client, payload, event="ping")
    assert resp.status_code == 202
    assert resp.json()["status"] == "ignored"


def test_pr_action_not_opened_is_ignored(client: TestClient):
    resp = _post_webhook(client, _pr_payload(action="closed"))
    assert resp.status_code == 202
    assert resp.json()["status"] == "ignored"


def test_pr_action_synchronize_is_ignored(client: TestClient):
    resp = _post_webhook(client, _pr_payload(action="synchronize"))
    assert resp.status_code == 202
    assert resp.json()["status"] == "ignored"


# ---------------------------------------------------------------------------
# Valid pull_request opened
# ---------------------------------------------------------------------------

def test_pr_opened_accepted(client: TestClient):
    resp = _post_webhook(client, _pr_payload(action="opened"))
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "accepted"
    assert body["pr"] == PR_NUMBER


# ---------------------------------------------------------------------------
# Malformed payload
# ---------------------------------------------------------------------------

def test_malformed_payload_returns_400(client: TestClient):
    bad_body = b'{"action": "opened", "number": 1}'  # missing required fields
    headers = {
        "X-GitHub-Event": "pull_request",
        "X-Hub-Signature-256": _sign(bad_body),
    }
    resp = client.post("/webhook", content=bad_body, headers=headers)
    assert resp.status_code == 400
