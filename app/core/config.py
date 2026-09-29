"""Application settings, read from environment variables (and `.env` in development)."""

import os
from functools import lru_cache

from dotenv import load_dotenv
from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Legacy modules still read os.environ directly; never overrides variables already set.
load_dotenv()

# Railway sets RAILWAY_ENVIRONMENT_NAME ("production", "staging", ...) on every deploy.
_DEFAULT_ENVIRONMENT = os.getenv("RAILWAY_ENVIRONMENT_NAME") or "development"
_DEV_JWT_SECRET = "dev-only-insecure-secret-change-me-0123456789"
_MIN_JWT_SECRET_LENGTH = 32


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
        # Validation errors end up in deploy logs: never echo the (secret) input values.
        hide_input_in_errors=True,
        # An empty JWT_SECRET= (as in .env.example) must not hide a valid SECRET_KEY.
        env_ignore_empty=True,
    )

    environment: str = Field(_DEFAULT_ENVIRONMENT, validation_alias=AliasChoices("ENVIRONMENT", "APP_ENV"))
    log_level: str = "INFO"

    database_url: str = "sqlite:///./local.db"

    jwt_secret: str = Field("", validation_alias=AliasChoices("JWT_SECRET", "SECRET_KEY"), repr=False)
    # 7 days, as before: the deployed frontend calls fetch() directly and never refreshes.
    # Lowered once every screen goes through the new API client (which refreshes).
    access_token_minutes: int = 10080
    refresh_token_days: int = 30

    # Comma-separated list of allowed browser origins, plus an optional regex (e.g. Vercel previews).
    cors_origins: str = (
        "http://localhost:3001,"
        "https://action-colleague.vercel.app,"
        "https://action-colleague-mecalozas-projects.vercel.app"
    )
    cors_origin_regex: str = ""

    supabase_url: str = ""
    supabase_service_key: str = Field(
        "", validation_alias=AliasChoices("SUPABASE_SERVICE_KEY", "SUPABASE_SECRET_KEY", "SUPABASE_KEY"), repr=False
    )

    openai_api_key: str = Field("", repr=False)
    openai_model: str = "gpt-4o"
    elevenlabs_api_key: str = Field("", repr=False)
    elevenlabs_voice_id: str = "onwK4e9ZLuTAKqWnGpdt"
    heygen_api_key: str = Field("", repr=False)

    @field_validator("database_url")
    @classmethod
    def _normalize_postgres_scheme(cls, value: str) -> str:
        # SQLAlchemy 2 only understands postgresql://; Heroku-style URLs use postgres://.
        if value.startswith("postgres://"):
            return "postgresql://" + value.removeprefix("postgres://")
        return value

    @property
    def is_production(self) -> bool:
        return self.environment.lower() == "production"

    @property
    def is_deployed(self) -> bool:
        """Running on real infrastructure: production, or any Railway environment (e.g. staging)."""
        return self.is_production or bool(os.getenv("RAILWAY_ENVIRONMENT_NAME"))

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @model_validator(mode="after")
    def _validate_environment(self) -> "Settings":
        if self.is_deployed:
            if not self.database_url.startswith("postgresql"):
                raise ValueError("DATABASE_URL must point to Postgres outside local development")
            if len(self.jwt_secret) < _MIN_JWT_SECRET_LENGTH:
                raise ValueError(
                    f"JWT_SECRET (or SECRET_KEY) must be set to at least {_MIN_JWT_SECRET_LENGTH} "
                    "characters outside local development"
                )
        elif not self.jwt_secret:
            self.jwt_secret = _DEV_JWT_SECRET
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
