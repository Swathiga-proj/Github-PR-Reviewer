"""
Unit tests for app.github_client.client using pytest-httpx mock transport.
"""
from __future__ import annotations

import json
import pytest
from pytest_httpx import HTTPXMock

from app.github_client.client import GithubClient, GithubAPIError

OWNER = "test-org"
REPO = "test-repo"
PR_NUMBER = 42
BASE = "https://api.github.com"


@pytest.fixture
def client():
    return GithubClient(token="test-token")


# ---------------------------------------------------------------------------
# get_pr_diff
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_pr_diff_success(httpx_mock: HTTPXMock, client: GithubClient):
    httpx_mock.add_response(
        url=f"{BASE}/repos/{OWNER}/{REPO}/pulls/{PR_NUMBER}",
        text="diff --git a/foo.py b/foo.py\n+new line",
        status_code=200,
    )
    diff = await client.get_pr_diff(OWNER, REPO, PR_NUMBER)
    assert "diff --git" in diff
    assert "+new line" in diff


@pytest.mark.asyncio
async def test_get_pr_diff_error(httpx_mock: HTTPXMock, client: GithubClient):
    httpx_mock.add_response(
        url=f"{BASE}/repos/{OWNER}/{REPO}/pulls/{PR_NUMBER}",
        status_code=404,
        text="Not Found",
    )
    with pytest.raises(GithubAPIError) as exc_info:
        await client.get_pr_diff(OWNER, REPO, PR_NUMBER)
    assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# get_pr_files
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_pr_files_success(httpx_mock: HTTPXMock, client: GithubClient):
    files = [{"filename": "app/main.py", "patch": "@@ -1 +1 @@\n+hello"}]
    httpx_mock.add_response(
        url=f"{BASE}/repos/{OWNER}/{REPO}/pulls/{PR_NUMBER}/files?per_page=100",
        json=files,
        status_code=200,
    )
    result = await client.get_pr_files(OWNER, REPO, PR_NUMBER)
    assert len(result) == 1
    assert result[0]["filename"] == "app/main.py"


@pytest.mark.asyncio
async def test_get_pr_files_error(httpx_mock: HTTPXMock, client: GithubClient):
    httpx_mock.add_response(
        url=f"{BASE}/repos/{OWNER}/{REPO}/pulls/{PR_NUMBER}/files?per_page=100",
        status_code=403,
        text="Forbidden",
    )
    with pytest.raises(GithubAPIError) as exc_info:
        await client.get_pr_files(OWNER, REPO, PR_NUMBER)
    assert exc_info.value.status_code == 403


# ---------------------------------------------------------------------------
# list_past_prs
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_past_prs_single_page(httpx_mock: HTTPXMock, client: GithubClient):
    prs = [{"number": 1, "title": "Fix bug"}, {"number": 2, "title": "Add feature"}]
    httpx_mock.add_response(
        url=f"{BASE}/repos/{OWNER}/{REPO}/pulls?state=closed&per_page=50&page=1",
        json=prs,
        status_code=200,
    )
    result = await client.list_past_prs(OWNER, REPO, per_page=50, max_pages=5)
    assert len(result) == 2
    assert result[0]["number"] == 1


@pytest.mark.asyncio
async def test_list_past_prs_pagination(httpx_mock: HTTPXMock, client: GithubClient):
    page1 = [{"number": i} for i in range(1, 3)]  # 2 items == per_page → fetch page 2
    page2 = [{"number": 3}]  # 1 item < per_page → stop
    httpx_mock.add_response(
        url=f"{BASE}/repos/{OWNER}/{REPO}/pulls?state=closed&per_page=2&page=1",
        json=page1,
        status_code=200,
    )
    httpx_mock.add_response(
        url=f"{BASE}/repos/{OWNER}/{REPO}/pulls?state=closed&per_page=2&page=2",
        json=page2,
        status_code=200,
    )
    result = await client.list_past_prs(OWNER, REPO, per_page=2, max_pages=5)
    assert len(result) == 3


# ---------------------------------------------------------------------------
# post_pr_review
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_post_pr_review_success(httpx_mock: HTTPXMock, client: GithubClient):
    httpx_mock.add_response(
        url=f"{BASE}/repos/{OWNER}/{REPO}/pulls/{PR_NUMBER}/reviews",
        json={"id": 1},
        status_code=200,
    )
    # Should not raise
    await client.post_pr_review(
        OWNER, REPO, PR_NUMBER,
        review_body="## Review",
        comments=[{"path": "app/main.py", "position": 1, "body": "Issue here"}],
    )


@pytest.mark.asyncio
async def test_post_pr_review_error(httpx_mock: HTTPXMock, client: GithubClient):
    httpx_mock.add_response(
        url=f"{BASE}/repos/{OWNER}/{REPO}/pulls/{PR_NUMBER}/reviews",
        status_code=422,
        text="Unprocessable Entity",
    )
    with pytest.raises(GithubAPIError) as exc_info:
        await client.post_pr_review(OWNER, REPO, PR_NUMBER, "body", [])
    assert exc_info.value.status_code == 422


# ---------------------------------------------------------------------------
# post_issue_comment
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_post_issue_comment_success(httpx_mock: HTTPXMock, client: GithubClient):
    httpx_mock.add_response(
        url=f"{BASE}/repos/{OWNER}/{REPO}/issues/{PR_NUMBER}/comments",
        json={"id": 99},
        status_code=201,
    )
    # Should not raise
    await client.post_issue_comment(OWNER, REPO, PR_NUMBER, "Summary comment")


@pytest.mark.asyncio
async def test_post_issue_comment_error(httpx_mock: HTTPXMock, client: GithubClient):
    httpx_mock.add_response(
        url=f"{BASE}/repos/{OWNER}/{REPO}/issues/{PR_NUMBER}/comments",
        status_code=401,
        text="Unauthorized",
    )
    with pytest.raises(GithubAPIError) as exc_info:
        await client.post_issue_comment(OWNER, REPO, PR_NUMBER, "body")
    assert exc_info.value.status_code == 401


# ---------------------------------------------------------------------------
# GithubAPIError message formatting
# ---------------------------------------------------------------------------

def test_github_api_error_message():
    err = GithubAPIError("GET", "/repos/x/y/pulls/1", 404, "Not Found")
    assert "404" in str(err)
    assert "/repos/x/y/pulls/1" in str(err)
    assert err.status_code == 404
