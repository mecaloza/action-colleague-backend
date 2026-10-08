"""All ORM models; importing this package registers every table on `Base.metadata`."""

from app.db.models.activity import Activity, ActivityAttempt
from app.db.models.certificate import Certificate
from app.db.models.course import Course, Module
from app.db.models.enrollment import Enrollment, ModuleProgress
from app.db.models.evaluation import Evaluation, EvaluationAttempt
from app.db.models.job import Job
from app.db.models.media import MediaAsset, UserVideo
from app.db.models.path import LearningPath, LearningPathCourse, LearningPathProfile, PathAssignment
from app.db.models.profile import JobProfile
from app.db.models.settings import AppSettings
from app.db.models.user import RefreshToken, User

__all__ = [
    "Activity",
    "ActivityAttempt",
    "AppSettings",
    "Certificate",
    "Course",
    "Enrollment",
    "Evaluation",
    "EvaluationAttempt",
    "Job",
    "JobProfile",
    "LearningPath",
    "LearningPathCourse",
    "LearningPathProfile",
    "MediaAsset",
    "Module",
    "ModuleProgress",
    "PathAssignment",
    "RefreshToken",
    "User",
    "UserVideo",
]
