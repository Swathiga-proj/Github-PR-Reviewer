"""
Async GitHub REST API client.

All methods raise GithubAPIError on non-2xx responses.
Use GithubClient as an async context manager or call close() when done.
"""
from __future__ import annotations

import httpx

from app.config import get_settings

GITHUB_API_BASE = "https://api.github.com"


class GithubAPIError(Exception):
    """Raised when the GitHub API returns a non-2xx response."""

    def __init__(self, method: str, url: str, status_code: int, body: str) -> None:
        self.method = method
        self.url = url
        self.status_code = status_code
        self.body = body
        super().__init__(
            f"GitHub API {method} {url} returned {status_code}: {body[:200]}"
        )


class GithubClient:
    """Thin async wrapper around the GitHub REST API."""

    def __init__(self, token: str | None = None) -> None:
        _token = token or get_settings().github_token
        self._client = httpx.AsyncClient(
            base_url=GITHUB_API_BASE,
            headers={
                "Authorization": f"Bearer {_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30.0,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "GithubClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get(self, path: str, **kwargs: object) -> httpx.Response:
        resp = await self._client.get(path, **kwargs)
        if not resp.is_success:
            raise GithubAPIError("GET", path, resp.status_code, resp.text)
        return resp

    async def _post(self, path: str, **kwargs: object) -> httpx.Response:
        resp = await self._client.post(path, **kwargs)
        if not resp.is_success:
            raise GithubAPIError("POST", path, resp.status_code, resp.text)
        return resp

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    async def get_pr_diff(self, owner: str, repo: str, pull_number: int) -> str:
        """Return the unified diff for a pull request as a plain string."""
        resp = await self._client.get(
            f"/repos/{owner}/{repo}/pulls/{pull_number}",
            headers={"Accept": "application/vnd.github.v3.diff"},
        )
        if not resp.is_success:
            raise GithubAPIError(
                "GET",
                f"/repos/{owner}/{repo}/pulls/{pull_number}",
                resp.status_code,
                resp.text,
            )
        return resp.text

    async def get_pr_files(
        self, owner: str, repo: str, pull_number: int
    ) -> list[dict]:
        """Return the list of changed files with patch data for a pull request."""
        resp = await self._get(
            f"/repos/{owner}/{repo}/pulls/{pull_number}/files",
            params={"per_page": 100},
        )
        return resp.json()

    async def list_past_prs(
        self,
        owner: str,
        repo: str,
        state: str = "closed",
        per_page: int = 50,
        max_pages: int = 5,
    ) -> list[dict]:
        """
        Return merged/closed pull requests for a repository.
        Paginates up to max_pages pages (default 250 PRs total).
        """
        results: list[dict] = []
        for page in range(1, max_pages + 1):
            resp = await self._get(
                f"/repos/{owner}/{repo}/pulls",
                params={"state": state, "per_page": per_page, "page": page},
            )
            batch: list[dict] = resp.json()
            results.extend(batch)
            if len(batch) < per_page:
                break
        return results

    async def post_pr_review(
        self,
        owner: str,
        repo: str,
        pull_number: int,
        review_body: str,
        comments: list[dict],
        event: str = "COMMENT",
    ) -> None:
        """
        Post a PR review with optional inline comments.

        Each item in `comments` must contain:
          - path: str        — file path relative to repo root
          - position: int    — line position within the diff hunk
          - body: str        — comment text
        """
        await self._post(
            f"/repos/{owner}/{repo}/pulls/{pull_number}/reviews",
            json={
                "body": review_body,
                "event": event,
                "comments": comments,
            },
        )

    async def post_issue_comment(
        self, owner: str, repo: str, pull_number: int, body: str
    ) -> None:
        """Post a top-level comment on the pull request (issue comment)."""
        await self._post(
            f"/repos/{owner}/{repo}/issues/{pull_number}/comments",
            json={"body": body},
        )
