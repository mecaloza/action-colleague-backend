from datetime import datetime, timezone
from typing import Annotated

from pydantic import PlainSerializer


def _as_utc_iso(value: datetime) -> str:
    # Older columns store naive UTC timestamps; always emit an explicit offset so browsers
    # don't read them as local time.
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).isoformat()


UtcDatetime = Annotated[datetime, PlainSerializer(_as_utc_iso, return_type=str)]


def required_text(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("No puede quedar vacío")
    return value
