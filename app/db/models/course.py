from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, JSONDict, updated_at_column


class Course(Base):
    __tablename__ = "courses"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text, default="")
    language: Mapped[str | None] = mapped_column(String(5), default="es", server_default="es")
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft | published | archived
    source: Mapped[str] = mapped_column(String(20), default="manual", server_default="manual")  # ai | manual
    # Studio settings: tone, audience, voice, avatar, presenter on/off, theme.
    settings: Mapped[dict[str, Any] | None] = mapped_column(JSONDict)
    cover_asset_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("media_assets.id", ondelete="SET NULL", name="fk_courses_cover_asset")
    )
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now())
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = updated_at_column()

    creator = relationship("User")
    modules = relationship("Module", back_populates="course", order_by="Module.order")
    enrollments = relationship("Enrollment", back_populates="course")


def _asset_fk(column: str) -> ForeignKey:
    return ForeignKey("media_assets.id", ondelete="SET NULL", name=f"fk_modules_{column}")


class Module(Base):
    __tablename__ = "modules"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"))
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str] = mapped_column(Text, default="", server_default="")
    order: Mapped[int] = mapped_column(default=0)
    # ai | upload | recording | text | document
    source: Mapped[str] = mapped_column(String(20), default="text", server_default="text")
    content_text: Mapped[str | None] = mapped_column(Text, default="")
    storyboard: Mapped[dict[str, Any] | None] = mapped_column(JSONDict)
    video_asset_id: Mapped[str | None] = mapped_column(String(36), _asset_fk("video_asset_id"))
    poster_asset_id: Mapped[str | None] = mapped_column(String(36), _asset_fk("poster_asset_id"))
    captions_asset_id: Mapped[str | None] = mapped_column(String(36), _asset_fk("captions_asset_id"))
    document_asset_id: Mapped[str | None] = mapped_column(String(36), _asset_fk("document_asset_id"))
    duration_seconds: Mapped[float | None]
    # pending | queued | generating | completed | failed
    generation_status: Mapped[str | None] = mapped_column(String(50), default="pending", server_default="pending")
    generation_error: Mapped[str | None] = mapped_column(Text)
    # URLs written by the previous app (HeyGen references, public Storage URLs, /uploads paths).
    video_url: Mapped[str | None] = mapped_column(Text, default="")
    audio_url: Mapped[str | None] = mapped_column(String(500), default="", server_default="")
    created_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = updated_at_column()

    course = relationship("Course", back_populates="modules")
    evaluation = relationship("Evaluation", back_populates="module", uselist=False)
    progress_records = relationship("ModuleProgress", back_populates="module")
