from typing import Any

from pydantic import BaseModel, Field

from app.schemas.common import UtcDatetime
from app.schemas.courses import MediaRef


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


class QuizSummary(BaseModel):
    question_count: int
    max_attempts: int
    attempts_used: int
    passed: bool
    best_score: float | None = None
    passing_score: int


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


class LearnerQuiz(BaseModel):
    module_id: int
    questions: list[dict[str, Any]]
    max_attempts: int
    attempts_used: int
    passing_score: int
    passed: bool


class Answer(BaseModel):
    question_id: str = Field(min_length=1, max_length=40)
    response: Any = None


class AttemptCreate(BaseModel):
    answers: list[Answer] = Field(max_length=50)


class QuestionResult(BaseModel):
    question_id: str
    correct: bool
    explanation: str = ""
    expected: Any = None


class AttemptResult(BaseModel):
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


class CompletionResult(BaseModel):
    module_completed: bool
    next_module_id: int | None = None
    course_completed: bool


class PositionUpdate(BaseModel):
    seconds: float = Field(ge=0, le=24 * 3600)
