"""
The platform's parameters ("parámetros"): one row of JSON with every default filled in on read. Read once per request
(cached on its session); saved by merging what changed into what is stored.
"""

from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import Session

from app.db.models import AppSettings, User
from app.schemas.settings import PlatformSettings

ROW_ID = 1
_CACHE = "platform_settings"  # key in the session's `info`: a session lives as long as its request

# What a wrong value gets back, in the admin's words (by section and key, whatever is wrong with it).
_MESSAGES = {
    ("general", "company_name"): "El nombre de la empresa debe ser un texto de hasta 200 caracteres.",
    ("general", "timezone"): "La zona horaria no existe: elige una de la lista (por ejemplo, «America/Bogota»).",
    ("evaluations", "passing_score"): "La nota mínima por defecto debe ser un número entero entre 1 y 100.",
    ("evaluations", "max_attempts"): "Los intentos por defecto deben ser un número entero entre 1 y 10.",
    ("certificates", "enabled"): "Indica si los certificados se emiten automáticamente (sí o no).",
    ("certificates", "validity_months"): "La vigencia debe ser un número entero de meses entre 1 y 120, o quedar "
                                         "vacía para que no venzan.",
    ("certificates", "min_score"): "La nota mínima promedio debe ser un número entero entre 0 y 100, o quedar vacía.",
    ("certificates", "signer_name"): "El nombre del firmante debe ser un texto de hasta 200 caracteres.",
    ("certificates", "signer_title"): "El cargo del firmante debe ser un texto de hasta 200 caracteres.",
}
_INVALID = "Revisa los parámetros: hay un valor que no es válido."


class InvalidSettings(ValueError):
    """A value out of range, or of the wrong kind; the message is the admin's."""


def parse(values: dict[str, Any] | None) -> PlatformSettings:
    """The stored values over the defaults. A section edited by hand into something invalid falls back to its
    defaults: the platform never breaks over it."""
    values = values if isinstance(values, dict) else {}
    try:
        return PlatformSettings.model_validate(values)
    except ValidationError:
        sections = {}
        for name, field in PlatformSettings.model_fields.items():
            try:
                sections[name] = field.annotation.model_validate(values.get(name) or {})
            except ValidationError:
                continue
        return PlatformSettings(**sections)


def saved(db: Session) -> bool:
    """An admin saved the parameters at least once (until then, every one is its default)."""
    return db.execute(select(AppSettings.id).where(AppSettings.id == ROW_ID)).first() is not None


def get_settings(db: Session) -> PlatformSettings:
    settings = db.info.get(_CACHE)
    if settings is None:
        stored = db.execute(select(AppSettings.values).where(AppSettings.id == ROW_ID)).scalar()
        settings = db.info[_CACHE] = parse(stored)
    return settings


def _merge(base: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in changes.items():
        nested = isinstance(value, dict) and isinstance(base.get(key), dict)
        merged[key] = _merge(base[key], value) if nested else value
    return merged


def _message(error: ValidationError) -> str:
    location = tuple(str(part) for part in error.errors()[0]["loc"][:2])
    return _MESSAGES.get(location, _INVALID)


def _locked_row(db: Session) -> AppSettings:
    """The row, locked: two admins saving at once each merge into what the other saved. Created on first save."""
    query = db.query(AppSettings).filter(AppSettings.id == ROW_ID).with_for_update().populate_existing()
    row = query.first()
    if row is None:
        insert = postgresql.insert if db.get_bind().dialect.name == "postgresql" else sqlite.insert
        db.execute(insert(AppSettings).values(id=ROW_ID, values={}).on_conflict_do_nothing(index_elements=["id"]))
        row = query.one()
    return row


def update_settings(db: Session, changes: dict[str, Any], user: User) -> PlatformSettings:
    """Merge what changed (section by section, key by key) and validate the result; raises `InvalidSettings`. What is
    stored is the whole set, valid and without unknown keys."""
    row = _locked_row(db)
    current = parse(row.values).model_dump(mode="json")
    try:
        settings = PlatformSettings.model_validate(_merge(current, changes))
    except ValidationError as exc:
        raise InvalidSettings(_message(exc)) from exc
    row.values = settings.model_dump(mode="json")
    row.updated_by = user.id
    row.updated_at = datetime.now(timezone.utc)  # also when the values come out the same
    db.info[_CACHE] = settings
    return settings
