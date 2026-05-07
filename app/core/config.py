"""
AcademicLink — Application Configuration

Centralised settings loaded from environment variables / .env file.
Uses pydantic-settings for validation and type coercion.
"""

from pathlib import Path
from typing import Optional

from pydantic import Field as PydanticField

from pydantic_settings import BaseSettings, SettingsConfigDict

# Resolve .env relative to project root (two levels up from this file)
_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class Settings(BaseSettings):
    """Immutable, validated application settings."""

    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # --- Project ---
    project_name: str = "AcademicLink"

    # --- Telegram ---
    bot_token: Optional[str] = None

    # --- Database ---
    database_url: str = (
        "postgresql+asyncpg://user:password@localhost:5432/academiclink"
    )

    # --- Security ---
    secret_key: str = "change-me"

    # --- Default Tutor (used to seed DB on first run) ---
    default_tutor_tg_id: int | None = PydanticField(
        default=None,
        description="Telegram ID for the default tutor (set in .env)",
    )
    default_tutor_name: str = "Tutor"

    # --- Runtime ---
    environment: str = "development"

    @property
    def is_production(self) -> bool:
        return self.environment.lower() == "production"


# Singleton — import `settings` everywhere
settings = Settings()  # type: ignore[call-arg]
