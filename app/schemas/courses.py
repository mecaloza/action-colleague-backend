from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.common import UtcDatetime

Language = Literal["es", "en", "pt"]
CourseStatus = Literal["draft", "published", "archived"]
CourseSource = Literal["ai", "manual"]
ModuleSource = Literal["ai", "upload", "recording", "text", "document"]


def _required_text(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("No puede quedar vacío")
    return value


class MediaRef(BaseModel):
    url: str
    mime_type: str = ""
    duration_seconds: float | None = None
    width: int | None = None
    height: int | None = None


class CourseSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    tone: str = Field(default="", max_length=200)
    audience: str = Field(default="", max_length=500)
    voice_id: str = Field(default="", max_length=100)
    voice_name: str = Field(default="", max_length=200)
    avatar_id: str = Field(default="", max_length=100)
    avatar_name: str = Field(default="", max_length=200)
    presenter: bool = True
    theme: Literal["dark", "light"] = "dark"


class CourseCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=5000)
    language: Language = "es"
    source: CourseSource = "manual"

    _title = field_validator("title")(classmethod(lambda cls, v: _required_text(v)))


class CourseUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=5000)
    language: Language | None = None
    settings: CourseSettings | None = None

    _title = field_validator("title")(classmethod(lambda cls, v: _required_text(v) if v is not None else v))


class CourseSummary(BaseModel):
    id: int
    title: str
    description: str
    language: str
    status: str
    source: str
    module_count: int
    enrolled_count: int
    completed_count: int
    completion_rate: float
    generating_count: int
    cover_url: str | None = None
    created_at: UtcDatetime | None = None
    updated_at: UtcDatetime | None = None
    published_at: UtcDatetime | None = None


class EvaluationSummary(BaseModel):
    question_count: int
    max_attempts: int
    passing_score: int


class ModuleAdmin(BaseModel):
    id: int
    course_id: int
    title: str
    description: str
    order: int
    source: str
    content_text: str
    generation_status: str
    generation_error: str | None = None
    video: MediaRef | None = None
    poster_url: str | None = None
    captions_url: str | None = None
    document: MediaRef | None = None
    duration_seconds: float | None = None
    scene_count: int = 0
    evaluation: EvaluationSummary | None = None
    updated_at: UtcDatetime | None = None


class CourseDetail(CourseSummary):
    settings: CourseSettings
    modules: list[ModuleAdmin]


class ModuleCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=3000)
    content_text: str = Field(default="", max_length=100_000)
    source: ModuleSource = "text"

    _title = field_validator("title")(classmethod(lambda cls, v: _required_text(v)))


class ModuleUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=3000)
    content_text: str | None = Field(default=None, max_length=100_000)

    _title = field_validator("title")(classmethod(lambda cls, v: _required_text(v) if v is not None else v))


class ModuleOrder(BaseModel):
    module_ids: list[int] = Field(min_length=1, max_length=200)


class EvaluationAdmin(BaseModel):
    id: int
    module_id: int
    questions: list[dict[str, Any]]
    max_attempts: int
    passing_score: int


class EvaluationPut(BaseModel):
    questions: list[dict[str, Any]] = Field(min_length=1, max_length=30)
    max_attempts: int = Field(default=3, ge=1, le=20)
    passing_score: int = Field(default=70, ge=1, le=100)
