"""All ORM models; importing this package registers every table on `Base.metadata`."""

from app.db.models.course import Course, Module
from app.db.models.enrollment import Enrollment, ModuleProgress
from app.db.models.evaluation import Evaluation, EvaluationAttempt
from app.db.models.media import UserVideo
from app.db.models.user import RefreshToken, User

__all__ = [
    "Course",
    "Enrollment",
    "Evaluation",
    "EvaluationAttempt",
    "Module",
    "ModuleProgress",
    "RefreshToken",
    "User",
    "UserVideo",
]
