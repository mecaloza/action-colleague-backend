"""A person's overview for admins ("ficha de la persona") and the actions taken from it."""

from typing import Literal

from pydantic import BaseModel

from app.schemas.certificates import AdminCertificate, CertificateRef
from app.schemas.common import UtcDatetime
from app.schemas.learn import QuizSummary
from app.schemas.paths import AssignmentSource, LearnerPath
from app.schemas.profiles import ProfileRef

RecentKind = Literal[
    "quiz_passed", "quiz_failed", "activity_finished", "module_completed", "course_completed", "certificate_issued",
    "path_completed",
]


class PersonUser(BaseModel):
    id: int
    name: str
    email: str
    role: str
    position: str
    department: str
    profile: ProfileRef | None = None
    is_active: bool
    created_at: UtcDatetime | None = None
    last_seen_at: UtcDatetime | None = None  # their last sign-in or session refresh


class PersonKpis(BaseModel):
    courses_assigned: int
    courses_completed: int
    average_score: float | None = None  # of their best quiz scores (null: no quiz taken)
    points: int  # ever earned
    certificates: int  # valid ones
    activities_played: int  # different activities played to the end


class PersonPath(LearnerPath):
    source: AssignmentSource  # by their profile, or by hand


class PersonActivity(BaseModel):
    activity_id: int
    title: str
    type: str
    best_score: int | None = None  # of the plays finished (null: none yet)
    max_score: int
    plays: int  # finished
    last_played_at: UtcDatetime | None = None


class PersonModule(BaseModel):
    module_id: int
    title: str
    order: int
    completed: bool
    completed_at: UtcDatetime | None = None
    quiz: QuizSummary | None = None  # null when the module has no quiz
    activities: list[PersonActivity]  # its published ones, in order


class PersonCourse(BaseModel):
    course_id: int
    title: str
    status: str  # of the enrollment: assigned | in_progress | completed
    course_status: str  # of the course: draft | published | archived (learners only see published ones)
    progress_pct: float
    enrolled_at: UtcDatetime | None = None
    completed_at: UtcDatetime | None = None
    locked: bool  # by one of their learning paths
    certificate: CertificateRef | None = None  # not revoked
    modules: list[PersonModule]


class RecentEvent(BaseModel):
    kind: RecentKind
    at: UtcDatetime
    title: str  # the module, activity, course, certificate or path
    detail: str = ""  # the course it was in, or the certificate's code
    score: float | None = None  # 0-100: a quiz's score, a play's percentage of its maximum, a certificate's average


class PersonOverview(BaseModel):
    user: PersonUser
    kpis: PersonKpis
    paths: list[PersonPath]  # their published ones, oldest first
    courses: list[PersonCourse]  # newest enrollment first, drafts included
    certificates: list[AdminCertificate]  # newest first, revoked included
    recent: list[RecentEvent]  # the latest 25, newest first


class QuizReset(BaseModel):
    attempts_used: int
    max_attempts: int
