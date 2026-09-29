from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings


def build_engine(database_url: str) -> Engine:
    # hide_parameters: SQL errors are logged, and their parameters can hold hashes and emails.
    if database_url.startswith("sqlite"):
        return create_engine(database_url, connect_args={"check_same_thread": False}, hide_parameters=True)
    # Supabase's pooler closes idle connections; pre-ping and recycle avoid stale ones.
    return create_engine(
        database_url, pool_pre_ping=True, pool_recycle=300, pool_size=5, max_overflow=5, hide_parameters=True
    )


engine = build_engine(get_settings().database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    with SessionLocal() as db:
        yield db
