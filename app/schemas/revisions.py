from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, StringConstraints, field_validator

from app.schemas.common import UtcDatetime, required_text
from app.schemas.media import JobOut

RevisionTarget = Literal["script", "quiz", "activity"]
RevisionSource = Literal["observation", "qa", "manual", "regenerate", "revert"]
RevisionStatus = Literal["proposing", "proposed", "applied", "discarded", "failed", "superseded", "reverted"]
ChangeAction = Literal["update", "insert", "delete", "move"]  # move: the same content, in another place
ItemId = Annotated[str, StringConstraints(min_length=1, max_length=40)]  # a scene's or a question's


class RevisionCreate(BaseModel):
    target: RevisionTarget = "script"
    observation: str = Field(min_length=1, max_length=4000)
    scene_ids: list[ItemId] | None = Field(default=None, max_length=40)  # script: none (or empty) = the whole script
    question_ids: list[ItemId] | None = Field(default=None, max_length=30)  # quiz: none (or empty) = all of it
    activity_id: int | None = None  # activity: the one it changes (required for that target)
    qa_issue_id: str | None = Field(default=None, max_length=64)  # the video review's finding it fixes

    _observation = field_validator("observation")(classmethod(lambda cls, v: required_text(v)))


class RevisionApply(BaseModel):
    render: bool = False  # and produce the module's video again (a script's change, when the module has a video)


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


class Position(BaseModel):
    """Where the scene or question is (1 = first), before and after the change (None: not there)."""

    before: int | None = None
    after: int | None = None


class SceneChange(BaseModel):
    scene_id: str
    action: ChangeAction
    position: Position
    # What changed, by field: layout, title, subtitle, points, icons, icon, stat {stat_value, stat_label}, quote,
    # comparison {left, right}, chart {chart_labels, chart_values, chart_unit}, eyebrow, narration, visual.
    fields: dict[str, FieldChange] = Field(default_factory=dict)
    reason: str = ""
    narration_diff: list[TextOp] = Field(default_factory=list)  # word by word, when the narration changed
    before: dict[str, Any] | None = None  # the whole scene ({id, slide, narration, visual}), for its preview
    after: dict[str, Any] | None = None


class QuestionChange(BaseModel):
    question_id: str  # its id before the change (a new question: its own)
    action: ChangeAction
    position: Position
    # An update's fields that changed (prompt, scenario, options, correct_index, correct, items, pairs, answers,
    # hint, explanation; type and all of them when it became another kind of question).
    fields: dict[str, FieldChange] = Field(default_factory=dict)
    reason: str = ""
    # The whole question (canonical format). When what is answered changes, `after` has a new id: the answers given
    # to the old wording, and its statistics, stay with the old one.
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None


class EntityChange(BaseModel):
    """A part of an activity that changed: an element of a list of its own (a situation, a choice, a decision, an
    item, a control) or another field of its spec, whole."""

    path: str  # "nodes/inicio", "nodes/inicio/choices/precio", "verdicts/retirar", "items/l3", "variables/km", "prompt"
    action: ChangeAction
    label: str  # for the admin: "Situación 2", "Opción 1 de la situación 2", "Control «Precio»", "Instrucción"
    before: Any = None  # an element without the elements inside it (added or removed: whole)
    after: Any = None


class ReadingChange(BaseModel):
    before: str
    after: str


class ActivityChange(BaseModel):
    title: ReadingChange | None = None
    intro: ReadingChange | None = None
    entities: list[EntityChange] = Field(default_factory=list)
    lines_changed: int = 0  # spoken lines it didn't say before: voiced again when it is published


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

    scene_id: str = ""  # a script's
    question_id: str = ""  # a quiz's
    action: str  # update | insert_after | delete
    reason: str


class RevisionChanges(BaseModel):
    scenes: list[SceneChange] = Field(default_factory=list)  # a script's
    reading: ReadingChange | None = None
    impact: RevisionImpact | None = None
    questions: list[QuestionChange] = Field(default_factory=list)  # a quiz's
    activity: ActivityChange | None = None  # an activity's
    ignored: list[IgnoredChange] = Field(default_factory=list)
    affected_question_ids: list[str] = Field(default_factory=list)  # quiz questions the new script may contradict
    rebased: bool = False  # applied over other edits made after the proposal


class RevisionLearners(BaseModel):
    """Who a change to a quiz or an activity reaches as soon as it is applied (a script's waits for its video)."""

    published: bool  # learners see it now (the course is published; an activity, published too)
    mid_quiz: int = 0  # learners in the middle of the quiz whose next attempt the change alters
    mid_play: int = 0  # learners with a play of the activity open: they start it again


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
    learners: RevisionLearners | None = None  # a quiz's or activity's proposal still pending


class RevisionProposal(BaseModel):
    revision: Revision
    job: JobOut


class RevisionApplied(BaseModel):
    revision: Revision
    render_job: JobOut | None = None
