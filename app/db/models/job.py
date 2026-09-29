from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Index, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JSONDict, new_uuid, updated_at_column


class Job(Base):
    """A unit of background work (AI generation, rendering, media processing)."""

    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_claim", "status", "run_after"),
        # At most one active job per dedupe key (see app.worker.queue.enqueue).
        Index(
            "uq_jobs_active_dedupe_key",
            "dedupe_key",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running')"),
            sqlite_where=text("status IN ('queued', 'running')"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    type: Mapped[str] = mapped_column(String(60), index=True)
    # queued | running | succeeded | failed | canceled
    status: Mapped[str] = mapped_column(String(20), default="queued", server_default="queued")
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONDict, default=dict)
    # Checkpoints a job keeps between runs (e.g. an external render id while it waits).
    state: Mapped[dict[str, Any] | None] = mapped_column(JSONDict, default=dict)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONDict)
    error: Mapped[str | None] = mapped_column(Text)
    progress: Mapped[int] = mapped_column(default=0, server_default="0")
    step: Mapped[str] = mapped_column(String(160), default="", server_default="")
    attempts: Mapped[int] = mapped_column(default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(default=3, server_default="3")
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Lease: a worker owns the job until this time and must renew it while working.
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(String(100))
    dedupe_key: Mapped[str | None] = mapped_column(String(200), index=True)
    course_id: Mapped[int | None] = mapped_column(index=True)
    module_id: Mapped[int | None] = mapped_column(index=True)
    created_by: Mapped[int | None]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = updated_at_column()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
