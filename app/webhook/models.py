"""
Pydantic models for the GitHub webhook payload.
Only the fields we actually use are declared; extra fields are ignored.
"""
from __future__ import annotations

from pydantic import BaseModel


class PRUser(BaseModel):
    login: str


class PRHead(BaseModel):
    sha: str


class PullRequest(BaseModel):
    number: int
    title: str
    user: PRUser
    head: PRHead


class Repository(BaseModel):
    name: str
    full_name: str
    owner: PRUser


class PullRequestEvent(BaseModel):
    """GitHub pull_request webhook payload (only fields we need)."""

    action: str
    number: int
    pull_request: PullRequest
    repository: Repository

    @property
    def owner(self) -> str:
        return self.repository.owner.login

    @property
    def repo(self) -> str:
        return self.repository.name

    @property
    def pull_number(self) -> int:
        return self.number
