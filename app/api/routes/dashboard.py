"""Admin dashboard: platform-wide numbers and recent learner activity."""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.api.deps import get_db, require_admin
from app.db.models import Course, Enrollment, EvaluationAttempt, Module, ModuleProgress, User
from app.schemas.dashboard import ActivityItem, Dashboard, TopCourse
from app.services.course_views import ACTIVE_GENERATION
from app.services.metrics import percentage
from app.services.progress import completed_enrollments

router = APIRouter(prefix="/dashboard", tags=["dashboard"], dependencies=[Depends(require_admin)])

RECENT_ACTIVITY_LIMIT = 12
RECENT_COMPLETIONS_LIMIT = 6
TOP_COURSES_LIMIT = 5


def _naive_utc(value: datetime | None) -> datetime:
    """Comparable timestamps: older columns are naive UTC, newer ones are timezone-aware."""
    if value is None:
        return datetime.min
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


def _active_learners_30d(db: Session) -> int:
    """People who took a quiz or completed a module in the last 30 days."""
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=30)  # naive UTC columns
    took_quiz = db.query(EvaluationAttempt.user_id).filter(EvaluationAttempt.created_at >= since).distinct()
    completed_module = (
        db.query(Enrollment.user_id)
        .join(ModuleProgress, ModuleProgress.enrollment_id == Enrollment.id)
        .filter(ModuleProgress.completed_at >= since)
        .distinct()
    )
    return len({user_id for (user_id,) in took_quiz} | {user_id for (user_id,) in completed_module})


def _quiz_activity(attempt: EvaluationAttempt) -> ActivityItem:
    return ActivityItem(
        kind="passed_quiz" if attempt.passed else "failed_quiz",
        user_name=attempt.user.name,
        course_id=attempt.module.course_id,
        course_title=attempt.module.course.title,
        module_title=attempt.module.title,
        score=attempt.score,
        at=attempt.created_at,
    )


def _completion_activity(enrollment: Enrollment) -> ActivityItem:
    return ActivityItem(
        kind="completed_course",
        user_name=enrollment.user.name,
        course_id=enrollment.course_id,
        course_title=enrollment.course.title,
        at=enrollment.completed_at,
    )


def _recent_activity(db: Session) -> list[ActivityItem]:
    """The latest quiz results and course completions together, newest first."""
    attempts = (
        db.query(EvaluationAttempt)
        .options(joinedload(EvaluationAttempt.user), joinedload(EvaluationAttempt.module).joinedload(Module.course))
        .order_by(EvaluationAttempt.created_at.desc(), EvaluationAttempt.id.desc())
        .limit(RECENT_ACTIVITY_LIMIT)
        .all()
    )
    completions = (
        db.query(Enrollment)
        .options(joinedload(Enrollment.user), joinedload(Enrollment.course))
        .filter(Enrollment.completed_at.isnot(None))
        .order_by(Enrollment.completed_at.desc())
        .limit(RECENT_COMPLETIONS_LIMIT)
        .all()
    )
    activity = [_quiz_activity(attempt) for attempt in attempts]
    activity += [_completion_activity(enrollment) for enrollment in completions]
    activity.sort(key=lambda item: _naive_utc(item.at), reverse=True)
    return activity[:RECENT_ACTIVITY_LIMIT]


def _top_courses(db: Session) -> list[TopCourse]:
    rows = (
        db.query(Course.id, Course.title, func.count(Enrollment.id), completed_enrollments())
        .join(Enrollment, Enrollment.course_id == Course.id)
        .group_by(Course.id, Course.title)
        .order_by(func.count(Enrollment.id).desc())
        .limit(TOP_COURSES_LIMIT)
        .all()
    )
    return [
        TopCourse(
            id=course_id,
            title=title,
            enrolled_count=enrolled,
            completion_rate=percentage(int(completed or 0), enrolled),
        )
        for course_id, title, enrolled, completed in rows
    ]


@router.get("", response_model=Dashboard)
def dashboard(db: Session = Depends(get_db)):
    courses_by_status = dict(db.query(Course.status, func.count(Course.id)).group_by(Course.status).all())
    generating = (
        db.query(func.count(func.distinct(Module.course_id)))
        .filter(Module.generation_status.in_(ACTIVE_GENERATION))
        .scalar()
    )
    enrollments_by_status = dict(
        db.query(Enrollment.status, func.count(Enrollment.id)).group_by(Enrollment.status).all()
    )
    total_enrollments = sum(enrollments_by_status.values())
    completed = enrollments_by_status.get("completed", 0)
    average_score = db.query(func.avg(EvaluationAttempt.score)).scalar()
    return Dashboard(
        courses={
            "total": sum(courses_by_status.values()),
            "published": courses_by_status.get("published", 0),
            "draft": courses_by_status.get("draft", 0),
            "generating": generating or 0,
        },
        learners={
            "total": db.query(func.count(User.id)).filter(User.role == "collaborator", User.is_active.is_(True)).scalar(),
            "active_30d": _active_learners_30d(db),
        },
        enrollments={
            "total": total_enrollments,
            "completed": completed,
            "in_progress": enrollments_by_status.get("in_progress", 0),
        },
        completion_rate=percentage(completed, total_enrollments),
        average_score=round(float(average_score), 1) if average_score is not None else None,
        recent_activity=_recent_activity(db),
        top_courses=_top_courses(db),
    )
