"""Admin course management: library, detail, publishing, modules and evaluations."""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import delete, or_, select, update
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_admin
from app.api.lookups import course_or_404, module_or_404
from app.db.models import (
    Course,
    Enrollment,
    Evaluation,
    EvaluationAttempt,
    Job,
    MediaAsset,
    Module,
    ModuleProgress,
    User,
    UserVideo,
)
from app.schemas.courses import (
    CourseCreate,
    CourseDetail,
    CourseStatus,
    CourseSummary,
    CourseUpdate,
    EvaluationAdmin,
    EvaluationPut,
    ModuleAdmin,
    ModuleCreate,
    ModuleOrder,
    ModuleUpdate,
)
from app.schemas.learn import LearnerCourseDetail
from app.schemas.media import PURPOSES_NEEDING_MODULE
from app.services import legacy_data, progress, quiz
from app.services.course_views import ACTIVE_GENERATION, course_counts, course_detail, course_summary, module_admin
from app.services.learner_views import course_player_detail
from app.services.media import MediaResolver, discard_assets
from app.services.storage import StorageError, get_storage
from app.worker.jobs import render as render_jobs
from app.worker.jobs.recording import LEGACY_VIDEO_JOB

logger = logging.getLogger(__name__)
router = APIRouter(tags=["courses"], dependencies=[Depends(require_admin)])


def _renumber(course: Course) -> None:
    """Close the gap a deleted module leaves: orders become 1..n again."""
    for index, module in enumerate(progress.ordered_modules(course), start=1):
        module.order = index


def _detach_legacy_videos(db: Session, module_ids) -> None:
    # Recordings from the previous app may point at the modules; keep the recordings.
    db.execute(
        update(UserVideo).where(UserVideo.module_id.in_(module_ids)).values(module_id=None),
        execution_options={"synchronize_session": False},
    )


def _module_media(module: Module) -> list[str | None]:
    """What the module shows, plus the camera and deck of its recording (they can be combined again)."""
    take = (module.storyboard or {}).get("recording") or {}
    return [
        module.video_asset_id, module.poster_asset_id, module.captions_asset_id, module.document_asset_id,
        take.get("recording_asset_id"), take.get("deck_asset_id"),
    ]


BOUND_PURPOSES = (*PURPOSES_NEEDING_MODULE, "course_cover")  # uploads only their module or course can use


def _drop_pending_jobs(db: Session, *conditions) -> tuple[list[str], list[str]]:
    """
    Delete the jobs that would only work on deleted rows (running ones finish on their own and clean
    up). Returns the uploads the queued ones were bringing to the module or course (nothing will use
    them now) and the files a render waiting for its presenter had made so far. A queued course
    material keeps its job: that file is still wanted.
    """
    dropped, uploads, files = [], [], []
    rows = db.query(Job.id, Job.type, Job.status, Job.payload, Job.state).filter(*conditions, Job.status != "running")
    for job_id, job_type, job_status, payload, state in rows:
        if job_type == "media.process" and job_status == "queued":
            if (payload or {}).get("purpose") not in BOUND_PURPOSES:
                continue
            uploads.append((payload or {}).get("asset_id"))
        elif job_type == LEGACY_VIDEO_JOB and job_status == "queued":  # the old video's copy, not processed yet
            uploads.append((payload or {}).get("asset_id"))
        elif job_type == "video.render" and job_status == "queued":  # finished ones already cleaned up
            files += render_jobs.leftover_paths(state)
        dropped.append(job_id)
    if dropped:  # a job claimed since the query keeps running: it cleans up after itself
        no_sync = {"synchronize_session": False}
        db.execute(delete(Job).where(Job.id.in_(dropped), Job.status != "running"), execution_options=no_sync)
    return uploads, files


def _delete_files_quietly(paths: list[str]) -> None:
    """After the commit, like `_discard_quietly`: a leftover file only costs storage."""
    if not paths:
        return
    try:
        get_storage().delete(paths)
    except StorageError as exc:
        logger.warning("render_files_cleanup_failed", extra={"files": len(paths), "error": str(exc)[:300]})


