import os

# Never let the test suite touch a real database or provider; set before any app module is
# imported. Keys are blanked (not removed) so a developer's .env can't fill them back in.
os.environ["DATABASE_URL"] = "sqlite://"
os.environ["ENVIRONMENT"] = "test"
for key in (
    "HEYGEN_API_KEY",
    "OPENAI_API_KEY",
    "ELEVENLABS_API_KEY",
    "SUPABASE_URL",
    "SUPABASE_SERVICE_KEY",
    "SUPABASE_SECRET_KEY",
    "SUPABASE_KEY",
    "RAILWAY_ENVIRONMENT_NAME",
):
    os.environ[key] = ""

from app.core.config import Settings  # noqa: E402

Settings.model_config["env_file"] = None  # settings come only from the variables above

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.core.security import hash_password  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.models import User  # noqa: E402
from app.db.session import get_db  # noqa: E402

TEST_PASSWORD = "Passw0rd!"


@pytest.fixture
def engine():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session_factory(engine):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.fixture
def db(session_factory):
    with session_factory() as session:
        yield session


@pytest.fixture
def client(session_factory, monkeypatch):
    from app import main

    def override_get_db():
        with session_factory() as session:
            yield session

    monkeypatch.setattr(main, "create_tables", lambda: None)
    monkeypatch.setattr(main, "start_background_sweeps", lambda: None)
    main.app.dependency_overrides[get_db] = override_get_db
    with TestClient(main.app) as test_client:
        yield test_client
    main.app.dependency_overrides.clear()


def _make_user(db, email: str, role: str) -> User:
    user = User(name=email.split("@")[0], email=email, role=role, password_hash=hash_password(TEST_PASSWORD))
    db.add(user)
    db.commit()
    return user


@pytest.fixture
def admin(db):
    return _make_user(db, "admin@test.dev", "admin")


@pytest.fixture
def collaborator(db):
    return _make_user(db, "colab@test.dev", "collaborator")


def login(client, email: str, password: str = TEST_PASSWORD) -> dict:
    response = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return response.json()


def auth_headers(client, email: str) -> dict:
    return {"Authorization": f"Bearer {login(client, email)['access_token']}"}
