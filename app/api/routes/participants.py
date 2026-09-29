"""Admin: who takes a course, their progress, attempts and per-question analytics."""

from collections import Counter, defaultdict

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.api.deps import get_db, require_admin
from app.api.lookups import course_or_404
from app.db.models import Course, Enrollment, EvaluationAttempt, Module, ModuleProgress, User
from app.schemas.participants import (
    AttemptDetail,
    AttemptQuestionResult,
    CourseAnalytics,
    ModuleAnalytics,
    ModuleAttempts,
    Participant,
    ParticipantsAdd,
    ParticipantUser,
    QuestionAnalytics,
)
from app.services import legacy_data, progress, quiz
from app.services.metrics import percentage

router = APIRouter(prefix="/courses/{course_id}", tags=["participants"], dependencies=[Depends(require_admin)])


def _participants(db: Session, course: Course) -> list[Participant]:
    enrollments = (
        db.query(Enrollment).options(joinedload(Enrollment.user)).filter(Enrollment.course_id == course.id).all()
    )
    if not enrollments:
        return []
    enrollment_ids = [e.id for e in enrollments]
    total_modules = len(course.modules)
    completed = dict(
        db.query(ModuleProgress.enrollment_id, func.count(ModuleProgress.id))
        .filter(ModuleProgress.enrollment_id.in_(enrollment_ids), ModuleProgress.completed.is_(True))
        .group_by(ModuleProgress.enrollment_id)
        .all()
    )
    attempt_stats = {
        enrollment_id: (average, last_at)
        for enrollment_id, average, last_at in db.query(
            EvaluationAttempt.enrollment_id, func.avg(EvaluationAttempt.score), func.max(EvaluationAttempt.created_at)
        )
        .filter(EvaluationAttempt.enrollment_id.in_(enrollment_ids))
        .group_by(EvaluationAttempt.enrollment_id)
    }
    last_completion = dict(
        db.query(ModuleProgress.enrollment_id, func.max(ModuleProgress.completed_at))
        .filter(ModuleProgress.enrollment_id.in_(enrollment_ids))
        .group_by(ModuleProgress.enrollment_id)
        .all()
    )
    result = []
    for enrollment in sorted(enrollments, key=lambda e: (e.user.name or "").lower()):
        user = enrollment.user
        average, last_attempt_at = attempt_stats.get(enrollment.id, (None, None))
        # Latest of the two, compared without tzinfo (naive and aware datetimes can't be compared).
        activity = [value for value in (last_attempt_at, last_completion.get(enrollment.id)) if value]
        result.append(
            Participant(
                enrollment_id=enrollment.id,
                user=ParticipantUser(
                    id=user.id,
                    name=user.name,
                    email=user.email,
                    position=user.position or "",
                    department=user.department or "",
                ),
                status=enrollment.status,
                progress_pct=enrollment.progress_pct or 0.0,
                completed_modules=completed.get(enrollment.id, 0),
                total_modules=total_modules,
                average_score=round(float(average), 1) if average is not None else None,
                enrolled_at=enrollment.enrolled_at,
                completed_at=enrollment.completed_at,
                last_activity_at=max(activity, key=lambda d: d.replace(tzinfo=None)) if activity else None,
            )
        )
    return result


@router.get("/participants", response_model=list[Participant])
def list_participants(course_id: int, db: Session = Depends(get_db)):
    return _participants(db, course_or_404(db, course_id))


