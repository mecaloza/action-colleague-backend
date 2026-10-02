"""Application settings, read from environment variables (and `.env` in development)."""

import os
from functools import lru_cache
from typing import Literal

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
    # Short-lived: every screen goes through the API client, which refreshes it transparently.
    access_token_minutes: int = 60
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

    # Media storage: a private Supabase bucket, or local files for development and tests.
    storage_backend: Literal["auto", "supabase", "local"] = "auto"
    media_bucket: str = "course-media"
    local_storage_dir: str = "./.storage"
    # Public URL of this API, used to build local-storage links (development only).
    public_api_url: str = "http://localhost:8001/api/v1"

    # Background jobs run inside the API process unless disabled (e.g. a separate worker service).
    worker_enabled: bool = True
    worker_concurrency: int = Field(2, ge=1, le=8)

    openai_api_key: str = Field("", repr=False)
    openai_model: str = "gpt-4o"
    # Deterministic offline AI, voice and presenter (local development and e2e without API keys).
    use_fake_providers: bool = False
    elevenlabs_api_key: str = Field("", repr=False)
    elevenlabs_voice_id: str = "onwK4e9ZLuTAKqWnGpdt"
    elevenlabs_model: str = "eleven_multilingual_v2"  # es / en / pt with the same voice
    heygen_api_key: str = Field("", repr=False)
    # HeyGen v3 avatar engine: avatar_iii is the most affordable; avatar_iv / avatar_v look more natural.
    heygen_engine: str = "avatar_iii"
    # Scene visuals: stock video (Pixabay), generated images (OpenAI) and short animated clips (Google Veo).
    pixabay_api_key: str = Field("", repr=False)
    openai_image_model: str = "gpt-image-2"
    gemini_api_key: str = Field("", repr=False)
    veo_model: str = "veo-3.1-lite-generate-preview"
    max_clips_per_module: int = Field(2, ge=0, le=10)  # the costly kind: the rest of the scenes use stock or images
    max_clips_high: int = Field(12, ge=0, le=40)  # per module with animation "high" (every visual a clip)

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
            if self.use_fake_providers:
                raise ValueError("USE_FAKE_PROVIDERS is only for local development")
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
