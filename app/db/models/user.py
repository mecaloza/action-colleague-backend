from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, String, Text, func, true
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    password_hash: Mapped[str | None] = mapped_column(String(300), default="", server_default="")
    role: Mapped[str] = mapped_column(String(20), default="collaborator")  # admin | collaborator
    position: Mapped[str | None] = mapped_column(String(200), default="")
    department: Mapped[str | None] = mapped_column(String(200), default="")
    preferred_language: Mapped[str | None] = mapped_column(String(5), default="es", server_default="es")
    # Legacy org-chart and permissions data: kept in the database, no longer exposed by the API.
    reports_to: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    permissions_json: Mapped[str | None] = mapped_column(Text, default="[]", server_default="[]")
    is_active: Mapped[bool | None] = mapped_column(default=True, server_default=true())
    hire_date: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now())

    enrollments = relationship("Enrollment", back_populates="user", foreign_keys="Enrollment.user_id")


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    token: Mapped[str] = mapped_column(String(300), unique=True, index=True)  # sha256 of the token
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked: Mapped[bool | None] = mapped_column(default=False)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now())

    user = relationship("User")
