from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # GitHub
    github_token: str
    github_webhook_secret: str

    # watsonx.ai
    watsonx_api_key: str
    watsonx_project_id: str
    watsonx_url: str = "https://us-south.ml.cloud.ibm.com"
    watsonx_llm_model_id: str = "ibm/granite-4-h-small"
    watsonx_embed_model_id: str = "ibm/granite-embedding-278m-multilingual"

    # PostgreSQL (pgvector)
    database_url: str

    # External API contract registry (fallback)
    external_api_registry_url: str = ""


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached Settings instance. Import and call this everywhere."""
    return Settings()
