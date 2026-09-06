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

    # FX provider.
    #
    # Frankfurter (ECB-derived, free, no API key) is the default because
    # exchangerate.host retired its free no-key tier in late 2024 and now
    # returns `missing_access_key` — and a failed rate lookup does not
    # error, it contributes 0, which silently erases every foreign-currency
    # holding from net worth and skews every allocation percentage (§8.3).
    # ARCHITECTURE_NOTES §5 and RUNBOOK both already specified frankfurter;
    # this default was the odd one out.
    fx_provider: str = "frankfurter"
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

    # NOTE: there is deliberately no Futu trade-unlock password setting.
    # Reads do not require unlock (§4.3 rule 2, verified against real
    # OpenD), so the password does not exist anywhere in this system —
    # not in settings, not in .env, not in the credential context. That
    # is a structural guarantee, not a policy to remember.

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