def _discard_quietly(db: Session, asset_ids: list[str | None]) -> None:
    """The deletion is committed: leftover media only costs storage, never an error for the admin."""
    try:
        discard_assets(db, asset_ids)
    except Exception:
        db.rollback()
        logger.exception("media_cleanup_after_delete_failed", extra={"assets": len([i for i in asset_ids if i])})


def _purge_modules(db: Session, module_ids) -> None:
    """Attempts, progress and evaluations of these modules: one statement per table, whatever the audience."""
    no_sync = {"synchronize_session": False}
    _detach_legacy_videos(db, module_ids)
    # Progress first: it waits for quiz submissions in flight (they lock their progress row), whose
    # attempts are then deleted too instead of breaking the evaluations' foreign key.
    db.execute(delete(ModuleProgress).where(ModuleProgress.module_id.in_(module_ids)), execution_options=no_sync)
    db.execute(delete(EvaluationAttempt).where(EvaluationAttempt.module_id.in_(module_ids)), execution_options=no_sync)
    db.execute(delete(Evaluation).where(Evaluation.module_id.in_(module_ids)), execution_options=no_sync)


# ── Courses ───────────────────────────────────────────────────────────


@router.get("/courses", response_model=list[CourseSummary])
def list_courses(
    status_filter: CourseStatus | None = Query(None, alias="status"),
    q: str | None = Query(None, max_length=200),
    db: Session = Depends(get_db),
):
    query = db.query(Course)
    # Archived courses only show up when asked for.
    query = query.filter(Course.status == status_filter) if status_filter else query.filter(Course.status != "archived")
    if q and q.strip():
        pattern = f"%{q.strip()}%"
        query = query.filter(or_(Course.title.ilike(pattern), Course.description.ilike(pattern)))
    courses = query.order_by(Course.updated_at.desc(), Course.id.desc()).all()
    counts = course_counts(db, [course.id for course in courses])
    media = MediaResolver(db).prepare(extra_asset_ids=[course.cover_asset_id for course in courses])
    return [course_summary(course, counts[course.id], media.url(course.cover_asset_id)) for course in courses]


