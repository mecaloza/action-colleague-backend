from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, JSONDict, new_uuid, updated_at_column


class MediaAsset(Base):
    """A file in object storage (video, audio, image, document, captions)."""

    __tablename__ = "media_assets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    # video | audio | image | document | captions | recording | deck
    kind: Mapped[str] = mapped_column(String(20))
    # pending | uploaded | processing | ready | failed
    status: Mapped[str] = mapped_column(String(20), default="pending", server_default="pending")
    bucket: Mapped[str] = mapped_column(String(100))
    path: Mapped[str] = mapped_column(String(500))
    mime_type: Mapped[str] = mapped_column(String(120), default="", server_default="")
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    duration_seconds: Mapped[float | None]
    width: Mapped[int | None]
    height: Mapped[int | None]
    original_filename: Mapped[str | None] = mapped_column(String(300))
    meta: Mapped[dict[str, Any] | None] = mapped_column(JSONDict)
    error: Mapped[str | None] = mapped_column(Text)
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    course_id: Mapped[int | None] = mapped_column(index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = updated_at_column()


class UserVideo(Base):
    """Recordings uploaded by the previous app (never linked to modules); migrated to media_assets later."""

    __tablename__ = "user_videos"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, index=True)
    module_id: Mapped[int | None] = mapped_column(ForeignKey("modules.id"))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    storage_url: Mapped[str] = mapped_column(String(500))
    duration: Mapped[int | None]  # seconds
    file_size: Mapped[int | None]  # bytes
    format: Mapped[str | None] = mapped_column(String(20), default="webm")  # webm | mp4
    created_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now())
    status: Mapped[str | None] = mapped_column(String(20), default="uploaded")  # uploaded | processing | ready

    module = relationship("Module")
    user = relationship("User")
