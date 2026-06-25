"""
src/config.py — Application settings.

Loads configuration from environment variables and/or a local .env file using
Pydantic Settings.  Every setting has a type, a default where sensible, and a
clear description.  Required secrets (Telegram, email) are validated at startup
so the process fails fast with a useful error message rather than silently at
notification time.

Usage
-----
    from src.config import get_settings

    settings = get_settings()
    print(settings.base_url)

`get_settings()` is cached with `@lru_cache` so the .env file is parsed only
once per process, even when called from multiple modules.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import (
    AnyHttpUrl,
    EmailStr,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _split_keywords(raw: str) -> list[str]:
    """Split a comma-separated keyword string and strip whitespace."""
    return [kw.strip() for kw in raw.split(",") if kw.strip()]


# ---------------------------------------------------------------------------
# Settings model
# ---------------------------------------------------------------------------

class Settings(BaseSettings):
    """
    Application-wide configuration.

    Environment variable names are the upper-cased field names.
    A .env file in the project root is loaded automatically when running
    locally; in GitHub Actions the values come from repository secrets /
    variables injected as environment variables.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,       # BOT_TOKEN == bot_token
        extra="ignore",             # unknown env vars are silently ignored
        populate_by_name=True,
    )

    # ── Telegram ────────────────────────────────────────────────────────────
    bot_token: SecretStr | None = Field(
        default=None,
        description="Telegram bot HTTP API token (required for notifications).",
    )
    chat_id: str | None = Field(
        default=None,
        description="Telegram chat/user ID to send alerts to.",
    )

    # ── Email (SMTP) ─────────────────────────────────────────────────────────
    email_host: str | None = Field(
        default=None,
        description="SMTP server hostname (e.g. smtp.gmail.com).",
    )
    email_port: Annotated[int, Field(ge=1, le=65535)] = Field(
        default=587,
        description="SMTP port (587 for STARTTLS, 465 for SSL).",
    )
    email_username: str | None = Field(
        default=None,
        description="SMTP login / sender address.",
    )
    email_password: SecretStr | None = Field(
        default=None,
        description="SMTP password or App Password.",
    )
    email_to: str | None = Field(
        default=None,
        description="Recipient e-mail address for alerts.",
    )

    # ── Monitor behaviour ───────────────────────────────────────────────────
    base_url: str = Field(
        default="https://pareekshabhavan.uoc.ac.in/",
        description="Root URL of the Pareeksha Bhavan website.",
    )
    keywords_raw: str = Field(
        default=(
            "Special Examination,Special Exam,One Time Supplementary,"
            "One Time Regular Supplementary,Exhausted Chances,CBCSS,"
            "2020 Admission,B.Sc,Computer Science,Third Semester"
        ),
        alias="KEYWORDS",
        description="Comma-separated list of keywords to match against notifications.",
    )
    last_seen_path: Path = Field(
        default=Path("data/last_seen.json"),
        description="Path to the JSON file that tracks already-seen notifications.",
    )
    pdf_download_dir: Path = Field(
        default=Path("data/pdfs"),
        description="Directory for temporarily downloaded PDFs.",
    )
    request_timeout: Annotated[int, Field(ge=1, le=300)] = Field(
        default=30,
        description="HTTP request timeout in seconds.",
    )
    max_retries: Annotated[int, Field(ge=0, le=10)] = Field(
        default=3,
        description="Number of retry attempts for failed HTTP requests.",
    )
    log_level: str = Field(
        default="INFO",
        description="Logging level: DEBUG | INFO | WARNING | ERROR | CRITICAL.",
    )
    log_file: Path | None = Field(
        default=Path("logs/monitor.log"),
        description="Path to the rotating log file.  Set to empty string to disable.",
    )
    debug_mode: bool = Field(
        default=False,
        description="Enable verbose debug output (overrides log_level to DEBUG).",
    )

    # ── Derived / computed ──────────────────────────────────────────────────
    @property
    def keywords(self) -> list[str]:
        """Return the parsed keyword list."""
        return _split_keywords(self.keywords_raw)

    @property
    def telegram_enabled(self) -> bool:
        """True when both Telegram credentials are present."""
        return bool(self.bot_token and self.chat_id)

    @property
    def email_enabled(self) -> bool:
        """True when all SMTP credentials are present."""
        return bool(
            self.email_host
            and self.email_username
            and self.email_password
            and self.email_to
        )

    @property
    def effective_log_level(self) -> str:
        """Return DEBUG when debug_mode is on, otherwise log_level."""
        return "DEBUG" if self.debug_mode else self.log_level.upper()

    # ── Validators ──────────────────────────────────────────────────────────
    @field_validator("log_level", mode="before")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = str(v).upper()
        if upper not in allowed:
            raise ValueError(
                f"log_level must be one of {allowed}, got {v!r}"
            )
        return upper

    @field_validator("last_seen_path", "pdf_download_dir", mode="before")
    @classmethod
    def coerce_path(cls, v: object) -> Path:
        """Accept both str and Path."""
        return Path(str(v)) if v else Path()

    @field_validator("log_file", mode="before")
    @classmethod
    def coerce_log_file(cls, v: object) -> Path | None:
        """Empty string → None (disables file logging)."""
        if v == "" or v is None:
            return None
        return Path(str(v))

    @model_validator(mode="after")
    def warn_no_notifiers(self) -> "Settings":
        """
        Emit a warning if neither Telegram nor email is configured.
        The monitor will still run and log matches, but nothing will be sent.
        """
        if not self.telegram_enabled and not self.email_enabled:
            import warnings
            warnings.warn(
                "Neither Telegram nor Email credentials are configured. "
                "The monitor will run but no notifications will be sent.",
                UserWarning,
                stacklevel=2,
            )
        return self


# ---------------------------------------------------------------------------
# Cached accessor
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Return the global Settings instance.

    Parsed once and cached for the lifetime of the process.
    Call `get_settings.cache_clear()` in tests to reset.
    """
    return Settings()
