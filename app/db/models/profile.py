from datetime import datetime

from sqlalchemy import DateTime, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, updated_at_column


class JobProfile(Base):
    """A role in the company ("Asesor de flotas"): the people who have it get the learning paths linked to it."""

    __tablename__ = "job_profiles"
    # Case-insensitive uniqueness is checked by the API; the constraint catches two identical names at once.
    __table_args__ = (UniqueConstraint("name", name="uq_job_profiles_name"),)

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = updated_at_column()
