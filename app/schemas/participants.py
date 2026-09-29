from pydantic import BaseModel, Field

from app.schemas.common import UtcDatetime


class ParticipantUser(BaseModel):
    id: int
    name: str
    email: str
    position: str = ""
    department: str = ""


class Participant(BaseModel):
    enrollment_id: int
    user: ParticipantUser
    status: str
    progress_pct: float
    completed_modules: int
    total_modules: int
    average_score: float | None = None
    enrolled_at: UtcDatetime | None = None
    completed_at: UtcDatetime | None = None
    last_activity_at: UtcDatetime | None = None


class ParticipantsAdd(BaseModel):
    user_ids: list[int] = Field(min_length=1, max_length=500)


class AttemptQuestionResult(BaseModel):
    question_id: str
    prompt: str
    correct: bool


class AttemptDetail(BaseModel):
    attempt_number: int
    score: float | None = None
    passed: bool
    created_at: UtcDatetime | None = None
    results: list[AttemptQuestionResult]


class ModuleAttempts(BaseModel):
    module_id: int
    module_title: str
    attempts: list[AttemptDetail]


class QuestionAnalytics(BaseModel):
    question_id: str
    prompt: str
    type: str
    responses: int
    correct: int
    accuracy: float | None = None


class ModuleAnalytics(BaseModel):
    module_id: int
    title: str
    attempts: int
    learners: int
    pass_rate: float
    average_score: float | None = None
    questions: list[QuestionAnalytics]


class CourseAnalytics(BaseModel):
    course_id: int
    modules: list[ModuleAnalytics]
