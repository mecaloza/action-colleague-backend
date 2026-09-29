"""Admin course management: library, detail, publishing, modules and evaluations."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_admin
from app.api.lookups import course_or_404, module_or_404
from app.db.models import Course, Evaluation, Module, User, UserVideo
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
from app.services import legacy_data, progress, quiz
from app.services.course_views import ACTIVE_GENERATION, course_counts, course_detail, course_summary, module_admin
from app.services.learner_views import course_player_detail
from app.services.media import MediaResolver

router = APIRouter(tags=["courses"], dependencies=[Depends(require_admin)])


def _renumber(course: Course) -> None:
    """Close the gap a deleted module leaves: orders become 1..n again."""
    for index, module in enumerate(progress.ordered_modules(course), start=1):
        module.order = index


def _detach_legacy_videos(db: Session, module_ids: list[int]) -> None:
    # Recordings from the previous app may point at the modules; keep the recordings.
    if module_ids:
        db.query(UserVideo).filter(UserVideo.module_id.in_(module_ids)).update(
            {UserVideo.module_id: None}, synchronize_session=False
        )


# ── Courses ───────────────────────────────────────────────────────────


@router.get("/courses", response_model=list[CourseSummary])
def list_courses(
    status_filter: CourseStatus | None = Query(None, alias="status"),
    q: str | None = Query(None, max_length=200),
    db: Session = Depends(get_db),
):
    query = db.query(Course)
    if status_filter:
        query = query.filter(Course.status == status_filter)
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
        course.settings = {**(course.settings or {}), **payload.settings.model_dump()}
    db.commit()
    return course_detail(db, course)


@router.delete("/courses/{course_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_course(course_id: int, db: Session = Depends(get_db)):
    """Deletes the course with its modules, evaluations and participants' results."""
    course = course_or_404(db, course_id)
    _detach_legacy_videos(db, [module.id for module in course.modules])
    legacy_data.delete_certificates(db, [enrollment.id for enrollment in course.enrollments])
    db.delete(course)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


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
    _detach_legacy_videos(db, [module.id])
    db.delete(module)
    db.flush()
    db.refresh(course)
    _renumber(course)
    progress.refresh_course_enrollments(db, course)
    db.commit()
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
    db.flush()
    progress.refresh_course_enrollments(db, course)
    db.commit()
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
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"Hay una pregunta inválida: {exc}") from exc
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
        db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
