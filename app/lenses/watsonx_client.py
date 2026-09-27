"""
Thin async wrapper around ibm-watsonx-ai for text generation.

Usage
-----
response_text = await generate(prompt)
"""
from __future__ import annotations

import asyncio
import logging
from functools import lru_cache

from ibm_watsonx_ai import Credentials
from ibm_watsonx_ai.foundation_models import ModelInference

from app.config import get_settings

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _get_model() -> ModelInference:
    settings = get_settings()
    return ModelInference(
        model_id=settings.watsonx_llm_model_id,
        credentials=Credentials(
            api_key=settings.watsonx_api_key,
            url=settings.watsonx_url,
        ),
        project_id=settings.watsonx_project_id,
        params={
            "max_tokens": 1024,
            "temperature": 0.0,        # deterministic output for code review
            "repetition_penalty": 1.05,
        },
        # Disable SDK-level retries — we handle errors at the pipeline level.
        # Auto-retries on 429 hammer the rate limit further on Lite plans.
        max_retries=0,
    )


async def generate(prompt: str) -> str:
    """
    Send *prompt* to Granite and return the generated text.
    Uses the chat API (/ml/v1/text/chat) — the generate_text API is deprecated.
    The ibm-watsonx-ai SDK call is synchronous; we run it in a thread
    executor so it does not block the event loop.
    """
    model = _get_model()
    loop = asyncio.get_event_loop()
    response = await loop.run_in_executor(
        None,
        lambda: model.chat(
            messages=[{"role": "user", "content": prompt}],
        ),
    )
    # chat() returns: {"choices": [{"message": {"content": "..."}}]}
    text = response["choices"][0]["message"]["content"]
    logger.debug("watsonx generate: %d chars returned", len(text))
    return text
