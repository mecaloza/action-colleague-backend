from datetime import date, datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel


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


class RefreshRequest(BaseModel):
    refresh_token: str


class RegisterRequest(BaseModel):
    name: str
    email: str
    password: str
    role: str = "collaborator"
    position: str = ""
    department: str = ""
    reports_to: Optional[int] = None


# ── User ──────────────────────────────────────────────────────────────
class UserBase(BaseModel):
    name: str
    email: str
    role: str = "collaborator"
    position: str = ""
    department: str = ""
    hire_date: Optional[date] = None


class UserCreate(UserBase):
    password: str
    reports_to: Optional[int] = None
    permissions: List[str] = []


class UserUpdate(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    role: Optional[str] = None
    position: Optional[str] = None
    department: Optional[str] = None
    hire_date: Optional[date] = None
    reports_to: Optional[int] = None
    permissions: Optional[List[str]] = None
    is_active: Optional[bool] = None
    password: Optional[str] = None


class UserOut(UserBase):
    id: int
    reports_to: Optional[int] = None
    permissions: List[str] = []
    is_active: bool = True
    created_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class PermissionsUpdate(BaseModel):
    permissions: List[str]


class RoleUpdate(BaseModel):
    role: str


class UserProfile(BaseModel):
    id: int
    name: str
    email: str
    role: str
    position: str = ""
    department: str = ""
    hire_date: Optional[date] = None
    reports_to: Optional[int] = None
    permissions: List[str] = []
    is_active: bool = True
    created_at: Optional[datetime] = None
    manager: Optional[UserOut] = None
    direct_reports: List[UserOut] = []

    model_config = {"from_attributes": True}


class OrgChartNode(BaseModel):
    id: int
    name: str
    email: str
    position: str
    department: str
    role: str
    children: List["OrgChartNode"] = []

    model_config = {"from_attributes": True}


# ── Course ────────────────────────────────────────────────────────────
class CourseBase(BaseModel):
    title: str
    description: str = ""
    status: str = "draft"
    created_by: Optional[int] = None


class CourseCreate(CourseBase):
    pass


class CourseUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None


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


class EvaluationCreate(EvaluationBase):
    pass


class EvaluationUpdate(BaseModel):
    questions: Optional[List[Any]] = None


class EvaluationOut(BaseModel):
    id: int
    module_id: int
    questions: List[Any] = []
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
    next_module_unlocked: bool


# ── Certificate ───────────────────────────────────────────────────────
class CertificateBase(BaseModel):
    enrollment_id: int
    pdf_url: str = ""


class CertificateCreate(CertificateBase):
    pass


class CertificateOut(CertificateBase):
    id: int
    issued_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


# ── Document ──────────────────────────────────────────────────────────
class DocumentBase(BaseModel):
    user_id: int
    type: str
    pdf_url: str = ""
    template_data: Dict[str, Any] = {}


class DocumentCreate(BaseModel):
    user_id: int
    type: str = "labor_letter"
    template_data: Dict[str, Any] = {}


class DocumentOut(BaseModel):
    id: int
    user_id: int
    type: str
    pdf_url: str
    template_data: Dict[str, Any] = {}
    generated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


# ── AI Generate ───────────────────────────────────────────────────────
class CourseGenerateRequest(BaseModel):
    topic: str = ""
    num_modules: int = 3


class CourseGenerateResponse(BaseModel):
    message: str
    course_id: int
    modules_created: int


# ── Dashboard ─────────────────────────────────────────────────────────
class AdminDashboard(BaseModel):
    total_users: int
    total_courses: int
    total_enrollments: int
    completed_enrollments: int
    active_enrollments: int
    total_certificates: int


class CollaboratorDashboard(BaseModel):
    user_id: int
    enrollments: List[EnrollmentOut] = []
    completed_courses: int
    in_progress_courses: int
    certificates: int


# ── Micro-Series (Sora Video Generation) ─────────────────────────────


class SceneBase(BaseModel):
    order: int = 0
    sora_prompt: str = ""
    narration_text: str = ""
    duration_seconds: int = 8


class SceneCreate(SceneBase):
    episode_id: int


class SceneResponse(SceneBase):
    id: int
    episode_id: int
    video_url: str = ""
    audio_url: str = ""
    sora_video_id: str = ""
    status: str = "draft"
    created_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class EpisodeBase(BaseModel):
    title: str
    synopsis: str = ""
    order: int = 0


class EpisodeCreate(EpisodeBase):
    series_id: int


class EpisodeResponse(EpisodeBase):
    id: int
    series_id: int
    final_video_url: str = ""
    duration_seconds: int = 0
    status: str = "draft"
    scenes: List[SceneResponse] = []
    created_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class SeriesBase(BaseModel):
    title: str
    description: str = ""
    category: str = "custom"


class SeriesCreate(SeriesBase):
    created_by: Optional[int] = None


class SeriesResponse(SeriesBase):
    id: int
    thumbnail_url: str = ""
    status: str = "draft"
    created_by: Optional[int] = None
    episodes: List[EpisodeResponse] = []
    created_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class SeriesListItem(SeriesBase):
    id: int
    thumbnail_url: str = ""
    status: str = "draft"
    created_by: Optional[int] = None
    created_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class SeriesGenerateRequest(BaseModel):
    title: str
    description: str = ""
    category: str = "custom"
    case_description: str = ""
    num_episodes: int = 3
