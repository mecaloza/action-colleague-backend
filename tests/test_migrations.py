"""Migrations: fresh databases, production-like legacy databases, and parity with the models."""

import logging
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Engine, create_engine, inspect, text

import app.db.models  # noqa: F401  (registers every table)
from app.db.base import Base
from app.db.migrate import BASELINE_REVISION, COMPARE_OPTIONS, VERSION_TABLE, alembic_config, run_migrations


@pytest.fixture(params=["sqlite", "postgres"])
def database_url(request, tmp_path):
    if request.param == "sqlite":
        yield f"sqlite:///{tmp_path / 'migrate.db'}"
        return
    pgserver = pytest.importorskip("pgserver")
    # pgserver logs its shutdown at interpreter exit, after pytest closed the output streams.
    logging.getLogger("pgserver").setLevel(logging.WARNING)
    server = pgserver.get_server(str(tmp_path / "pg"), cleanup_mode="stop")
    try:
        yield server.get_uri()
    finally:
        server.cleanup()


@contextmanager
def _engine(url: str) -> Iterator[Engine]:
    engine = create_engine(url)
    try:
        yield engine
    finally:
        engine.dispose()


def _execute(url: str, *statements: str, **params) -> None:
    with _engine(url) as engine, engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement), params)


def _fetch(url: str, statement: str) -> list[tuple]:
    with _engine(url) as engine, engine.connect() as connection:
        return [tuple(row) for row in connection.execute(text(statement))]


def _table_names(url: str) -> set[str]:
    with _engine(url) as engine:
        return set(inspect(engine).get_table_names())


def _column_names(url: str, table: str) -> set[str]:
    with _engine(url) as engine:
        return {column["name"] for column in inspect(engine).get_columns(table)}


def _schema_diff(url: str) -> list:
    with _engine(url) as engine, engine.connect() as connection:
        context = MigrationContext.configure(connection, opts=COMPARE_OPTIONS)
        return compare_metadata(context, Base.metadata)


def _legacy_database(url: str) -> None:
    """A database like production: baseline schema, data, and no alembic_version table."""
    command.upgrade(alembic_config(url), BASELINE_REVISION)
    _execute(
        url,
        f"DROP TABLE {VERSION_TABLE}",
        "INSERT INTO users (id, name, email, password_hash, role) VALUES (1, 'Ana', 'a@x.test', 'h', 'admin')",
        "INSERT INTO courses (id, title, description, status) VALUES "
        "(1, 'IA', 'Curso generado con IA. Voz: abc', 'published'), "
        "(2, 'Manual', 'hecho a mano', 'draft'), "
        "(3, 'IA sin descripción', 'Capacitación', 'published')",
        'INSERT INTO modules (id, course_id, title, "order", video_url) VALUES '
        "(1, 1, 'con avatar', 1, 'heygen://video/abc'), "
        "(2, 1, 'fallido', 2, ''), "
        "(3, 2, 'texto', 1, ''), "
        "(4, 2, 'subido', 2, 'https://cdn.test/video.mp4'), "
        "(5, 3, 'persistido', 1, 'https://x.supabase.co/storage/v1/object/public/course-videos/modules/5/v.mp4')",
        "INSERT INTO refresh_tokens (user_id, token, expires_at, revoked) VALUES "
        "(1, 'plain-token', '2030-01-01 00:00:00+00', :revoked)",
        revoked=False,
    )


def test_fresh_database_matches_the_models(database_url):
    run_migrations(database_url)

    assert _schema_diff(database_url) == []
    assert _fetch(database_url, f"SELECT version_num FROM {VERSION_TABLE}") == [("0002_course_studio",)]


def test_production_like_database_is_stamped_then_upgraded(database_url):
    _legacy_database(database_url)

    run_migrations(database_url)

    assert dict(_fetch(database_url, "SELECT id, source FROM courses")) == {1: "ai", 2: "manual", 3: "ai"}
    assert dict(_fetch(database_url, "SELECT id, source FROM modules")) == {
        1: "ai",
        2: "ai",
        3: "text",
        4: "upload",
        5: "ai",
    }
    assert [bool(revoked) for (revoked,) in _fetch(database_url, "SELECT revoked FROM refresh_tokens")] == [True]
    assert _fetch(database_url, f"SELECT version_num FROM {VERSION_TABLE}") == [("0002_course_studio",)]
    assert _schema_diff(database_url) == []


def test_other_applications_tables_are_left_alone(database_url):
    _execute(database_url, "CREATE TABLE wendy_candidates (id INTEGER PRIMARY KEY, name VARCHAR(50))")

    run_migrations(database_url)

    assert "wendy_candidates" in _table_names(database_url)
    assert _schema_diff(database_url) == []


def test_migrations_are_idempotent(database_url):
    run_migrations(database_url)
    run_migrations(database_url)

    assert _schema_diff(database_url) == []


def test_new_tables_have_row_level_security_on_postgres(database_url):
    if not database_url.startswith("postgresql"):
        pytest.skip("RLS only exists on Postgres")
    run_migrations(database_url)

    rows = _fetch(
        database_url,
        f"SELECT relname, relrowsecurity FROM pg_class WHERE relname IN ('media_assets', 'jobs', '{VERSION_TABLE}') ORDER BY relname",
    )
    assert rows == [(VERSION_TABLE, True), ("jobs", True), ("media_assets", True)]


def test_downgrade_returns_to_the_baseline(database_url):
    run_migrations(database_url)
    command.downgrade(alembic_config(database_url), BASELINE_REVISION)

    assert {"media_assets", "jobs"}.isdisjoint(_table_names(database_url))
    assert "source" not in _column_names(database_url, "courses")


