"""Learner endpoints: my courses, paths and certificates, the course player, quizzes, interactive activities and
completion."""

import json

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.core.config import get_settings
from app.db.models import (
    Activity,
    ActivityAttempt,
    Certificate,
    Course,
    Enrollment,
    EvaluationAttempt,
    Module,
    ModuleProgress,
    User,
)
from app.schemas.certificates import LearnerCertificate
from app.schemas.learn import (
    ActivityFinishOut,
    ActivityMoveIn,
    ActivityMoveOut,
    ActivityPlay,
    ActivityStart,
    AttemptCreate,
    AttemptResult,
    CompletedPath,
    CompletionResult,
    LearnerCourse,
    LearnerCourseDetail,
    LearnerQuiz,
    PositionUpdate,
    QuestionResult,
)
from app.schemas.paths import LearnerPath
from app.services import activities as engine
from app.services import certificates, paths, progress, quiz
from app.services.learner_views import course_player_detail, learner_course
from app.services.media import MediaResolver
from app.services.path_views import learner_paths

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
    """The learner's enrollment, the module's state and all the course's states; 403 while it is locked (by the
    previous module, or by the course's learning path)."""
    module = db.get(Module, module_id)
    if module is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NO_ACCESS)
    enrollment = _enrollment_for_course(db, user, module.course_id)
    locked_by = paths.journey(db, user.id).locked_by.get(module.course_id)
    states = progress.module_states(db, enrollment, course_unlocked=locked_by is None)
    state = next(s for s in states if s.module.id == module_id)
    if not state.unlocked:
        if locked_by:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"Completa el curso «{locked_by.title}» para continuar.")
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


def _path_outcome(db: Session, enrollment: Enrollment) -> dict:
    """Once the course is completed: its certificate, and the next course its path opened or the path it completed
    (with that path's certificate)."""
    if enrollment.status != "completed":
        return {}
    db.flush()  # the completion is read back below
    journey = paths.journey(db, enrollment.user_id)
    step, path = journey.next_course(enrollment.course_id), journey.completed_path(enrollment.course_id)
    earned = certificates.learner_refs(db, enrollment.user_id, course_ids=[enrollment.course_id],
                                       path_ids=[path.id] if path else [])
    return {
        "certificate": earned.courses.get(enrollment.course_id),
        "next_course": step.ref() if step else None,
        "path_completed": CompletedPath(id=path.id, title=path.title, certificate=earned.paths.get(path.id))
        if path else None,
    }


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
    journey = paths.journey(db, user.id)
    earned = certificates.learner_refs(db, user.id, course_ids=[e.course_id for e in enrollments]).courses
    return [
        learner_course(
            e,
            progress.module_states(db, e, evaluations, course_unlocked=e.course_id not in journey.locked_by),
            media.url(e.course.cover_asset_id),
            journey,
            earned.get(e.course_id),
        )
        for e in enrollments
    ]


