"""Rows the previous app left in tables this app no longer maps (the database keeps them)."""

from collections.abc import Iterable
from functools import lru_cache

from sqlalchemy import bindparam, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session


@lru_cache
def _has_table(engine: Engine, name: str) -> bool:
    return inspect(engine).has_table(name)


def delete_certificates(db: Session, enrollment_ids: Iterable[int]) -> None:
    """The old certificates table references enrollments; clear it before deleting them."""
    ids = list(enrollment_ids)
    if ids and _has_table(db.get_bind(), "certificates"):
        db.execute(
            text("DELETE FROM certificates WHERE enrollment_id IN :ids").bindparams(bindparam("ids", expanding=True)),
            {"ids": ids},
        )
