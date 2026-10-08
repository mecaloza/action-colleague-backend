import logging

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.logging import configure_logging


@pytest.fixture(autouse=True)
def real_providers(monkeypatch):
    # The test suite runs with fake providers; these tests build production settings (with their public URLs).
    monkeypatch.setenv("USE_FAKE_PROVIDERS", "false")
    monkeypatch.setenv("PUBLIC_API_URL", "https://api.test/api/v1")
    monkeypatch.setenv("PUBLIC_APP_URL", "https://app.test")


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


def test_fake_providers_are_refused_outside_local_development(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h:5432/db")
    monkeypatch.setenv("JWT_SECRET", "y" * 40)
    monkeypatch.setenv("USE_FAKE_PROVIDERS", "true")
    with pytest.raises(ValidationError, match="USE_FAKE_PROVIDERS"):
        Settings(environment="production", _env_file=None)


def test_the_web_apps_url_defaults_to_the_first_cors_origin(monkeypatch):
    # Certificates link their verification page there.
    monkeypatch.setenv("CORS_ORIGINS", "https://app.test, https://preview.test")
    monkeypatch.delenv("PUBLIC_APP_URL", raising=False)
    assert Settings(_env_file=None).public_app_url == "https://app.test"
    monkeypatch.setenv("PUBLIC_APP_URL", "https://verificar.test")
    assert Settings(_env_file=None).public_app_url == "https://verificar.test"


def test_development_links_to_the_local_api_and_app(monkeypatch):
    for name in ("PUBLIC_API_URL", "PUBLIC_APP_URL", "CORS_ORIGINS"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(environment="development", _env_file=None)
    assert (settings.public_api_url, settings.public_app_url) == ("http://localhost:8001/api/v1",
                                                                  "http://localhost:3001")


@pytest.mark.parametrize(("variables", "missing"), [
    ({"PUBLIC_API_URL": None}, "PUBLIC_API_URL"),
    ({"PUBLIC_APP_URL": None}, "PUBLIC_APP_URL"),  # nor CORS_ORIGINS: the default origins start with localhost
    ({"PUBLIC_APP_URL": None, "CORS_ORIGINS": "*"}, "PUBLIC_APP_URL"),
    ({"PUBLIC_API_URL": "api.test/api/v1"}, "PUBLIC_API_URL"),
])
def test_certificate_links_need_the_public_urls_when_deployed(monkeypatch, variables, missing):
    # Certificates keep their PDF and verification links for good: a deploy without them must not start.
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h:5432/db")
    monkeypatch.setenv("JWT_SECRET", "y" * 40)
    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    for name, value in variables.items():
        if value is None:
            monkeypatch.delenv(name)
        else:
            monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError, match=missing):
        Settings(environment="production", _env_file=None)


def test_a_deploy_may_link_the_web_app_through_its_cors_origin(monkeypatch):
    # As the local demo does against production data: its own API and app, on this machine.
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h:5432/db")
    monkeypatch.setenv("JWT_SECRET", "y" * 40)
    monkeypatch.setenv("PUBLIC_API_URL", "http://localhost:8030/api/v1")
    monkeypatch.setenv("CORS_ORIGINS", "http://localhost:3030")
    monkeypatch.delenv("PUBLIC_APP_URL")
    settings = Settings(environment="production", _env_file=None)
    assert (settings.public_api_url, settings.public_app_url) == ("http://localhost:8030/api/v1",
                                                                  "http://localhost:3030")
