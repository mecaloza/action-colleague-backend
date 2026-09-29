import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.mutable import MutableDict, MutableList
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# JSONB on Postgres (as created by the migrations). Mutable*: in-place changes such as
# `job.state["render_id"] = ...` are detected and saved; plain JSON columns silently drop them.
def _json_type():
    return JSON().with_variant(JSONB(), "postgresql")


# One type instance each: `as_mutable` applies to every column that shares the instance.
JSONDict = MutableDict.as_mutable(_json_type())
JSONList = MutableList.as_mutable(_json_type())


# Timestamps: columns from the previous app are naive UTC (`created_at`); new ones are timezone-aware
# (`updated_at`, `published_at`...). Normalize before comparing them in Python.


class Base(DeclarativeBase):
    # Fetch server-generated values (updated_at on UPDATE) with RETURNING instead of expiring them.
    __mapper_args__ = {"eager_defaults": True}


def new_uuid() -> str:
    return str(uuid.uuid4())


def updated_at_column() -> Mapped[datetime]:
    """`updated_at`: filled by the database on insert and refreshed by the ORM on every UPDATE."""
    return mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
