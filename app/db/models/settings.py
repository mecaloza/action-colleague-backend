from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JSONDict, updated_at_column


class AppSettings(Base):
    """The platform's parameters ("parámetros"): one row (id 1) of JSON, read with its defaults by
    `services.settings`."""

    __tablename__ = "app_settings"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    values: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict, server_default=text("'{}'"))
    updated_at: Mapped[datetime] = updated_at_column()
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
