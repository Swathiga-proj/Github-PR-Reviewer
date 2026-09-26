"""
FastAPI router for the GitHub webhook endpoint.

POST /webhook
  - Verifies X-Hub-Signature-256 HMAC (returns 403 on mismatch).
  - Accepts only pull_request events with action == "opened".
  - Dispatches the review pipeline as a BackgroundTask and returns 202 immediately.
"""
from __future__ import annotations

import hashlib
import hmac
import logging

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request, status

from app.config import get_settings
from app.webhook.models import PullRequestEvent

logger = logging.getLogger(__name__)

router = APIRouter()


def _verify_signature(body: bytes, signature_header: str | None) -> None:
    """
    Verify the GitHub HMAC-SHA256 webhook signature.
    Raises HTTP 403 if the signature is missing or does not match.
    """
    if signature_header is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Missing X-Hub-Signature-256 header",
        )

    secret = get_settings().github_webhook_secret.encode()
    expected = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()

    if not hmac.compare_digest(expected, signature_header):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid webhook signature",
        )


@router.post("/webhook", status_code=status.HTTP_202_ACCEPTED)
async def github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_github_event: str | None = Header(default=None),
    x_hub_signature_256: str | None = Header(default=None),
) -> dict:
    """
    Receive GitHub webhook events.

    Only pull_request events with action == "opened" trigger the review pipeline.
    All other events are acknowledged with 202 and ignored.
    """
    body = await request.body()

    # --- HMAC verification ---------------------------------------------------
    _verify_signature(body, x_hub_signature_256)

    # --- Event filter --------------------------------------------------------
    if x_github_event != "pull_request":
        logger.debug("Ignoring event type: %s", x_github_event)
        return {"status": "ignored", "reason": f"event={x_github_event}"}

    # --- Parse payload -------------------------------------------------------
    try:
        event = PullRequestEvent.model_validate_json(body)
    except Exception as exc:
        logger.warning("Failed to parse pull_request payload: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid payload: {exc}",
        ) from exc

    if event.action != "opened":
        logger.debug("Ignoring pull_request action: %s", event.action)
        return {"status": "ignored", "reason": f"action={event.action}"}

    logger.info(
        "PR #%d opened in %s — dispatching review pipeline",
        event.pull_number,
        event.repository.full_name,
    )

    # --- Dispatch background review ------------------------------------------
    # Import here to avoid circular imports; combiner is built in Sub-Task 6.
    from app.combiner.combiner import run_review_pipeline  # noqa: PLC0415

    background_tasks.add_task(
        run_review_pipeline,
        owner=event.owner,
        repo=event.repo,
        pull_number=event.pull_number,
    )

    return {"status": "accepted", "pr": event.pull_number}
