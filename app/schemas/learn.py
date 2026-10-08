from typing import Annotated, Any

from pydantic import BaseModel, Field, StrictBool

from app.schemas.certificates import CertificateRef
from app.schemas.common import UtcDatetime
from app.schemas.courses import MediaRef
from app.schemas.paths import CoursePath, CourseRef, PathRef


class LearnerCourse(BaseModel):
    course_id: int
    title: str
    description: str
    cover_url: str | None = None
    status: str
    progress_pct: float
    total_modules: int
    completed_modules: int
    next_module_id: int | None = None
    next_module_title: str | None = None
    total_duration_seconds: float = 0
    enrolled_at: UtcDatetime | None = None
    completed_at: UtcDatetime | None = None
    # Learning paths: a course of a sequential path opens once the earlier ones are completed.
    locked: bool = False
    locked_by: CourseRef | None = None  # the course to complete first
    path: CoursePath | None = None  # the first of the learner's paths with this course
    certificate: CertificateRef | None = None  # earned by completing it (not revoked)


class QuizSummary(BaseModel):
    question_count: int
    max_attempts: int
    attempts_used: int
    passed: bool
    best_score: float | None = None
    passing_score: int


class LearnerActivitySummary(BaseModel):
    id: int
    type: str
    title: str
    intro: str
    finished: bool
    best_score: int | None = None
    max_score: int
    resumable: bool  # an open play that can go on: starting the activity picks it up where it was left


class LearnerModule(BaseModel):
    id: int
    title: str
    description: str
    order: int
    source: str
    unlocked: bool
    completed: bool
    duration_seconds: float | None = None
    video: MediaRef | None = None
    poster_url: str | None = None
    captions_url: str | None = None
    document: MediaRef | None = None
    content_text: str | None = None
    last_position_seconds: float = 0
    quiz: QuizSummary | None = None
    activities: list[LearnerActivitySummary] = []


class LearnerCourseInfo(BaseModel):
    id: int
    title: str
    description: str
    language: str


class LearnerEnrollment(BaseModel):
    id: int
    status: str
    progress_pct: float
    completed_at: UtcDatetime | None = None


class LearnerCourseDetail(BaseModel):
    course: LearnerCourseInfo
    enrollment: LearnerEnrollment | None = None
    modules: list[LearnerModule]
    locked: bool = False  # by a learning path: no module opens until `locked_by` is completed
    locked_by: CourseRef | None = None
    path: CoursePath | None = None
    certificate: CertificateRef | None = None


class LearnerQuiz(BaseModel):
    module_id: int
    questions: list[dict[str, Any]]
    max_attempts: int
    attempts_used: int
    passing_score: int
    passed: bool
    version: str  # send it back with the attempt: a quiz edited meanwhile is not graded


Token = Annotated[str, Field(max_length=32)]


class OptionResponse(BaseModel):
    option: Token


class BoolResponse(BaseModel):
    value: StrictBool


class OrderResponse(BaseModel):
    order: list[Token] = Field(max_length=8)


class MatchesResponse(BaseModel):
    matches: dict[Token, Token] = Field(max_length=8)


class TextResponse(BaseModel):
    text: str = Field(max_length=300)


class Answer(BaseModel):
    question_id: str = Field(min_length=1, max_length=40)
    # Bounded shapes: what is stored in answers_json can't grow with the request body.
    response: OptionResponse | BoolResponse | OrderResponse | MatchesResponse | TextResponse | None = None


class AttemptCreate(BaseModel):
    answers: list[Answer] = Field(max_length=50)
    version: str | None = Field(default=None, max_length=32)


class QuestionResult(BaseModel):
    question_id: str
    correct: bool
    explanation: str = ""
    expected: Any = None


class CompletedPath(PathRef):
    certificate: CertificateRef | None = None  # the path's, when it gives one


class PathOutcome(BaseModel):
    """What completing a course earned and opened in the learner's paths (null until the course is completed)."""

    certificate: CertificateRef | None = None  # the course's (when one was issued, now or before)
    next_course: CourseRef | None = None  # the next course of its path, now available
    path_completed: CompletedPath | None = None  # the path this course completed


class AttemptResult(PathOutcome):
    score: float
    passed: bool
    correct: int
    total: int
    attempts_used: int
    attempts_remaining: int
    results: list[QuestionResult]
    module_completed: bool
    next_module_id: int | None = None
    course_completed: bool


class CompletionResult(PathOutcome):
    module_completed: bool
    next_module_id: int | None = None
    course_completed: bool


class PositionUpdate(BaseModel):
    seconds: float = Field(ge=0, le=24 * 3600)


class ActivityStart(BaseModel):
    untimed: bool = False  # a quick challenge without its clock (and its time bonus), for those who need it


class ActivityPlay(BaseModel):
    attempt_id: int
    activity_id: int
    view: dict[str, Any]
    narration: dict[str, str]  # line key -> audio URL, for the lines this response reveals
    image_url: str | None = None  # a reference photo shown during the play, if the activity has one
    resumed: bool = False  # the open play, picked up where it was left (an admin's preview always starts anew)


class ActivityMoveIn(BaseModel):
    # One play: {"choice"} for a decision, {"item", "verdict"} for an inspection, {"values"} for a simulator,
    # {"card", "answer"?} for a quick challenge. Bounded: a move is a handful of fields.
    move: dict[str, Any] = Field(max_length=8)


class ActivityMoveOut(PathOutcome):
    result: dict[str, Any]
    done: bool
    narration: dict[str, str]
    # The move that ends the game also finishes the play: what `finish` tells, told now (null for any other move,
    # and in an admin's preview, which never counts).
    module_completed: bool | None = None
    next_module_id: int | None = None
    course_completed: bool | None = None


class ActivityFinishOut(PathOutcome):
    score: int
    max_score: int
    module_completed: bool
    next_module_id: int | None = None
    course_completed: bool
