"""
Shared pytest fixtures for the PR Reviewer test suite.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def anyio_backend():
    return "asyncio"
