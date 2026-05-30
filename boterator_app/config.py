from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    master_token: str
    database_url: str
    log_level: str = "INFO"
    publish_mode: str = "forward"
    registration_timeout_seconds: int = 600
    ai_provider: str = "none"
    ai_max_image_bytes: int = 7 * 1024 * 1024
    openai_api_key: str | None = None
    openai_model: str = "gpt-5-mini"
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-3.5-flash"
    data_retention_days: int = 90
    cleanup_interval_hours: int = 24

    @classmethod
    def from_env(cls) -> "Settings":
        master_token = os.getenv("TG_TOKEN") or os.getenv("MASTER_BOT_TOKEN")
        if not master_token:
            raise RuntimeError("TG_TOKEN or MASTER_BOT_TOKEN is required")

        database_url = os.getenv("DATABASE_URL") or os.getenv("DB")
        if not database_url:
            raise RuntimeError("DATABASE_URL is required")

        publish_mode = os.getenv("PUBLISH_MODE", "forward").strip().lower()
        if publish_mode not in {"copy", "forward"}:
            raise RuntimeError("PUBLISH_MODE must be either 'copy' or 'forward'")

        ai_provider = os.getenv("AI_PROVIDER", "none").strip().lower()
        if ai_provider not in {"none", "openai", "gemini"}:
            raise RuntimeError("AI_PROVIDER must be one of: none, openai, gemini")

        return cls(
            master_token=master_token,
            database_url=database_url,
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            publish_mode=publish_mode,
            registration_timeout_seconds=int(os.getenv("REGISTRATION_TIMEOUT_SECONDS", "600")),
            ai_provider=ai_provider,
            ai_max_image_bytes=int(os.getenv("AI_MAX_IMAGE_BYTES", str(7 * 1024 * 1024))),
            openai_api_key=os.getenv("OPENAI_API_KEY"),
            openai_model=os.getenv("OPENAI_MODEL", "gpt-5-mini"),
            gemini_api_key=os.getenv("GEMINI_API_KEY"),
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.5-flash"),
            data_retention_days=max(1, int(os.getenv("DATA_RETENTION_DAYS", "90"))),
            cleanup_interval_hours=max(1, int(os.getenv("CLEANUP_INTERVAL_HOURS", "24"))),
        )
