from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, JSONDict, updated_at_column

ACTIVITY_TYPES = ("decision", "inspection", "simulator", "quick")


class Activity(Base):
    """An interactive activity after a module's video: a client decision, a visual inspection, a simulator or a
    quick timed challenge. `spec` holds everything, answers included: only the server reads it."""

    __tablename__ = "activities"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    module_id: Mapped[int] = mapped_column(ForeignKey("modules.id", ondelete="CASCADE"), index=True)
    order: Mapped[int] = mapped_column(default=0, server_default="0")
    type: Mapped[str] = mapped_column(String(20))  # one of ACTIVITY_TYPES
    title: Mapped[str] = mapped_column(String(200))
    intro: Mapped[str] = mapped_column(Text, default="", server_default="")
    spec: Mapped[dict[str, Any]] = mapped_column(JSONDict)
    # Bumped on every edit: an attempt started on an older version can't go on.
    version: Mapped[int] = mapped_column(default=1, server_default="1")
    # draft (only admins see it) | ready (learners play it)
    status: Mapped[str] = mapped_column(String(20), default="draft", server_default="draft")
    # ai: proposed by the AI and untouched (a new proposal may replace it) | admin: written or edited by an admin
    source: Mapped[str] = mapped_column(String(10), default="admin", server_default="admin")
    image_asset_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("media_assets.id", ondelete="SET NULL"))
    # One voice clip per spoken line: {line key: {"asset_id", "text_hash"}}.
    narration: Mapped[dict[str, Any] | None] = mapped_column(JSONDict)
    # none | pending | ready | failed
    voice_status: Mapped[str] = mapped_column(String(20), default="none", server_default="none")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = updated_at_column()

    module = relationship("Module", back_populates="activities")
    attempts = relationship("ActivityAttempt", back_populates="activity", cascade="all, delete-orphan",
                            passive_deletes=True)


class ActivityAttempt(Base):
    """One play of an activity. `state` is the server's record of the game (current step, zones found, deadline...):
    the learner only ever sees what they have earned."""

    __tablename__ = "activity_attempts"
    __table_args__ = (
        Index("ix_activity_attempts_enrollment_activity", "enrollment_id", "activity_id"),
        Index("ix_activity_attempts_user_finished", "user_id", "finished_at"),  # a person's plays, newest first
    )

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    activity_id: Mapped[int] = mapped_column(ForeignKey("activities.id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    # None for an admin's preview: it never counts towards anyone's progress.
    enrollment_id: Mapped[int | None] = mapped_column(ForeignKey("enrollments.id", ondelete="CASCADE"))
    version: Mapped[int] = mapped_column()
    state: Mapped[dict[str, Any]] = mapped_column(JSONDict)
    score: Mapped[int] = mapped_column(default=0, server_default="0")
    max_score: Mapped[int] = mapped_column(default=0, server_default="0")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    activity = relationship("Activity", back_populates="attempts")
