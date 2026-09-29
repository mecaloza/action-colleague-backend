from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, false, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Enrollment(Base):
    __tablename__ = "enrollments"
    __table_args__ = (UniqueConstraint("user_id", "course_id", name="uq_enrollments_user_course"),)

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"))
    status: Mapped[str] = mapped_column(String(20), default="assigned")  # assigned | in_progress | completed
    progress_pct: Mapped[float | None] = mapped_column(default=0.0)
    assigned_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL", name="fk_enrollments_assigned_by")
    )
    enrolled_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user = relationship("User", back_populates="enrollments", foreign_keys=[user_id])
    course = relationship("Course", back_populates="enrollments")
    module_progress = relationship("ModuleProgress", back_populates="enrollment")


class ModuleProgress(Base):
    __tablename__ = "module_progress"
    __table_args__ = (
        UniqueConstraint("enrollment_id", "module_id", name="uq_module_progress_enrollment_module"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    enrollment_id: Mapped[int] = mapped_column(ForeignKey("enrollments.id"))
    module_id: Mapped[int] = mapped_column(ForeignKey("modules.id"))
    completed: Mapped[bool | None] = mapped_column(default=False)
    passed: Mapped[bool | None] = mapped_column(default=False, server_default=false())
    score: Mapped[float | None]
    attempts: Mapped[int | None] = mapped_column(default=0, server_default="0")
    # Where the learner left the module's video, to resume playback.
    last_position_seconds: Mapped[float] = mapped_column(default=0, server_default="0")
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)

    enrollment = relationship("Enrollment", back_populates="module_progress")
    module = relationship("Module", back_populates="progress_records")
