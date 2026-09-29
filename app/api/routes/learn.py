"""Learner endpoints: my courses, the course player, quizzes and module completion."""

import json

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.core.config import get_settings
from app.db.models import Course, Enrollment, EvaluationAttempt, Module, User
from app.schemas.learn import (
    AttemptCreate,
    AttemptResult,
    CompletionResult,
    LearnerCourse,
    LearnerCourseDetail,
    LearnerQuiz,
    PositionUpdate,
    QuestionResult,
)
from app.services import progress, quiz
from app.services.learner_views import course_player_detail, learner_course
from app.services.media import MediaResolver

router = APIRouter(prefix="/learn", tags=["learn"])

# Same answer for "doesn't exist" and "not yours": ids of other courses can't be probed.
NO_ACCESS = "No tienes acceso a este contenido"


def _token_secret() -> str:
    return quiz.token_secret(get_settings().jwt_secret)


def _enrollment_for_course(db: Session, user: User, course_id: int) -> Enrollment:
    enrollment = (
        db.query(Enrollment)
        .join(Course, Course.id == Enrollment.course_id)
        .filter(Enrollment.user_id == user.id, Enrollment.course_id == course_id, Course.status == "published")
        .first()
    )
    if not enrollment:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NO_ACCESS)
    return enrollment


def _unlocked_module(
    db: Session, user: User, module_id: int
) -> tuple[Enrollment, progress.ModuleState, list[progress.ModuleState]]:
    """The learner's enrollment, the module's state and all the course's states; 403 while it is locked."""
    module = db.get(Module, module_id)
    if module is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NO_ACCESS)
    enrollment = _enrollment_for_course(db, user, module.course_id)
    states = progress.module_states(db, enrollment)
    state = next(s for s in states if s.module.id == module_id)
    if not state.unlocked:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Completa el módulo anterior para continuar")
    return enrollment, state, states


def _quiz_questions(state: progress.ModuleState) -> list[quiz.Question]:
    """The module's quiz questions; 404 when it has no evaluation (or none of its questions is usable)."""
    questions = quiz.evaluation_questions(state.evaluation)
    if not questions:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Este módulo no tiene evaluación")
    return questions


def _next_module_id(states: list[progress.ModuleState], module_id: int) -> int | None:
    ids = [state.module.id for state in states]
    index = ids.index(module_id)
    return ids[index + 1] if index + 1 < len(ids) else None


def _learner_results(graded_results: list[dict], reveal: bool) -> list[QuestionResult]:
    """
    Per-question feedback for the learner. Until `reveal`, only whether each answer was right is
    shown: no solution for anyone, and no explanation for wrong answers.
    """
    return [
        QuestionResult(
            question_id=r["question_id"],
            correct=r["correct"],
            explanation=r["explanation"] if reveal or r["correct"] else "",
            expected=r["expected"] if reveal else None,
        )
        for r in graded_results
    ]


