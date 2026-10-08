from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JSONDict, JSONList, updated_at_column

# queued -> running -> done | error (the AI could not look at it: what the video's own checks found is kept)
REVIEW_STATUSES = ("queued", "running", "done", "error")
REVIEW_VERDICTS = ("ok", "warnings", "problems")


class VideoReview(Base):
    """The automatic review of one of a module's AI videos: frames taken where its slides are settled and at random,
    what FFmpeg and the layout lint tell about it on their own, and what the AI sees in the frames and reads in the
    script. Each finding (`issues`) can be dismissed, or fixed by a change to the script; the video itself is never
    touched. A video can be reviewed again: the newest review of the module's video is the one shown."""

    __tablename__ = "video_reviews"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    module_id: Mapped[int] = mapped_column(ForeignKey("modules.id", ondelete="CASCADE"), index=True)
    # The video it reviews (None once it is deleted: the review stays as history).
    video_asset_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("media_assets.id", ondelete="SET NULL"), index=True
    )
    render_job_id: Mapped[str | None] = mapped_column(String(36))  # the render that made the video: its timings
    job_id: Mapped[str | None] = mapped_column(String(36))  # the "video.review" job
    status: Mapped[str] = mapped_column(String(20), default="queued", server_default="queued")
    verdict: Mapped[str | None] = mapped_column(String(20))
    summary: Mapped[str] = mapped_column(Text, default="", server_default="")
    error: Mapped[str | None] = mapped_column(Text)
    # [{id, source, scene_id, at_seconds, frame_index, category, severity, description, suggested_observation, box,
    #   status}] (see schemas.reviews)
    issues: Mapped[list[Any]] = mapped_column(JSONList, default=list, server_default=text("'[]'"))
    # [{index, at_seconds, scene_id, kind, path}]: the frames, in the video's folder
    frames: Mapped[list[Any]] = mapped_column(JSONList, default=list, server_default=text("'[]'"))
    checks: Mapped[dict[str, Any] | None] = mapped_column(JSONDict)  # what the video's own checks found
    model: Mapped[str | None] = mapped_column(String(100))
    usage: Mapped[dict[str, Any] | None] = mapped_column(JSONDict)  # the model's: {prompt_tokens, completion_tokens}
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = updated_at_column()
