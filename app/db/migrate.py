"""
Bring the database schema up to date (runs on startup, or `python -m app.db.migrate`).

Production predates Alembic: its tables were created by `create_all`. When the tables exist but no
revision was ever recorded, the baseline revision (which describes that schema) is stamped and the
database is upgraded from there. Everything runs in one transaction holding an advisory lock, so
two containers starting together can't both migrate.
"""

import hashlib
import logging
import random
import time
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import Connection, Engine, create_engine, inspect, make_url, pool, text
from sqlalchemy.exc import DBAPIError

from app.core.config import get_settings
from app.db.base import Base

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
BASELINE_REVISION = "0001_baseline"
# The database is shared with another application: never use Alembic's generic `alembic_version`.
VERSION_TABLE = "action_colleague_alembic_version"
# Advisory locks are database-wide and the database is shared: derive the key from a namespaced name.
_LOCK_ID = int.from_bytes(hashlib.sha256(b"action-colleague:migrations").digest()[:8], "big", signed=True)
# The previous release keeps serving during a deploy. DDL must never queue behind one of its long
# requests: a waiting ALTER TABLE blocks every other query on that table. Fail fast and retry.
LOCK_TIMEOUT = "5s"
STATEMENT_TIMEOUT = "60s"
MAX_ATTEMPTS = 6  # ~75 s worst case, under Railway's 120 s healthcheck window
_RETRYABLE = {"55P03", "40P01"}  # lock_not_available, deadlock_detected


def include_object(obj, name, type_, reflected, compare_to) -> bool:
    """Only manage what the models declare.

    The production database also holds another application's tables (`wendy_*`) and legacy
    tables/columns this app no longer maps; autogenerate must never propose dropping them.
    """
    return not (reflected and compare_to is None)


# How the models are compared with a database: used by autogenerate and by the schema-parity tests.
COMPARE_OPTIONS = {"include_object": include_object, "compare_type": True}


def configure_context(context, **kwargs) -> None:
    """Alembic settings shared by every way env.py runs (online, offline, autogenerate)."""
    connection = kwargs.get("connection")
    backend = connection.dialect.name if connection is not None else make_url(kwargs["url"]).get_backend_name()
    context.configure(
        target_metadata=Base.metadata,
        version_table=VERSION_TABLE,
        render_as_batch=backend == "sqlite",  # SQLite can't ALTER constraints in place
        **COMPARE_OPTIONS,
        **kwargs,
    )


def lock_for_migrations(connection: Connection) -> None:
    if connection.dialect.name != "postgresql":
        return
    # Transaction-scoped: released on commit/rollback, and works through Supabase's transaction pooler.
    connection.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": _LOCK_ID})
    # Set after the advisory lock (it obeys lock_timeout too): a second container just waits its turn.
    connection.execute(text("SELECT set_config('lock_timeout', :v, true)"), {"v": LOCK_TIMEOUT})
    connection.execute(text("SELECT set_config('statement_timeout', :v, true)"), {"v": STATEMENT_TIMEOUT})


def alembic_config(database_url: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def current_revision(connection: Connection) -> str | None:
    return MigrationContext.configure(connection, opts={"version_table": VERSION_TABLE}).get_current_revision()


def _migrate_once(engine: Engine, config: Config) -> None:
    with engine.begin() as connection:
        lock_for_migrations(connection)
        config.attributes["connection"] = connection
        before = current_revision(connection)
        # Tables from `create_all` but no recorded revision (a missing *or empty* version table).
        if before is None and "users" in inspect(connection).get_table_names():
            logger.info("migrations_stamp_baseline", extra={"revision": BASELINE_REVISION})
            command.stamp(config, BASELINE_REVISION)
        command.upgrade(config, "head")
        after = current_revision(connection)
    logger.info("migrations_applied", extra={"from_revision": before, "to_revision": after})


def run_migrations(database_url: str | None = None) -> None:
    url = database_url or get_settings().database_url
    config = alembic_config(url)
    engine = create_engine(url, poolclass=pool.NullPool, hide_parameters=True)
    try:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                _migrate_once(engine, config)
                return
            except DBAPIError as exc:
                if getattr(exc.orig, "pgcode", None) not in _RETRYABLE or attempt == MAX_ATTEMPTS:
                    logger.error("migrations_failed", extra={"attempt": attempt}, exc_info=True)
                    raise
                delay = min(2**attempt, 15) + random.uniform(0, 1)
                logger.warning("migrations_lock_busy", extra={"attempt": attempt, "retry_in_s": round(delay, 1)})
                time.sleep(delay)
    finally:
        engine.dispose()


if __name__ == "__main__":
    from app.core.logging import configure_logging

    configure_logging()
    run_migrations()
