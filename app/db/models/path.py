from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JSONDict, updated_at_column


class LearningPath(Base):
    """Courses in an order, for the people of some profiles (and anyone assigned by hand)."""

    __tablename__ = "learning_paths"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="", server_default="")
    # draft (only admins see it) | published (assigned people take it) | archived (hidden, results kept)
    status: Mapped[str] = mapped_column(String(20), default="draft", server_default="draft")
    # PathSettings: sequential unlock, days to finish, certificate.
    settings: Mapped[dict[str, Any] | None] = mapped_column(JSONDict)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = updated_at_column()
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LearningPathProfile(Base):
    __tablename__ = "learning_path_profiles"

    path_id: Mapped[int] = mapped_column(ForeignKey("learning_paths.id", ondelete="CASCADE"), primary_key=True)
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("job_profiles.id", ondelete="CASCADE"), primary_key=True, index=True
    )


class LearningPathCourse(Base):
    __tablename__ = "learning_path_courses"
    __table_args__ = (UniqueConstraint("path_id", "course_id", name="uq_learning_path_courses_path_course"),)

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    path_id: Mapped[int] = mapped_column(ForeignKey("learning_paths.id", ondelete="CASCADE"))
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id", ondelete="CASCADE"), index=True)
    order: Mapped[int] = mapped_column()


class PathAssignment(Base):
    """A person taking a path. Their courses are ordinary enrollments, created when the path is published."""

    __tablename__ = "path_assignments"
    __table_args__ = (UniqueConstraint("path_id", "user_id", name="uq_path_assignments_path_user"),)

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    path_id: Mapped[int] = mapped_column(ForeignKey("learning_paths.id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # profile: given by the person's profile (follows it) | manual: assigned by an admin
    source: Mapped[str] = mapped_column(String(20))
    assigned_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # When every published course of the path was completed (cleared if the path gets a course to do again).
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