@router.get("/courses", response_model=list[LearnerCourse])
def my_courses(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    enrollments = progress.user_enrollments(db, user.id, published_only=True)
    media = MediaResolver(db).prepare(extra_asset_ids=[e.course.cover_asset_id for e in enrollments])
    evaluations = progress.evaluations_by_module(db, [m for e in enrollments for m in e.course.modules])
    return [
        learner_course(e, progress.module_states(db, e, evaluations), media.url(e.course.cover_asset_id))
        for e in enrollments
    ]


@router.get("/courses/{course_id}", response_model=LearnerCourseDetail)
def course_player(course_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    enrollment = _enrollment_for_course(db, user, course_id)
    return course_player_detail(db, enrollment.course, progress.module_states(db, enrollment), enrollment)


@router.get("/modules/{module_id}/quiz", response_model=LearnerQuiz)
def get_quiz(module_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _, state, _ = _unlocked_module(db, user, module_id)
    questions = _quiz_questions(state)
    return LearnerQuiz(
        module_id=module_id,
        questions=quiz.learner_view(questions, _token_secret(), quiz.quiz_scope(state.evaluation.id)),
        max_attempts=quiz.max_attempts(state.evaluation),
        attempts_used=state.attempts_used,
        passing_score=quiz.passing_score(state.evaluation),
        passed=state.passed,
        version=quiz.questions_version(questions),
    )


@router.post("/modules/{module_id}/quiz/attempts", response_model=AttemptResult)
def submit_quiz(
    module_id: int,
    payload: AttemptCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    enrollment, state, states = _unlocked_module(db, user, module_id)
    evaluation = state.evaluation
    questions = _quiz_questions(state)
    if payload.version and payload.version != quiz.questions_version(questions):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "La evaluación cambió mientras la respondías. Vuelve a cargarla: este intento no cuenta.",
        )

    # Locked row: two submissions at once can't both use the last attempt.
    record = progress.get_or_create_progress(db, enrollment, state.module, lock=True)
    if record.passed:
        raise HTTPException(status.HTTP_409_CONFLICT, "Ya aprobaste esta evaluación")
    max_attempts = quiz.max_attempts(evaluation)
    used = record.attempts or 0
    if used >= max_attempts:
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"Agotaste los {max_attempts} intentos de esta evaluación")

    answers = [answer.model_dump(exclude_none=True) for answer in payload.answers]
    graded = quiz.grade(questions, answers, _token_secret(), quiz.quiz_scope(evaluation.id))
    passed = graded["score"] >= quiz.passing_score(evaluation)
    attempt_number = used + 1
    db.add(
        EvaluationAttempt(
            evaluation_id=evaluation.id,
            user_id=user.id,
            enrollment_id=enrollment.id,
            module_id=module_id,
            answers_json=json.dumps(answers),
            results=[{"question_id": r["question_id"], "correct": r["correct"]} for r in graded["results"]],
            score=graded["score"],
            passed=passed,
            attempt_number=attempt_number,
        )
    )
    record.attempts = attempt_number
    record.score = max(record.score or 0, graded["score"])
    if passed:
        record.passed = True
        progress.mark_completed(record)
    progress.refresh_enrollment(db, enrollment)
    db.commit()

    # Solutions only once passed: shown after the last attempt, they would pass the quiz as soon as the
    # admin grants more attempts or reassigns the course.
    reveal = passed
    return AttemptResult(
        score=graded["score"],
        passed=passed,
        correct=graded["correct"],
        total=graded["total"],
        attempts_used=attempt_number,
        attempts_remaining=max(max_attempts - attempt_number, 0),
        results=_learner_results(graded["results"], reveal),
        module_completed=passed,
        next_module_id=_next_module_id(states, module_id) if passed else None,
        course_completed=enrollment.status == "completed",
    )


@router.post("/modules/{module_id}/complete", response_model=CompletionResult)
def complete_module(module_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    enrollment, state, states = _unlocked_module(db, user, module_id)
    if quiz.evaluation_questions(state.evaluation):
        raise HTTPException(status.HTTP_409_CONFLICT, "Este módulo se completa aprobando su evaluación")
    record = progress.get_or_create_progress(db, enrollment, state.module)
    progress.mark_completed(record)
    progress.refresh_enrollment(db, enrollment)
    db.commit()
    return CompletionResult(
        module_completed=True,
        next_module_id=_next_module_id(states, module_id),
        course_completed=enrollment.status == "completed",
    )


@router.put("/modules/{module_id}/position", status_code=status.HTTP_204_NO_CONTENT)
def save_position(
    module_id: int, payload: PositionUpdate, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    enrollment, state, _ = _unlocked_module(db, user, module_id)
    record = progress.get_or_create_progress(db, enrollment, state.module)
    record.last_position_seconds = payload.seconds
    db.flush()  # progress row first, enrollment second: the lock order of every learner path
    # Conditional UPDATE: a stale read must never overwrite a status another request just set.
    db.execute(
        update(Enrollment)
        .where(Enrollment.id == enrollment.id, Enrollment.status == "assigned")
        .values(status="in_progress"),
        execution_options={"synchronize_session": False},
    )
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
