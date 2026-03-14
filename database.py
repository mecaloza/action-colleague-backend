import os

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, DeclarativeBase

load_dotenv()

SQLALCHEMY_DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres.xfazeoeebrdswhppjksi:AJP1MNrLx0zPh2VN@aws-0-us-west-2.pooler.supabase.com:6543/postgres",
)

engine = create_engine(SQLALCHEMY_DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def _column_exists(conn, table_name: str, column_name: str) -> bool:
    rows = conn.execute(text(f"PRAGMA table_info({table_name})")).fetchall()
    return any(row[1] == column_name for row in rows)


def _pg_column_exists(conn, table_name: str, column_name: str) -> bool:
    result = conn.execute(
        text("SELECT 1 FROM information_schema.columns WHERE table_name = :t AND column_name = :c"),
        {"t": table_name, "c": column_name},
    ).fetchone()
    return result is not None


def ensure_i18n_columns():
    with engine.begin() as conn:
        if engine.dialect.name == "sqlite":
            if not _column_exists(conn, "users", "preferred_language"):
                conn.execute(text('ALTER TABLE users ADD COLUMN preferred_language VARCHAR(5) DEFAULT "es"'))
            if not _column_exists(conn, "courses", "language"):
                conn.execute(text('ALTER TABLE courses ADD COLUMN language VARCHAR(5) DEFAULT "es"'))
        else:
            # PostgreSQL
            if not _pg_column_exists(conn, "users", "preferred_language"):
                conn.execute(text("ALTER TABLE users ADD COLUMN preferred_language VARCHAR(5) DEFAULT 'es'"))
            if not _pg_column_exists(conn, "courses", "language"):
                conn.execute(text("ALTER TABLE courses ADD COLUMN language VARCHAR(5) DEFAULT 'es'"))


def ensure_evaluation_columns():
    with engine.begin() as conn:
        if engine.dialect.name == "sqlite":
            if not _column_exists(conn, "evaluations", "max_attempts"):
                conn.execute(text("ALTER TABLE evaluations ADD COLUMN max_attempts INTEGER DEFAULT 3"))
        else:
            if not _pg_column_exists(conn, "evaluations", "max_attempts"):
                conn.execute(text("ALTER TABLE evaluations ADD COLUMN max_attempts INTEGER DEFAULT 3"))


def create_tables():
    Base.metadata.create_all(bind=engine)
    ensure_i18n_columns()
    ensure_evaluation_columns()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
