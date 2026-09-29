"""All ORM models; importing this package registers every table on `Base.metadata`."""

from app.db.models.course import Course, Module
from app.db.models.enrollment import Certificate, Enrollment, ModuleProgress
from app.db.models.evaluation import Evaluation, EvaluationAttempt
from app.db.models.legacy import Communication, Document, Episode, Scene, Series
from app.db.models.media import UserVideo
from app.db.models.user import RefreshToken, User

__all__ = [
    "Certificate",
    "Communication",
    "Course",
    "Document",
    "Enrollment",
    "Episode",
    "Evaluation",
    "EvaluationAttempt",
    "Module",
    "ModuleProgress",
    "RefreshToken",
    "Scene",
    "Series",
    "User",
    "UserVideo",
]
