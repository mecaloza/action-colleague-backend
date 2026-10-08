from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.certificates import CertificateRef
from app.schemas.common import UtcDatetime, required_text
from app.schemas.profiles import ProfileRef

PathStatus = Literal["draft", "published", "archived"]
AssignmentSource = Literal["profile", "manual"]
# A person's progress in a path, and in each of its courses.
JourneyStatus = Literal["not_started", "in_progress", "completed", "overdue"]
StepStatus = Literal["completed", "in_progress", "available", "locked"]
MAX_PATH_COURSES = 30


class PathSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # Each course opens once the previous one (published) is completed.
    sequential: bool = True
    # Days to finish the path from the day it is assigned (None: no deadline).
    due_days: int | None = Field(default=None, ge=1, le=365)
    certificate: bool = True


class CourseRef(BaseModel):
    course_id: int
    title: str


class PathRef(BaseModel):
    id: int
    title: str


class CoursePath(BaseModel):
    """Where a course sits in the learner's path: `step` of `steps` (its published courses)."""

    id: int
    title: str
    step: int
    steps: int


class PathCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=5000)

    _title = field_validator("title")(classmethod(lambda cls, v: required_text(v)))


class PathUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    status: PathStatus | None = None
    settings: PathSettings | None = None  # only the keys sent change
    profile_ids: list[int] | None = Field(default=None, max_length=200)

    _title = field_validator("title")(classmethod(lambda cls, v: required_text(v) if v is not None else v))


class PathCoursesPut(BaseModel):
    course_ids: list[int] = Field(max_length=MAX_PATH_COURSES)  # in order


class PathCourse(BaseModel):
    course_id: int
    title: str
    status: str
    module_count: int
    order: int
    cover_url: str | None = None


class PathRow(BaseModel):
    id: int
    title: str
    description: str
    status: str
    course_count: int
    profiles: list[ProfileRef]
    people_count: int
    completed_count: int
    completion_rate: float
    updated_at: UtcDatetime | None = None


class PathDetail(BaseModel):
    id: int
    title: str
    description: str
    status: str
    settings: PathSettings
    profiles: list[ProfileRef]
    courses: list[PathCourse]
    people_count: int
    completed_count: int
    published_at: UtcDatetime | None = None
    created_at: UtcDatetime | None = None
    updated_at: UtcDatetime | None = None


class PathPeopleAdd(BaseModel):
    user_ids: list[int] = Field(min_length=1, max_length=500)


class PathPersonUser(BaseModel):
    id: int
    name: str
    email: str
    position: str = ""
    department: str = ""
    profile: ProfileRef | None = None


class PersonStep(BaseModel):
    course_id: int
    status: StepStatus
    progress_pct: float


class PathPerson(BaseModel):
    user: PathPersonUser
    source: str  # how they got it; by hand ('manual') it stays whatever their profile
    # False while their profile gives them the path (whatever the source): removing it by hand answers 409.
    removable: bool
    assigned_at: UtcDatetime | None = None
    due_at: UtcDatetime | None = None
    completed_at: UtcDatetime | None = None
    status: JourneyStatus
    progress_pct: float
    current_course_id: int | None = None
    steps: list[PersonStep]


class LearnerPathStep(BaseModel):
    course_id: int
    title: str
    cover_url: str | None = None
    status: StepStatus
    progress_pct: float
    total_modules: int
    completed_modules: int
    locked_by: CourseRef | None = None  # the course to complete first


class LearnerPath(BaseModel):
    id: int
    title: str
    description: str
    status: JourneyStatus
    progress_pct: float
    assigned_at: UtcDatetime | None = None
    due_at: UtcDatetime | None = None
    completed_at: UtcDatetime | None = None
    current_course_id: int | None = None  # the course to go on with
    steps: list[LearnerPathStep]  # its published courses, in order
    certificate: CertificateRef | None = None  # earned by completing it (not revoked)
