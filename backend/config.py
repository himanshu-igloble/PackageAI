"""Centralized settings. Loads .env once and exposes a typed Settings object.

LLM model usage (set per architecture directive):
- Intake / conversation     -> Gemini 2.5 Flash (low latency)
- Reasoning / verification  -> Gemini 3 Pro    (deeper analysis)
Both come from the same GEMINI_API_KEY.
"""
from __future__ import annotations

from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    GEMINI_API_KEY: str = ""
    # Optional secondary key. When the primary key hits a quota / 429 / auth
    # error, the GeminiClient retries the same call on the secondary key
    # before falling through the model chain. Set in .env as GEMINI_API_KEY2.
    GEMINI_API_KEY2: str = ""

    # Intake / conversation role — Gemini 2.5 Flash drives chat. Temperature
    # for this role is tuned higher (see agents) so intent classification
    # is willing to commit instead of looping for confirmation.
    GEMINI_INTAKE_MODEL: str = "gemini-2.5-flash"
    GEMINI_INTAKE_FALLBACK_MODELS: str = "gemini-2.0-flash,gemini-1.5-flash"

    # Reasoning / verification role — Gemini 3 Pro. Used at HIGHER temperature
    # for design exploration, lower temperature for verification self-checks.
    GEMINI_REASONING_MODEL: str = "gemini-3-pro"
    # If 3 Pro is unavailable, fall through to 2.5 Pro, then 2.5 Flash, then
    # 2.0 Flash. 2.5 Flash is fast and still capable for reasoning passes
    # — the user explicitly asked for it as a fallback.
    GEMINI_REASONING_FALLBACK_MODELS: str = "gemini-2.5-pro,gemini-2.5-flash,gemini-2.0-flash"

    DATA_DIR: str = "storage"
    DB_URL: str = "sqlite:///./cpg_ista.db"

    @property
    def storage_path(self) -> Path:
        p = PROJECT_ROOT / self.DATA_DIR
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def intake_model_chain(self) -> list[str]:
        return [self.GEMINI_INTAKE_MODEL] + [
            m.strip() for m in self.GEMINI_INTAKE_FALLBACK_MODELS.split(",") if m.strip()
        ]

    @property
    def reasoning_model_chain(self) -> list[str]:
        return [self.GEMINI_REASONING_MODEL] + [
            m.strip() for m in self.GEMINI_REASONING_FALLBACK_MODELS.split(",") if m.strip()
        ]


settings = Settings()