@router.post("/participants", response_model=list[Participant])
def add_participants(
    course_id: int,
    payload: ParticipantsAdd,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    course = course_or_404(db, course_id)
    users = db.query(User).filter(User.id.in_(payload.user_ids), User.is_active.is_(True)).all()
    if len(users) != len(set(payload.user_ids)):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Alguno de los usuarios no existe o está inactivo")
    existing = {
        user_id
        for (user_id,) in db.query(Enrollment.user_id).filter(
            Enrollment.course_id == course.id, Enrollment.user_id.in_(payload.user_ids)
        )
    }
    for user in users:
        if user.id not in existing:
            db.add(
                Enrollment(
                    user_id=user.id, course_id=course.id, status="assigned", progress_pct=0.0, assigned_by=admin.id
                )
            )
    db.commit()
    db.refresh(course)
    return _participants(db, course)


@router.delete("/participants/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_participant(course_id: int, user_id: int, db: Session = Depends(get_db)):
    """Unassigns the person; their attempts and progress in this course are deleted."""
    course_or_404(db, course_id)
    enrollment = (
        db.query(Enrollment).filter(Enrollment.course_id == course_id, Enrollment.user_id == user_id).first()
    )
    if not enrollment:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "La persona no está inscrita en este curso")
    legacy_data.delete_certificates(db, [enrollment.id])
    db.delete(enrollment)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _module_attempts(module: Module, attempts: list[EvaluationAttempt]) -> ModuleAttempts:
    prompts = {q.id: q.prompt for q in quiz.evaluation_questions(module.evaluation)}
    return ModuleAttempts(
        module_id=module.id,
        module_title=module.title,
        attempts=[
            AttemptDetail(
                attempt_number=attempt.attempt_number or index,
                score=attempt.score,
                passed=bool(attempt.passed),
                created_at=attempt.created_at,
                results=[
                    AttemptQuestionResult(
                        question_id=r["question_id"],
                        prompt=prompts.get(r["question_id"], ""),
                        correct=bool(r.get("correct")),
                    )
                    for r in (attempt.results or [])
                ],
            )
            for index, attempt in enumerate(attempts, start=1)
        ],
    )


@router.get("/participants/{user_id}/attempts", response_model=list[ModuleAttempts])
def participant_attempts(course_id: int, user_id: int, db: Session = Depends(get_db)):
    course = course_or_404(db, course_id)
    attempts = (
        db.query(EvaluationAttempt)
        .join(Enrollment, Enrollment.id == EvaluationAttempt.enrollment_id)
        .filter(Enrollment.course_id == course.id, Enrollment.user_id == user_id)
        .order_by(EvaluationAttempt.created_at)
        .all()
    )
    by_module: dict[int, list[EvaluationAttempt]] = defaultdict(list)
    for attempt in attempts:
        by_module[attempt.module_id].append(attempt)
    return [
        _module_attempts(module, by_module[module.id])
        for module in progress.ordered_modules(course)
        if module.id in by_module
    ]


def _question_analytics(questions: list[quiz.Question], attempts: list[EvaluationAttempt]) -> list[QuestionAnalytics]:
    responses: Counter[str] = Counter()
    correct: Counter[str] = Counter()
    for attempt in attempts:
        for result in attempt.results or []:
            question_id = result.get("question_id")
            responses[question_id] += 1
            correct[question_id] += bool(result.get("correct"))
    return [
        QuestionAnalytics(
            question_id=q.id,
            prompt=q.prompt,
            type=q.type,
            responses=responses[q.id],
            correct=correct[q.id],
            accuracy=percentage(correct[q.id], responses[q.id]) if responses[q.id] else None,
        )
        for q in questions
    ]


def _module_analytics(
    module: Module, questions: list[quiz.Question], attempts: list[EvaluationAttempt]
) -> ModuleAnalytics:
    scores = [attempt.score for attempt in attempts if attempt.score is not None]
    return ModuleAnalytics(
        module_id=module.id,
        title=module.title,
        attempts=len(attempts),
        learners=len({attempt.user_id for attempt in attempts}),
        pass_rate=percentage(sum(1 for attempt in attempts if attempt.passed), len(attempts)),
        average_score=round(sum(scores) / len(scores), 1) if scores else None,
        questions=_question_analytics(questions, attempts),
    )


@router.get("/analytics", response_model=CourseAnalytics)
def course_analytics(course_id: int, db: Session = Depends(get_db)):
    course = course_or_404(db, course_id)
    modules = progress.ordered_modules(course)
    by_module: dict[int, list[EvaluationAttempt]] = defaultdict(list)
    if modules:
        for attempt in db.query(EvaluationAttempt).filter(EvaluationAttempt.module_id.in_([m.id for m in modules])):
            by_module[attempt.module_id].append(attempt)
    modules_out = []
    for module in modules:
        questions = quiz.evaluation_questions(module.evaluation)
        if questions:
            modules_out.append(_module_analytics(module, questions, by_module[module.id]))
    return CourseAnalytics(course_id=course.id, modules=modules_out)
