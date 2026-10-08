from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

MILESTONES = ("module", "course", "path")
# A milestone is earned once per person (plays and quizzes add a row each time they beat the person's best).
_ONCE = text(f"source IN ({', '.join(repr(source) for source in MILESTONES)})")


class PointsEntry(Base):
    """Points a learner earned, and when: what a play or a quiz improved on their best (`activity:12`, `quiz:44`, by
    module), or a module, course or path completed (`module:44`, `course:18`, `path:3`). Rankings add them up."""

    __tablename__ = "learner_points"
    __table_args__ = (
        Index("ix_learner_points_user_earned", "user_id", "earned_at"),
        Index("ix_learner_points_course_earned", "course_id", "earned_at"),
        Index("ix_learner_points_path_earned", "path_id", "earned_at"),
        Index("uq_learner_points_milestone", "user_id", "source_key", unique=True,
              postgresql_where=_ONCE, sqlite_where=_ONCE),
    )

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    # The course they were earned in (a path's own milestone has none): deleting it takes them away.
    course_id: Mapped[int | None] = mapped_column(ForeignKey("courses.id", ondelete="CASCADE"))
    path_id: Mapped[int | None] = mapped_column(ForeignKey("learning_paths.id", ondelete="CASCADE"))
    source: Mapped[str] = mapped_column(String(20))  # activity | quiz | module | course | path
    source_key: Mapped[str] = mapped_column(String(80))
    points: Mapped[int] = mapped_column()
    earned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
