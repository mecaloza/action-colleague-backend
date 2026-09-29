from datetime import date, datetime
from typing import Any, List, Literal, Optional

from pydantic import BaseModel, field_validator

VALID_LANGUAGES = {"es", "en", "pt"}


# ── Auth ──────────────────────────────────────────────────────────────
class LoginRequest(BaseModel):
    email: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user_id: int
    role: str
    preferred_language: str = "es"

    @field_validator("preferred_language")
    @classmethod
    def validate_preferred_language(cls, value: str) -> str:
        if value not in VALID_LANGUAGES:
            raise ValueError("preferred_language must be one of: es, en, pt")
        return value


class RefreshRequest(BaseModel):
    refresh_token: str


# ── User ──────────────────────────────────────────────────────────────
class UserBase(BaseModel):
    name: str
    email: str
    role: str = "collaborator"
    position: str = ""
    department: str = ""
    preferred_language: str = "es"
    hire_date: Optional[date] = None

    @field_validator("preferred_language")
    @classmethod
    def validate_preferred_language(cls, value: str) -> str:
        if value not in VALID_LANGUAGES:
            raise ValueError("preferred_language must be one of: es, en, pt")
        return value


Role = Literal["admin", "collaborator"]


class UserCreate(UserBase):
    role: Role = "collaborator"
    password: str


class UserUpdate(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    role: Optional[Role] = None
    position: Optional[str] = None
    department: Optional[str] = None
    preferred_language: str = "es"
    hire_date: Optional[date] = None
    is_active: Optional[bool] = None
    password: Optional[str] = None

    @field_validator("preferred_language")
    @classmethod
    def validate_preferred_language(cls, value: str) -> str:
        if value not in VALID_LANGUAGES:
            raise ValueError("preferred_language must be one of: es, en, pt")
        return value


class UserOut(UserBase):
    id: int
    is_active: bool = True
    created_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


# ── Course ────────────────────────────────────────────────────────────
class CourseBase(BaseModel):
    title: str
    description: str = ""
    language: str = "es"
    status: str = "draft"
    created_by: Optional[int] = None

    @field_validator("language")
    @classmethod
    def validate_language(cls, value: str) -> str:
        if value not in VALID_LANGUAGES:
            raise ValueError("language must be one of: es, en, pt")
        return value


class CourseCreate(CourseBase):
    pass


class CourseUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    language: str = "es"
    status: Optional[str] = None

    @field_validator("language")
    @classmethod
    def validate_language(cls, value: str) -> str:
        if value not in VALID_LANGUAGES:
            raise ValueError("language must be one of: es, en, pt")
        return value


class CourseOut(CourseBase):
    id: int
    created_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


# ── Module ────────────────────────────────────────────────────────────
class ModuleBase(BaseModel):
    course_id: int
    title: str
    order: int = 0
    content_text: str = ""
    video_url: Optional[str] = ""
    audio_url: Optional[str] = ""
    generation_status: Optional[str] = "pending"


class ModuleCreate(ModuleBase):
    pass


class ModuleUpdate(BaseModel):
    title: Optional[str] = None
    order: Optional[int] = None
    content_text: Optional[str] = None
    video_url: Optional[str] = None


class ModuleOut(ModuleBase):
    id: int
    created_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


# ── Evaluation ────────────────────────────────────────────────────────
class EvaluationBase(BaseModel):
    module_id: int
    questions: List[Any] = []
    max_attempts: int = 3


class EvaluationCreate(EvaluationBase):
    pass


class EvaluationUpdate(BaseModel):
    questions: Optional[List[Any]] = None
    max_attempts: Optional[int] = None


class EvaluationOut(BaseModel):
    id: int
    module_id: int
    questions: List[Any] = []
    max_attempts: int = 3
    created_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


# ── Enrollment ────────────────────────────────────────────────────────
class EnrollmentBase(BaseModel):
    user_id: int
    course_id: int
    status: str = "assigned"
    progress_pct: float = 0.0


class EnrollmentCreate(BaseModel):
    user_id: int
    course_id: int


class EnrollmentUpdate(BaseModel):
    status: Optional[str] = None
    progress_pct: Optional[float] = None


class EnrollmentOut(EnrollmentBase):
    id: int
    enrolled_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


# ── ModuleProgress ────────────────────────────────────────────────────
class ModuleProgressBase(BaseModel):
    enrollment_id: int
    module_id: int
    completed: bool = False
    passed: bool = False
    score: Optional[float] = None
    attempts: int = 0


class ModuleProgressCreate(ModuleProgressBase):
    pass


class ModuleProgressUpdate(BaseModel):
    completed: Optional[bool] = None
    passed: Optional[bool] = None
    score: Optional[float] = None
    attempts: Optional[int] = None


class ModuleProgressOut(ModuleProgressBase):
    id: int
    completed_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class EvaluationSubmit(BaseModel):
    enrollment_id: int
    module_id: int
    answers: List[Any]  # [{question_index: 0, selected: "b"}, ...]


class EvaluationResult(BaseModel):
    module_id: int
    enrollment_id: int
    score: float
    passed: bool
    correct: int
    total: int
    attempts: int
    attempts_remaining: int
    next_module_unlocked: bool


# ── Evaluation Analytics ─────────────────────────────────────────────
class QuestionAnalytics(BaseModel):
    question_index: int
    question_text: str
    question_type: str
    total_responses: int
    correct_count: int
    incorrect_count: int
    accuracy_pct: float
    most_common_wrong_answer: Optional[str] = None


class ModuleAnalytics(BaseModel):
    module_id: int
    module_title: str
    questions: List[QuestionAnalytics] = []


class CourseAnalyticsResponse(BaseModel):
    course_id: int
    modules: List[ModuleAnalytics] = []


class AttemptSummary(BaseModel):
    score: Optional[float] = None
    passed: bool = False
    answers: List[Any] = []
    created_at: Optional[datetime] = None


class UserResponseSummary(BaseModel):
    user_id: int
    user_name: str
    module_id: int
    module_title: str
    attempts: List[AttemptSummary] = []


class CourseResponsesResponse(BaseModel):
    course_id: int
    responses: List[UserResponseSummary] = []


# ── Dashboard ─────────────────────────────────────────────────────────
class AdminDashboard(BaseModel):
    total_users: int
    total_courses: int
    total_enrollments: int
    completed_enrollments: int
    active_enrollments: int
    total_certificates: int


# ── User Videos (Manual Course Creation) ──────────────────────────────


class VideoUploadResponse(BaseModel):
    video_id: str
    storage_url: str
    status: str
    duration: Optional[int] = None
    file_size: Optional[int] = None
    format: str = "webm"

    model_config = {"from_attributes": True}


class UserVideoOut(BaseModel):
    id: str
    module_id: Optional[int] = None
    user_id: int
    storage_url: str
    duration: Optional[int] = None
    file_size: Optional[int] = None
    format: str
    status: str
    created_at: datetime

    model_config = {"from_attributes": True}
