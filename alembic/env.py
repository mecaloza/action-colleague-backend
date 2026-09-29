from alembic import context
from sqlalchemy import create_engine, pool

import app.db.models  # noqa: F401  (registers every table on Base.metadata)
from app.core.config import get_settings
from app.db.migrate import configure_context, lock_for_migrations

config = context.config


def _url() -> str:
    return config.get_main_option("sqlalchemy.url") or get_settings().database_url


def _migrate(**configure_options) -> None:
    configure_context(context, **configure_options)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_offline() -> None:
    _migrate(url=_url(), literal_binds=True, dialect_opts={"paramstyle": "named"})


def run_migrations_online() -> None:
    # app.db.migrate passes its own connection (already locked, inside one transaction).
    connection = config.attributes.get("connection")
    if connection is not None:
        _migrate(connection=connection)
        return

    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.begin() as connection:
        lock_for_migrations(connection)
        _migrate(connection=connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
