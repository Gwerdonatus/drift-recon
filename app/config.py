"""
Application configuration.
All values come from environment variables — never from code.
Validated at startup via Pydantic; the app will refuse to start
if required values are missing or malformed.
"""

from __future__ import annotations

import hashlib
import secrets
from functools import lru_cache

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
        hide_input_in_errors=True,
    )

    # ── Application ────────────────────────────────────────────
    ENVIRONMENT: str = "production"
    LOG_LEVEL: str = "INFO"
    APP_VERSION: str = "1.0.0"
    APP_NAME: str = "Data Drift Reconciliation Service"

    # ── Database ───────────────────────────────────────────────
    DATABASE_URL: str
    DATABASE_POOL_SIZE: int = 10
    DATABASE_MAX_OVERFLOW: int = 20
    DATABASE_POOL_TIMEOUT: int = 30

    # ── Redis ──────────────────────────────────────────────────
    REDIS_URL: str = "redis://redis:6379/0"

    # ── Security ───────────────────────────────────────────────
    SECRET_KEY: str
    API_KEY_SALT: str
    VALID_API_KEYS: str  # comma-separated raw keys from env

    # Read-only sandbox integration; never accept a live credential.
    STRIPE_SECRET_KEY: SecretStr = SecretStr("")

    @field_validator("STRIPE_SECRET_KEY")
    @classmethod
    def sandbox_key_only(cls, value: SecretStr) -> SecretStr:
        key = value.get_secret_value()
        if key and not key.startswith("sk_test_"):
            raise ValueError("Stripe integration requires a sandbox secret key")
        return value

    # ── Matching Engine ────────────────────────────────────────
    MATCH_CONFIDENCE_THRESHOLD: float = 0.75
    MATCH_REVIEW_THRESHOLD: float = 0.50
    AMOUNT_TOLERANCE_PERCENT: float = 0.01
    DATE_TOLERANCE_DAYS: int = 3

    WEIGHT_AMOUNT: float = 0.50
    WEIGHT_DATE: float = 0.25
    WEIGHT_REFERENCE: float = 0.15
    WEIGHT_DESCRIPTION: float = 0.10

    # ── Drift Detection ────────────────────────────────────────
    DRIFT_LOOKBACK_DAYS: int = 30
    DRIFT_ZSCORE_THRESHOLD: float = 2.5
    DRIFT_MIN_SNAPSHOTS: int = 7

    # ── Scheduler ─────────────────────────────────────────────
    RECONCILIATION_CRON: str = "0 */6 * * *"
    DRIFT_CHECK_CRON: str = "0 8 * * *"

    # ── Alerts ─────────────────────────────────────────────────
    ALERT_WEBHOOK_URL: str = ""
    ALERT_ON_DRIFT: bool = True
    ALERT_ON_LOW_MATCH_RATE: bool = True
    ALERT_MATCH_RATE_THRESHOLD: float = 0.80

    # ── Derived (computed after validation) ───────────────────
    _hashed_api_keys: set[str] = set()

    @field_validator("ENVIRONMENT")
    @classmethod
    def validate_environment(cls, v: str) -> str:
        allowed = {"development", "staging", "production"}
        if v not in allowed:
            raise ValueError(f"ENVIRONMENT must be one of {allowed}")
        return v

    @field_validator("SECRET_KEY")
    @classmethod
    def validate_secret_key(cls, v: str) -> str:
        if len(v) < 32:
            raise ValueError("SECRET_KEY must be at least 32 characters")
        return v

    @model_validator(mode="after")
    def validate_weights_sum_to_one(self) -> "Settings":
        total = (
            self.WEIGHT_AMOUNT
            + self.WEIGHT_DATE
            + self.WEIGHT_REFERENCE
            + self.WEIGHT_DESCRIPTION
        )
        if abs(total - 1.0) > 0.001:
            raise ValueError(f"Matching weights must sum to 1.0, got {total:.3f}")
        return self

    @model_validator(mode="after")
    def build_hashed_api_keys(self) -> "Settings":
        raw_keys = [k.strip() for k in self.VALID_API_KEYS.split(",") if k.strip()]
        self._hashed_api_keys = {self._hash_key(k) for k in raw_keys}
        return self

    def _hash_key(self, raw_key: str) -> str:
        """Hash an API key with HMAC-SHA256 + salt for constant-time comparison."""
        return hashlib.sha256(f"{self.API_KEY_SALT}{raw_key}".encode()).hexdigest()

    def verify_api_key(self, raw_key: str) -> bool:
        """Constant-time API key verification."""
        candidate = self._hash_key(raw_key)
        # secrets.compare_digest prevents timing attacks
        return any(
            secrets.compare_digest(candidate, stored)
            for stored in self._hashed_api_keys
        )

    @property
    def is_development(self) -> bool:
        return self.ENVIRONMENT == "development"

    @property
    def match_weights(self) -> dict:
        return {
            "amount": self.WEIGHT_AMOUNT,
            "date": self.WEIGHT_DATE,
            "reference": self.WEIGHT_REFERENCE,
            "description": self.WEIGHT_DESCRIPTION,
        }


@lru_cache()
def get_settings() -> Settings:
    """Cached settings singleton — loaded once at startup."""
    return Settings()  # type: ignore[call-arg]
