"""Schema bootstrap used until Alembic migrations take over."""

from sqlalchemy import inspect, text

import app.db.models  # noqa: F401  (registers every table on Base.metadata)
from app.db.base import Base
from app.db.session import engine


def _add_missing_column(conn, table: str, column: str, ddl: str) -> None:
    if column not in {c["name"] for c in inspect(conn).get_columns(table)}:
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))


def create_tables() -> None:
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        _add_missing_column(conn, "users", "preferred_language", "VARCHAR(5) DEFAULT 'es'")
        _add_missing_column(conn, "courses", "language", "VARCHAR(5) DEFAULT 'es'")
        _add_missing_column(conn, "evaluations", "max_attempts", "INTEGER DEFAULT 3")
