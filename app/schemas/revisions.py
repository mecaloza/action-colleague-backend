from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, StringConstraints, field_validator

from app.schemas.common import UtcDatetime, required_text
from app.schemas.media import JobOut

RevisionTarget = Literal["script", "quiz", "activity"]
RevisionSource = Literal["observation", "qa", "manual", "regenerate", "revert"]
RevisionStatus = Literal["proposing", "proposed", "applied", "discarded", "failed", "superseded", "reverted"]
SceneId = Annotated[str, StringConstraints(min_length=1, max_length=40)]


class RevisionCreate(BaseModel):
    target: Literal["script"] = "script"  # (the quiz and the activities come next)
    observation: str = Field(min_length=1, max_length=4000)
    scene_ids: list[SceneId] | None = Field(default=None, max_length=40)  # none (or empty): the whole script
    qa_issue_id: str | None = Field(default=None, max_length=64)  # the video review's finding it fixes

    _observation = field_validator("observation")(classmethod(lambda cls, v: required_text(v)))


class RevisionApply(BaseModel):
    render: bool = False  # and produce the module's video again (when it has one)


class RevisionScope(BaseModel):
    scene_ids: list[str] = Field(default_factory=list)
    question_ids: list[str] = Field(default_factory=list)
    qa_issue_id: str | None = None
    reverted_id: int | None = None  # an undo: the change it undid


class FieldChange(BaseModel):
    before: Any = None
    after: Any = None


class TextOp(BaseModel):
    op: Literal["equal", "insert", "delete"]
    text: str


class ScenePosition(BaseModel):
    """Where the scene is in the script (1 = first), before and after the change (None: not there)."""

    before: int | None = None
    after: int | None = None


class SceneChange(BaseModel):
    scene_id: str
    action: Literal["update", "insert", "delete", "move"]  # move: same content, another place
    position: ScenePosition
    # What changed, by field: layout, title, subtitle, points, icons, icon, stat {stat_value, stat_label}, quote,
    # comparison {left, right}, chart {chart_labels, chart_values, chart_unit}, eyebrow, narration, visual.
    fields: dict[str, FieldChange] = Field(default_factory=dict)
    reason: str = ""
    narration_diff: list[TextOp] = Field(default_factory=list)  # word by word, when the narration changed
    before: dict[str, Any] | None = None  # the whole scene ({id, slide, narration, visual}), for its preview
    after: dict[str, Any] | None = None


class ReadingChange(BaseModel):
    before: str
    after: str


class RevisionImpact(BaseModel):
    """What producing the video again takes after the change."""

    narration_scenes: list[str] = Field(default_factory=list)  # scenes narrated again
    narration_chars: int = 0  # their characters
    presenter_takes: int = 0  # presenter takes recorded again
    reassigns_presenters: bool = False  # a scene added or removed swaps the presenters of the ones after it
    visuals_to_make: int = 0  # infographics, images or clips to make
    needs_render: bool = False  # the module has a video and its scenes changed


class IgnoredChange(BaseModel):
    """Something the AI proposed that was left out, and why (in Spanish)."""

    scene_id: str
    action: str  # update | insert_after | delete
    reason: str


class RevisionChanges(BaseModel):
    scenes: list[SceneChange] = Field(default_factory=list)
    reading: ReadingChange | None = None
    impact: RevisionImpact | None = None
    ignored: list[IgnoredChange] = Field(default_factory=list)
    affected_question_ids: list[str] = Field(default_factory=list)  # quiz questions the new script may contradict
    rebased: bool = False  # applied over other edits made after the proposal


class RevisionSummary(BaseModel):
    id: int
    module_id: int
    target: RevisionTarget
    activity_id: int | None = None
    source: RevisionSource
    status: RevisionStatus
    observation: str
    summary: str
    created_at: UtcDatetime | None = None
    applied_at: UtcDatetime | None = None
    created_by_name: str | None = None


class Revision(RevisionSummary):
    scope: RevisionScope
    unresolved: str
    changes: RevisionChanges | None = None  # None while it is being proposed (or when it failed)
    error: str | None = None
    job_id: str | None = None


class RevisionProposal(BaseModel):
    revision: Revision
    job: JobOut


class RevisionApplied(BaseModel):
    revision: Revision
    render_job: JobOut | None = None
