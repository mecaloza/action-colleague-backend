from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JSONDict, updated_at_column

REVISION_TARGETS = ("script", "quiz", "activity")
# observation: asked by an admin | qa: from the video's review | manual: saved in the editor | regenerate: the whole
# script rewritten by the AI | revert: an earlier change undone
REVISION_SOURCES = ("observation", "qa", "manual", "regenerate", "revert")
# proposing -> proposed -> applied | discarded | failed | superseded; an applied one that was undone: reverted
REVISION_STATUSES = ("proposing", "proposed", "applied", "discarded", "failed", "superseded", "reverted")


class ModuleRevision(Base):
    """A change to a module's content (its script; its quiz and activities later): one the AI proposes from an admin's
    observation, or one already made (saved by hand, rewritten, undone). It keeps the content before and after it, so
    an applied change can be undone, and what changed (`changes`: per scene, the reading, the cost of producing it)."""

    __tablename__ = "module_revisions"
    __table_args__ = (Index("ix_module_revisions_module_created", "module_id", "created_at"),)  # a module's history

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    module_id: Mapped[int] = mapped_column(ForeignKey("modules.id", ondelete="CASCADE"))
    target: Mapped[str] = mapped_column(String(10), default="script", server_default="script")
    activity_id: Mapped[int | None] = mapped_column(ForeignKey("activities.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20))
    observation: Mapped[str] = mapped_column(Text, default="", server_default="")
    # What the change may touch: {scene_ids, question_ids, qa_issue_id} (an undo also names the change it undid).
    scope: Mapped[dict[str, Any] | None] = mapped_column(JSONDict)
    summary: Mapped[str] = mapped_column(Text, default="", server_default="")
    unresolved: Mapped[str] = mapped_column(Text, default="", server_default="")  # what the AI could not do, and why
    # The content the proposal was made from (a script: its scenes and reading): what changed since is rebased on.
    base_hash: Mapped[str | None] = mapped_column(String(64))
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONDict)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONDict)
    changes: Mapped[dict[str, Any] | None] = mapped_column(JSONDict)
    job_id: Mapped[str | None] = mapped_column(String(36))
    error: Mapped[str | None] = mapped_column(Text)
    usage: Mapped[dict[str, Any] | None] = mapped_column(JSONDict)  # the model's: {prompt_tokens, completion_tokens}
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    applied_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = updated_at_column()