@router.get("/courses/{course_id}", response_model=LearnerCourseDetail)
def course_player(course_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """A course its path still keeps locked opens with every module locked, and says which course unlocks it."""
    enrollment = _enrollment_for_course(db, user, course_id)
    journey = paths.journey(db, user.id)
    states = progress.module_states(db, enrollment, course_unlocked=course_id not in journey.locked_by)
    certificate = certificates.learner_refs(db, user.id, course_ids=[course_id]).courses.get(course_id)
    return course_player_detail(db, enrollment.course, states, enrollment, journey, certificate)


@router.get("/paths", response_model=list[LearnerPath])
def my_paths(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """My published learning paths, oldest first: each course's state, the one to go on with and the deadline."""
    return learner_paths(db, user.id)


@router.get("/certificates", response_model=list[LearnerCertificate])
def my_certificates(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Every certificate I earned, newest first: valid, expired or revoked (its status says which)."""
    rows = (
        db.query(Certificate)
        .filter(Certificate.user_id == user.id)
        .order_by(Certificate.issued_at.desc(), Certificate.id.desc())
    )
    now = certificates.utcnow()
    return [certificates.learner_view(certificate, now) for certificate in rows]


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
        # Read after the lock: an activity finished at the same time counts (its request waited or committed).
        state.best_scores = progress.activity_best_scores(db, enrollment)
        progress.complete_if_done(state, record, has_quiz=True)  # its activities may still be pending
    progress.refresh_enrollment(db, enrollment)
    db.commit()

    # Solutions only once passed: shown after the last attempt, they would pass the quiz as soon as the
    # admin grants more attempts or reassigns the course.
    reveal = passed
    return AttemptResult(
        **_path_outcome(db, enrollment),
        score=graded["score"],
        passed=passed,
        correct=graded["correct"],
        total=graded["total"],
        attempts_used=attempt_number,
        attempts_remaining=max(max_attempts - attempt_number, 0),
        results=_learner_results(graded["results"], reveal),
        module_completed=bool(record.completed),
        next_module_id=_next_module_id(states, module_id) if record.completed else None,
        course_completed=enrollment.status == "completed",
    )


@router.post("/modules/{module_id}/complete", response_model=CompletionResult)
def complete_module(module_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    enrollment, state, states = _unlocked_module(db, user, module_id)
    # A passed quiz whose module waited for activities that are gone (deleted or unpublished) completes here.
    if quiz.evaluation_questions(state.evaluation) and not state.passed:
        raise HTTPException(status.HTTP_409_CONFLICT, "Este módulo se completa aprobando su evaluación")
    if state.pending_activities:
        raise HTTPException(status.HTTP_409_CONFLICT, "Completa las actividades del módulo para terminarlo")
    record = progress.get_or_create_progress(db, enrollment, state.module)
    progress.mark_completed(record)
    progress.refresh_enrollment(db, enrollment)
    db.commit()
    return CompletionResult(
        **_path_outcome(db, enrollment),
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


# ── Interactive activities ────────────────────────────────────────────


def _activity_secret() -> str:
    return engine.activity_secret(get_settings().jwt_secret)


def _ready_activity(db: Session, activity_id: int) -> Activity:
    """A published activity of a published course, in one read (a move checks it on every tap)."""
    activity = (
        db.query(Activity)
        .join(Module, Module.id == Activity.module_id)
        .join(Course, Course.id == Module.course_id)
        .filter(Activity.id == activity_id, Activity.status == "ready", Course.status == "published")
        .first()
    )
    if activity is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NO_ACCESS)
    return activity


def _playable_activity(db: Session, user: User, activity_id: int):
    activity = _ready_activity(db, activity_id)
    enrollment, state, states = _unlocked_module(db, user, activity.module_id)
    return activity, enrollment, state, states


def _own_attempt(db: Session, user: User, attempt_id: int) -> ActivityAttempt:
    # Locked: two taps at once can't both score the same zone or the same pair.
    attempt = db.query(ActivityAttempt).filter(ActivityAttempt.id == attempt_id).with_for_update().first()
    if attempt is None or attempt.user_id != user.id or attempt.enrollment_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NO_ACCESS)
    return attempt


def _count_finished_play(db: Session, enrollment: Enrollment, state: progress.ModuleState) -> ModuleProgress:
    """A play of this module just finished: the module completes if nothing else is left, and the course follows."""
    db.flush()  # the finished play first: counted below
    record = progress.get_or_create_progress(db, enrollment, state.module, lock=True)
    # Read after the lock: another activity (or the quiz) finished at the same time counts too.
    state.best_scores = progress.activity_best_scores(db, enrollment)
    progress.complete_if_done(state, record, has_quiz=bool(quiz.evaluation_questions(state.evaluation)))
    progress.refresh_enrollment(db, enrollment)
    return record


def _completion(db: Session, enrollment: Enrollment, states: list[progress.ModuleState], state: progress.ModuleState,
                module_completed: bool) -> dict:
    """What a finished play left done: its module (and then the next one to go to), the course and its path."""
    return {
        "module_completed": module_completed,
        "next_module_id": _next_module_id(states, state.module.id) if module_completed else None,
        "course_completed": enrollment.status == "completed",
        **_path_outcome(db, enrollment),
    }


def _finish_play(db: Session, activity: Activity, attempt: ActivityAttempt, enrollment: Enrollment,
                 state: progress.ModuleState, states: list[progress.ModuleState]) -> dict:
    """Close a play that is over and count it: on its last move, or through `finish`."""
    try:
        engine.finish(activity, attempt)
    except engine.GameError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    record = _count_finished_play(db, enrollment, state)
    return _completion(db, enrollment, states, state, bool(record.completed))


@router.post("/activities/{activity_id}/attempts", response_model=ActivityPlay)
def start_activity(
    activity_id: int, payload: ActivityStart, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """
    Resume the play left open while it can go on (a reload doesn't restart the game), or start a new one.

    An open play that can't go on is dealt with first. Untouched, it is deleted. With a move made, one whose clock ran
    out ended for real: it is closed with its score and counts, as `finish` does on a timeout. One left on an older
    version of the activity is never resumed nor counted (an edit must not turn a half-played game into a finished
    one): it stays as it is. A game whose time is up never comes back already lost, and the new one honors the untimed
    choice (it is for new plays only).
    """
    activity, enrollment, state, _ = _playable_activity(db, user, activity_id)
    # Locked in this order: the activity, shared (an admin deleting it waits for this start instead of deadlocking:
    # the delete's cascade needs the plays locked below, and the new play needs the activity), then the order of every
    # learner request: the plays, the module's progress, the enrollment. A move or a finish in flight, or a second tap
    # on "Empezar", waits for the open plays and then finds no stale play left to close.
    db.refresh(activity, with_for_update={"read": True, "key_share": True})
    open_plays = engine.open_plays(db, enrollment.id, [activity.id], lock=True)
    now = engine.utcnow()  # once the locks are held: a wait for them can't make an expired play look alive
    resumable = [play for play in open_plays if engine.resumable(activity, play, now)]
    closed_any = False
    for play in open_plays:
        if play in resumable:
            continue
        if not engine.played(play):
            db.delete(play)
        elif play.version == activity.version:  # its clock ran out (or it ended before last moves recorded plays)
            engine.close(play, now)
            closed_any = True
    if closed_any:
        _count_finished_play(db, enrollment, state)
    if not resumable:
        # A play is opened with the enrollment locked, last as in every learner request: a second tap on "Empezar"
        # (which found no play to resume either) waits here, then picks up the play this one opens.
        db.flush()  # the stale plays dealt with above, out of the read below
        db.query(Enrollment.id).filter(Enrollment.id == enrollment.id).with_for_update().one()
        resumable = engine.resumable_plays(db, enrollment.id, [activity])
    attempt = resumable[0] if resumable else None  # the newest
    resumed = attempt is not None
    try:
        if attempt is None:
            attempt = ActivityAttempt(activity_id=activity.id, user_id=user.id, enrollment_id=enrollment.id,
                                      version=activity.version, state=engine.start(activity, untimed=payload.untimed),
                                      max_score=engine.max_score(activity, untimed=payload.untimed), score=0)
            db.add(attempt)
            db.flush()
        view = engine.view(activity, attempt, _activity_secret())
    except engine.GameError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    db.commit()
    keys = ["intro", *engine.revealed_lines(view)]
    return ActivityPlay(attempt_id=attempt.id, activity_id=activity.id, view=view,
                        narration=engine.narration_urls(db, activity, keys),
                        image_url=engine.image_url(db, activity), resumed=resumed)


@router.post("/activity-attempts/{attempt_id}/moves", response_model=ActivityMoveOut)
def play_move(
    attempt_id: int, payload: ActivityMoveIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """
    One move. It is authorized by the play itself (a tap must be fast: a timed game is on): the learner's own play,
    started on an unlocked module, still open on the current version (the engine refuses it otherwise), of an activity
    and a course still published. The move that ends the game also finishes the play and counts it, as `finish` does:
    a learner who closes the game without "Ver resultado" keeps it.
    """
    attempt = _own_attempt(db, user, attempt_id)
    activity = _ready_activity(db, attempt.activity_id)
    try:
        outcome = engine.move(activity, attempt, payload.move, _activity_secret())
    except engine.GameError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    completion = {}
    if outcome.done:  # only now the learner's course is read: what finishing the play completes
        enrollment, state, states = _unlocked_module(db, user, activity.module_id)
        completion = _finish_play(db, activity, attempt, enrollment, state, states)
    db.commit()
    return ActivityMoveOut(result=outcome.result, done=outcome.done,
                           narration=engine.narration_urls(db, activity, engine.revealed_lines(outcome.result)),
                           **completion)


@router.post("/activity-attempts/{attempt_id}/finish", response_model=ActivityFinishOut)
def finish_activity(attempt_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Close a play that is over and count it. A play already finished (by its last move, or by this same call
    retried) answers with its result as it was recorded."""
    attempt = _own_attempt(db, user, attempt_id)
    activity, enrollment, state, states = _playable_activity(db, user, attempt.activity_id)
    if attempt.finished_at is not None:
        completion = _completion(db, enrollment, states, state, state.completed)
    else:
        completion = _finish_play(db, activity, attempt, enrollment, state, states)
        db.commit()
    return ActivityFinishOut(score=attempt.score, max_score=attempt.max_score, **completion)
