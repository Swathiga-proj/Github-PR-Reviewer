"""
FastAPI application entry point.
"""
from __future__ import annotations

import logging

from fastapi import FastAPI

from app.webhook.router import router as webhook_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)

app = FastAPI(
    title="GitHub PR Reviewer",
    description="Automated PR analysis using watsonx.ai — Regression, API Contract, and Backend Pitfall lenses.",
    version="0.1.0",
)

app.include_router(webhook_router)


@app.get("/health", tags=["ops"])
async def health() -> dict:
    """Liveness check."""
    return {"status": "ok"}
