"""Application settings, loaded from environment variables."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the backend service.

    Values are read from environment variables (or a `.env` file in dev).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="MBP_",
        extra="ignore",
    )

    app_name: str = "mbp-backend"
    env: str = "development"
    log_level: str = "INFO"

    # Firebase
    firebase_project_id: str | None = None
    firebase_credentials_path: str | None = None  # path to service account JSON

    # KMS
    kms_provider: str | None = None  # e.g. "gcp", "aws", "none"
    kms_key_id: str | None = None

    # FX provider
    fx_provider: str = "exchangerate.host"
    fx_provider_api_key: str | None = None

    # Broker gateway hosts (sidecars)
    ib_gateway_host: str = "localhost"
    ib_gateway_port: int = 5000
    futu_opend_host: str = "localhost"
    futu_opend_port: int = 11111

    # CORS
    cors_origins: list[str] = Field(default_factory=lambda: ["*"])

    # Futu RSA key for encrypted cross-network trade connections
    futu_conn_key_path: str | None = None

    # Futu trade unlock password — server-side fallback when not supplied by client
    futu_trade_unlock_password: str | None = None  # MBP_FUTU_TRADE_UNLOCK_PASSWORD

    # Auth toggle for local/test environments
    auth_disabled: bool = False

    # Gmail digest (watchlist daily email)
    gmail_from_email: str | None = None      # MBP_GMAIL_FROM_EMAIL
    gmail_app_password: str | None = None    # MBP_GMAIL_APP_PASSWORD
    gmail_digest_recipient: str | None = None  # fallback if user email unknown


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()
