from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JSONDict

# At most one certificate in force (not revoked) per person and course, and per person and path.
_IN_FORCE = {kind: text(f"kind = '{kind}' AND revoked_at IS NULL") for kind in ("course", "path")}


class Certificate(Base):
    """A certificate a learner earned by completing a course or a learning path. It is a snapshot (who, what, hours,
    score, issuer and signer as they were): later edits never change it. The previous app's `certificates` table is
    another one, no longer mapped."""

    __tablename__ = "learner_certificates"
    __table_args__ = (
        UniqueConstraint("code", name="uq_learner_certificates_code"),
        Index("uq_learner_certificates_course", "user_id", "course_id", unique=True,
              postgresql_where=_IN_FORCE["course"], sqlite_where=_IN_FORCE["course"]),
        Index("uq_learner_certificates_path", "user_id", "path_id", unique=True,
              postgresql_where=_IN_FORCE["path"], sqlite_where=_IN_FORCE["path"]),
    )

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    # RC-XXXX-XXXX: what anyone types or follows to verify it.
    code: Mapped[str] = mapped_column(String(16))
    kind: Mapped[str] = mapped_column(String(10))  # course | path
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # Null once the course or path is deleted: the certificate stays, with its snapshot.
    course_id: Mapped[int | None] = mapped_column(ForeignKey("courses.id", ondelete="SET NULL"), index=True)
    path_id: Mapped[int | None] = mapped_column(ForeignKey("learning_paths.id", ondelete="SET NULL"), index=True)
    recipient_name: Mapped[str] = mapped_column(String(200))
    title: Mapped[str] = mapped_column(String(300))
    # {hours, modules, courses: [{title, completed_at}], average_score, company_name, signer_name, signer_title}
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONDict)
    score: Mapped[float | None]  # the average of the learner's best quiz scores (None: no quiz taken)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_reason: Mapped[str | None] = mapped_column(Text)