@router.post("/courses", response_model=CourseDetail, status_code=status.HTTP_201_CREATED)
def create_course(payload: CourseCreate, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    course = Course(
        title=payload.title,
        description=payload.description,
        language=payload.language,
        source=payload.source,
        status="draft",
        settings={},
        created_by=admin.id,
    )
    db.add(course)
    db.commit()
    return course_detail(db, course)


@router.get("/courses/{course_id}", response_model=CourseDetail)
def get_course(course_id: int, db: Session = Depends(get_db)):
    return course_detail(db, course_or_404(db, course_id))


@router.patch("/courses/{course_id}", response_model=CourseDetail)
def update_course(course_id: int, payload: CourseUpdate, db: Session = Depends(get_db)):
    course = course_or_404(db, course_id)
    data = payload.model_dump(exclude_unset=True, exclude={"settings"})
    for field, value in data.items():
        setattr(course, field, value)
    if payload.settings is not None:
        course.settings = {**(course.settings or {}), **payload.settings.model_dump(exclude_unset=True)}
    db.commit()
    return course_detail(db, course)


@router.delete("/courses/{course_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_course(course_id: int, db: Session = Depends(get_db)):
    """Deletes the course with its modules, evaluations and participants' results."""
    course = course_or_404(db, course_id)
    no_sync = {"synchronize_session": False}
    media = [course.cover_asset_id, *(asset_id for module in course.modules for asset_id in _module_media(module))]
    media += [asset_id for (asset_id,) in db.execute(select(MediaAsset.id).where(MediaAsset.course_id == course_id))]
    enrollment_ids = [row[0] for row in db.execute(select(Enrollment.id).where(Enrollment.course_id == course_id))]
    legacy_data.delete_certificates(db, enrollment_ids)
    _purge_modules(db, select(Module.id).where(Module.course_id == course_id))
    db.execute(delete(EvaluationAttempt).where(EvaluationAttempt.enrollment_id.in_(enrollment_ids)), execution_options=no_sync)
    db.execute(delete(ModuleProgress).where(ModuleProgress.enrollment_id.in_(enrollment_ids)), execution_options=no_sync)
    db.execute(delete(Enrollment).where(Enrollment.course_id == course_id), execution_options=no_sync)
    db.execute(delete(Module).where(Module.course_id == course_id), execution_options=no_sync)
    db.execute(delete(Course).where(Course.id == course_id), execution_options=no_sync)
    uploads, files = _drop_pending_jobs(db, Job.course_id == course_id)
    db.commit()
    _discard_quietly(db, media + uploads)  # its videos, documents, cover and materials, in storage too
    _delete_files_quietly(files + _cached_visuals(course_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _cached_visuals(course_id: int) -> list[str]:
    """The stock videos, images and clips its videos were made with (made from its content: they go too)."""
    try:
        return get_storage().list_folder(f"courses/{course_id}/visuals")
    except StorageError as exc:
        logger.warning("visuals_listing_failed", extra={"course_id": course_id, "error": str(exc)[:300]})
        return []


def _publish_problems(db: Session, course: Course) -> list[dict]:
    """What keeps the course from being published, as [{module_id, message}] (empty when it is ready)."""
    detail = course_detail(db, course)
    problems = []
    if not detail.modules:
        problems.append({"module_id": None, "message": "Agrega al menos un módulo"})
    for module in detail.modules:
        if module.generation_status in ACTIVE_GENERATION:
            problems.append({"module_id": module.id, "message": f"«{module.title}» todavía se está generando"})
        elif not (module.video or module.document or module.content_text.strip()):
            problems.append({"module_id": module.id, "message": f"«{module.title}» no tiene contenido"})
    return problems


@router.post("/courses/{course_id}/publish", response_model=CourseDetail)
def publish_course(course_id: int, db: Session = Depends(get_db)):
    course = course_or_404(db, course_id)
    problems = _publish_problems(db, course)
    if problems:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"message": "El curso aún no se puede publicar", "problems": problems},
        )
    course.status = "published"
    course.published_at = course.published_at or datetime.now(timezone.utc)
    db.commit()
    return course_detail(db, course)


@router.post("/courses/{course_id}/unpublish", response_model=CourseDetail)
def unpublish_course(course_id: int, db: Session = Depends(get_db)):
    """Back to draft (also restores an archived course). Learners stop seeing it."""
    course = course_or_404(db, course_id)
    course.status = "draft"
    db.commit()
    return course_detail(db, course)


@router.post("/courses/{course_id}/archive", response_model=CourseDetail)
def archive_course(course_id: int, db: Session = Depends(get_db)):
    """Hide a course from learners and the default library view, keeping every result."""
    course = course_or_404(db, course_id)
    course.status = "archived"
    db.commit()
    return course_detail(db, course)


@router.get("/courses/{course_id}/preview", response_model=LearnerCourseDetail)
def preview_course(course_id: int, db: Session = Depends(get_db)):
    """The course as a learner sees it, with every module unlocked and no progress."""
    course = course_or_404(db, course_id)
    return course_player_detail(db, course, progress.preview_states(db, course), None)


# ── Modules ───────────────────────────────────────────────────────────


@router.post("/courses/{course_id}/modules", response_model=ModuleAdmin, status_code=status.HTTP_201_CREATED)
def create_module(course_id: int, payload: ModuleCreate, db: Session = Depends(get_db)):
    course = course_or_404(db, course_id)
    module = Module(
        course_id=course.id,
        title=payload.title,
        description=payload.description,
        content_text=payload.content_text,
        source=payload.source,
        order=max((m.order for m in course.modules), default=0) + 1,
        generation_status="pending",
    )
    db.add(module)
    db.flush()
    db.refresh(course)
    progress.refresh_course_enrollments(db, course)  # a new module means learners are not done yet
    db.commit()
    return module_admin(module, MediaResolver(db), None)


@router.patch("/modules/{module_id}", response_model=ModuleAdmin)
def update_module(module_id: int, payload: ModuleUpdate, db: Session = Depends(get_db)):
    module = module_or_404(db, module_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(module, field, value)
    db.commit()
    return module_admin(module, MediaResolver(db).prepare(modules=[module]), module.evaluation)


@router.delete("/modules/{module_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_module(module_id: int, db: Session = Depends(get_db)):
    module = module_or_404(db, module_id)
    course = module.course
    if course.status in ("published", "archived") and len(course.modules) == 1:
        # Learners would be left with an empty course (and their completions undone).
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Un curso publicado o archivado necesita al menos un módulo. Pásalo a borrador para quitar el último.",
        )
    _purge_modules(db, [module.id])  # progress first (see there), then the module row:
    db.refresh(module, with_for_update=True)  # a video published meanwhile is read, and discarded, too
    media = _module_media(module)
    uploads, files = _drop_pending_jobs(db, Job.module_id == module.id)
    media += uploads
    db.expire(module)  # its evaluation and progress were deleted in bulk
    db.delete(module)
    db.flush()
    db.refresh(course)
    _renumber(course)
    progress.refresh_course_enrollments(db, course)
    db.commit()
    _discard_quietly(db, media)
    _delete_files_quietly(files)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/courses/{course_id}/modules/order", response_model=list[ModuleAdmin])
def reorder_modules(course_id: int, payload: ModuleOrder, db: Session = Depends(get_db)):
    course = course_or_404(db, course_id)
    by_id = {module.id: module for module in course.modules}
    if sorted(payload.module_ids) != sorted(by_id):  # every module of the course, exactly once
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "La lista debe incluir cada módulo del curso una sola vez"
        )
    for index, module_id in enumerate(payload.module_ids, start=1):
        by_id[module_id].order = index
    db.commit()  # same modules: percentages and statuses don't change
    return course_detail(db, course).modules


# ── Evaluations ───────────────────────────────────────────────────────


def _evaluation_admin(evaluation: Evaluation) -> EvaluationAdmin:
    return EvaluationAdmin(
        id=evaluation.id,
        module_id=evaluation.module_id,
        questions=quiz.dump_questions(quiz.evaluation_questions(evaluation)),
        max_attempts=quiz.max_attempts(evaluation),
        passing_score=quiz.passing_score(evaluation),
    )


@router.get("/modules/{module_id}/evaluation", response_model=EvaluationAdmin)
def get_evaluation(module_id: int, db: Session = Depends(get_db)):
    module = module_or_404(db, module_id)
    if not module.evaluation:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Este módulo no tiene evaluación")
    return _evaluation_admin(module.evaluation)


@router.put("/modules/{module_id}/evaluation", response_model=EvaluationAdmin)
def put_evaluation(module_id: int, payload: EvaluationPut, db: Session = Depends(get_db)):
    module = module_or_404(db, module_id)
    try:
        questions = quiz.validate_questions(payload.questions)
    except quiz.InvalidQuestions as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    evaluation = module.evaluation or Evaluation(module_id=module.id)
    evaluation.spec = quiz.dump_questions(questions)
    evaluation.questions_json = "[]"  # the previous format is superseded by the spec
    evaluation.max_attempts = payload.max_attempts
    evaluation.passing_score = payload.passing_score
    db.add(evaluation)
    db.commit()
    return _evaluation_admin(evaluation)


@router.delete("/modules/{module_id}/evaluation", status_code=status.HTTP_204_NO_CONTENT)
def delete_evaluation(module_id: int, db: Session = Depends(get_db)):
    """Removes the quiz and its attempts; learners then complete the module by viewing it."""
    module = module_or_404(db, module_id)
    if module.evaluation:
        db.delete(module.evaluation)
        # Its attempts are gone: a future quiz must not start "exhausted" or "already passed".
        db.execute(
            update(ModuleProgress).where(ModuleProgress.module_id == module.id).values(attempts=0, passed=False, score=None),
            execution_options={"synchronize_session": False},
        )
        db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
