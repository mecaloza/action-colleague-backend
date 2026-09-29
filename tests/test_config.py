import logging

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.logging import configure_logging


def test_production_requires_a_real_database(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///./x.db")
    monkeypatch.setenv("JWT_SECRET", "y" * 40)
    with pytest.raises(ValidationError, match="DATABASE_URL"):
        Settings(environment="production", _env_file=None)


def test_production_requires_a_strong_jwt_secret(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h:5432/db")
    monkeypatch.setenv("JWT_SECRET", "short")
    with pytest.raises(ValidationError, match="JWT_SECRET"):
        Settings(environment="production", _env_file=None)


def test_legacy_secret_key_variable_is_accepted(monkeypatch):
    monkeypatch.delenv("JWT_SECRET", raising=False)
    monkeypatch.setenv("SECRET_KEY", "z" * 40)
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h:5432/db")
    assert Settings(environment="production", _env_file=None).jwt_secret == "z" * 40


def test_development_gets_a_default_secret(monkeypatch):
    monkeypatch.delenv("JWT_SECRET", raising=False)
    monkeypatch.delenv("SECRET_KEY", raising=False)
    settings = Settings(environment="development", _env_file=None)
    assert len(settings.jwt_secret) >= 32


def test_cors_origins_are_split_and_trimmed(monkeypatch):
    monkeypatch.setenv("CORS_ORIGINS", " https://a.test , https://b.test,,")
    assert Settings(_env_file=None).cors_origin_list == ["https://a.test", "https://b.test"]


def test_any_railway_environment_requires_real_secrets(monkeypatch):
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", "staging")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h:5432/db")
    monkeypatch.delenv("JWT_SECRET", raising=False)
    monkeypatch.delenv("SECRET_KEY", raising=False)
    with pytest.raises(ValidationError, match="JWT_SECRET"):
        Settings(environment="staging", _env_file=None)


def test_non_postgres_database_is_rejected_when_deployed(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "mysql://u:p@h/db")
    monkeypatch.setenv("JWT_SECRET", "y" * 40)
    with pytest.raises(ValidationError, match="DATABASE_URL"):
        Settings(environment="production", _env_file=None)


def test_secrets_are_hidden_from_repr(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-very-secret")
    monkeypatch.setenv("JWT_SECRET", "z" * 40)
    text = repr(Settings(_env_file=None))
    assert "sk-very-secret" not in text
    assert "z" * 40 not in text


def test_postgres_scheme_is_normalized_for_sqlalchemy(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://u:p@h:5432/db")
    monkeypatch.setenv("JWT_SECRET", "y" * 40)
    assert Settings(environment="production", _env_file=None).database_url == "postgresql://u:p@h:5432/db"


def test_empty_jwt_secret_does_not_hide_secret_key(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "")
    monkeypatch.setenv("SECRET_KEY", "k" * 40)
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h:5432/db")
    assert Settings(environment="production", _env_file=None).jwt_secret == "k" * 40


def test_validation_errors_do_not_echo_secret_values(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h:5432/db")
    monkeypatch.setenv("JWT_SECRET", "short-secret")
    monkeypatch.setenv("ACCESS_TOKEN_MINUTES", "sk-not-a-number")
    with pytest.raises(ValidationError) as error:
        Settings(environment="production", _env_file=None)
    assert "sk-not-a-number" not in str(error.value)


def test_log_level_is_case_insensitive():
    configure_logging("debug")
    assert logging.getLogger().level == logging.DEBUG
    configure_logging("INFO")