# --- added in review -------------------------------------------------------------------------

import threading  # noqa: E402
import time  # noqa: E402

from alembic.script import ScriptDirectory  # noqa: E402

import app.db.migrate as migrate  # noqa: E402

_HEAD = ScriptDirectory.from_config(alembic_config("sqlite://")).get_current_head()


def test_there_is_a_single_head():
    # Two stacked PRs adding a migration on the same parent would make startup fail ("multiple heads").
    assert len(ScriptDirectory.from_config(alembic_config("sqlite://")).get_heads()) == 1


def _is_postgres(url: str) -> bool:
    return url.startswith("postgresql")


def test_a_failed_migration_leaves_production_untouched(database_url):
    if not _is_postgres(database_url):
        pytest.skip("SQLite DDL is not transactional")
    _legacy_database(database_url)
    # Fails in the middle of 0002, after the stamp and the DDL on courses/modules/evaluations.
    _execute(database_url, "INSERT INTO enrollments (user_id, course_id, status) VALUES (1, 2, 'a'), (1, 2, 'b')")

    with pytest.raises(RuntimeError):
        run_migrations(database_url)

    assert VERSION_TABLE not in _table_names(database_url)
    assert "media_assets" not in _table_names(database_url)
    assert "source" not in _column_names(database_url, "modules")


def test_duplicate_progress_is_merged_keeping_the_best_row(database_url):
    _legacy_database(database_url)
    _execute(
        database_url,
        "INSERT INTO enrollments (id, user_id, course_id, status) VALUES (1, 1, 1, 'assigned')",
        "INSERT INTO module_progress (enrollment_id, module_id, completed, passed, score, attempts) VALUES "
        "(1, 1, false, false, 40, 1), (1, 1, true, true, 90, 2)",
    )

    run_migrations(database_url)

    rows = _fetch(database_url, "SELECT enrollment_id, module_id, completed, passed, score, attempts FROM module_progress")
    assert [(e, m, bool(c), bool(p), s, a) for e, m, c, p, s, a in rows] == [(1, 1, True, True, 90.0, 2)]


def test_duplicate_enrollments_stop_the_migration_with_a_clear_message(database_url):
    _legacy_database(database_url)
    _execute(
        database_url,
        "INSERT INTO enrollments (user_id, course_id, status) VALUES (1, 2, 'assigned'), (1, 2, 'completed')",
    )

    with pytest.raises(RuntimeError, match=r"\(user 1, course 2\) x2"):
        run_migrations(database_url)


def test_a_module_keeps_its_own_manual_video_inside_an_ai_course(database_url):
    _legacy_database(database_url)
    storage = "https://x.supabase.co/storage/v1/object/public"
    _execute(
        database_url,
        'INSERT INTO modules (id, course_id, title, "order", video_url) VALUES '
        f"(6, 1, 'replaced by hand', 3, '{storage}/user-videos/composed.mp4'), "
        "(7, 2, 'old heygen url', 3, 'https://files2.heygen.ai/aws_pacific/x.mp4')",
    )

    run_migrations(database_url)

    sources = dict(_fetch(database_url, "SELECT id, source FROM modules"))
    assert sources[6] == "upload"
    assert sources[7] == "ai"


def test_the_previous_release_can_still_insert_rows(database_url):
    _legacy_database(database_url)
    run_migrations(database_url)

    # What the previous app's models insert: none of the new NOT NULL columns.
    _execute(
        database_url,
        "INSERT INTO courses (id, title, status) VALUES (10, 't', 'draft')",
        'INSERT INTO modules (id, course_id, title, "order") VALUES (10, 10, \'m\', 1)',
        "INSERT INTO evaluations (module_id) VALUES (10)",
        "INSERT INTO enrollments (id, user_id, course_id, status) VALUES (10, 1, 10, 'assigned')",
        "INSERT INTO module_progress (enrollment_id, module_id) VALUES (10, 10)",
    )
    assert _fetch(database_url, "SELECT source FROM courses WHERE id = 10") == [("manual",)]


def test_an_empty_version_table_is_stamped_too(database_url):
    # e.g. a SQLite dev database where the first attempt failed after creating the version table.
    _legacy_database(database_url)
    _execute(database_url, f"CREATE TABLE {VERSION_TABLE} (version_num VARCHAR(32) NOT NULL PRIMARY KEY)")

    run_migrations(database_url)

    assert _fetch(database_url, f"SELECT version_num FROM {VERSION_TABLE}") == [(_HEAD,)]


def test_ddl_never_queues_behind_a_long_request_of_the_previous_release(database_url, monkeypatch):
    if not _is_postgres(database_url):
        pytest.skip("Postgres locking")
    _legacy_database(database_url)
    monkeypatch.setattr(migrate, "LOCK_TIMEOUT", "300ms")
    real_sleep = time.sleep
    monkeypatch.setattr(migrate.time, "sleep", lambda s: real_sleep(0.2))

    with _engine(database_url) as engine, engine.connect() as old_request:
        old_request.begin()
        old_request.execute(text("SELECT * FROM modules")).all()  # then waits on an external API
        migration = threading.Thread(target=run_migrations, args=(database_url,))
        migration.start()
        time.sleep(0.2)
        started = time.monotonic()
        _fetch(database_url, "SELECT count(*) FROM courses")  # another request of the old release
        waited = time.monotonic() - started
        time.sleep(1)
        old_request.rollback()
    migration.join(timeout=30)

    assert waited < 1
    assert _fetch(database_url, f"SELECT version_num FROM {VERSION_TABLE}") == [(_HEAD,)]
