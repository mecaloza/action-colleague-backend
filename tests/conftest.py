import os

# Never let the test suite touch a real database or provider; set before any app module is
# imported. Keys are blanked (not removed) so a developer's .env can't fill them back in.
os.environ["DATABASE_URL"] = "sqlite://"
os.environ["ENVIRONMENT"] = "test"
os.environ["WORKER_ENABLED"] = "false"
os.environ["USE_FAKE_PROVIDERS"] = "true"  # offline AI, voice and presenter
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

import logging  # noqa: E402
import uuid  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, event, text  # noqa: E402
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

    monkeypatch.setattr(main, "run_migrations", lambda: None)
    monkeypatch.setattr(main, "start_worker", lambda: None)
    monkeypatch.setattr(main, "schedule_maintenance", lambda: None)
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


# ── PostgreSQL (embedded server) ──────────────────────────────────────


@pytest.fixture(scope="session")
def pg_server(tmp_path_factory):
    pgserver = pytest.importorskip("pgserver")
    # pgserver logs its shutdown at interpreter exit, after pytest closed the output streams.
    logging.getLogger("pgserver").setLevel(logging.WARNING)
    server = pgserver.get_server(str(tmp_path_factory.mktemp("pg")), cleanup_mode="stop")
    yield server
    server.cleanup()


@pytest.fixture
def pg_engine(pg_server):
    """Engine on a fresh migrated database; override conftest's `engine` with it to run on Postgres."""
    from app.db.migrate import run_migrations
    from app.db.session import build_engine

    name = f"t_{uuid.uuid4().hex[:10]}"
    server = create_engine(pg_server.get_uri(), isolation_level="AUTOCOMMIT")
    with server.connect() as connection:
        connection.execute(text(f"CREATE DATABASE {name}"))
    url = pg_server.get_uri(name)
    run_migrations(url)
    engine = build_engine(url)
    with engine.begin() as connection:
        # Still in production, no longer mapped: a NOT NULL UNIQUE reference to enrollments.
        connection.execute(text(
            "CREATE TABLE IF NOT EXISTS certificates (id SERIAL PRIMARY KEY, "
            "enrollment_id INTEGER NOT NULL UNIQUE REFERENCES enrollments(id), issued_at TIMESTAMP DEFAULT now())"
        ))
    yield engine
    engine.dispose()
    with server.connect() as connection:
        connection.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
    server.dispose()


class QueryCounter:
    """Counts the statements an engine sends while active (`with counter: ...`)."""

    def __init__(self, engine):
        self.count, self._active = 0, False
        event.listen(engine, "before_cursor_execute", self._on_execute)

    def _on_execute(self, *args):
        if self._active:
            self.count += 1

    def __enter__(self):
        self.count, self._active = 0, True
        return self

    def __exit__(self, *exc):
        self._active = False


@pytest.fixture
def storage(tmp_path, monkeypatch):
    """Local storage in a temporary folder, with links the test client can follow."""
    from app.core.config import get_settings
    from app.services.storage import get_storage

    settings = get_settings()
    monkeypatch.setattr(settings, "storage_backend", "local")
    monkeypatch.setattr(settings, "local_storage_dir", str(tmp_path / "storage"))
    monkeypatch.setattr(settings, "public_api_url", "http://testserver/api/v1")
    get_storage.cache_clear()
    yield get_storage()
    get_storage.cache_clear()


@pytest.fixture
def worker(session_factory, monkeypatch):
    """Runs queued jobs inline against the test database."""
    from app.db import session as db_session
    from app.worker import jobs  # noqa: F401  (registers handlers)
    from app.worker.runner import run_pending

    monkeypatch.setattr(db_session, "SessionLocal", session_factory)
    return run_pending
