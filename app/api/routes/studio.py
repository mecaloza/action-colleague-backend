"""AI course studio: outline, per-module storyboards, quiz suggestions and slide previews."""

import threading

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_admin
from app.api.lookups import course_or_404, module_or_404
from app.core.config import get_settings
from app.db.models import Course, Job, Module, User
from app.schemas.courses import CourseDetail
from app.schemas.media import JobOut
from app.schemas.studio import (
    MAX_MODULES,
    MAX_TITLE_CHARS,
    Capabilities,
    CourseOutline,
    CourseOutlineIn,
    DraftRequest,
    OutlineGenerate,
    QuizGenerate,
    SlidePreview,
    Storyboard,
    StoryboardRegenerate,
)
from app.services import studio
from app.services.course_views import ACTIVE_GENERATION, course_detail
from app.services.media_views import job_out
from app.services.progress import ordered_modules, refresh_course_enrollments
from app.services.slides.render import render_png
from app.services.slides.spec import SlideContext
from app.services.storage import StorageError, get_storage
from app.worker import queue

router = APIRouter(tags=["studio"], dependencies=[Depends(require_admin)])

AI_JOB_ATTEMPTS = 2  # a second try covers a transient provider error
BUSY_WITH_OTHER_REQUEST = "Ya se está generando con otras indicaciones; espera a que termine para pedir cambios."


def _ai_available() -> bool:
    settings = get_settings()
    return settings.use_fake_providers or bool(settings.openai_api_key)


def _require_ai() -> None:
    if not _ai_available():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "La IA no está configurada en el servidor (OPENAI_API_KEY)")


def _same_request(job: Job, payload: dict) -> Job:
    """The job (maybe an active one returned by the dedupe key), or 409 if it was asked with other instructions."""
    if job.payload != payload:
        raise HTTPException(status.HTTP_409_CONFLICT, BUSY_WITH_OTHER_REQUEST)
    return job


def _storage_available() -> bool:
    try:
        get_storage()
    except StorageError:
        return False
    return True


@router.get("/studio/capabilities", response_model=Capabilities)
def capabilities():
    """What the studio can do on this server (the UI hides what isn't configured)."""
    return Capabilities(ai=_ai_available(), voice=False, avatar=False, storage=_storage_available())


# ── Outline ───────────────────────────────────────────────────────────


@router.post("/courses/{course_id}/outline/generate", response_model=JobOut, status_code=status.HTTP_202_ACCEPTED)
def generate_outline(
    course_id: int, payload: OutlineGenerate, db: Session = Depends(get_db), admin: User = Depends(require_admin)
):
    _require_ai()
    course = course_or_404(db, course_id)
    request = payload.model_dump()
    job = queue.enqueue(
        db, "ai.outline", request, course_id=course.id, created_by=admin.id,
        dedupe_key=f"outline:{course.id}", max_attempts=AI_JOB_ATTEMPTS,
    )
    return job_out(_same_request(job, request))


@router.get("/courses/{course_id}/outline", response_model=CourseOutline)
def get_outline(course_id: int, db: Session = Depends(get_db)):
    outline = studio.stored_outline(course_or_404(db, course_id))
    if outline is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Este curso aún no tiene una estructura propuesta")
    return outline


def _check_outline(outline: CourseOutlineIn) -> None:
    """422 unless the outline has 1 to MAX_MODULES modules and a title on the course and on each module."""
    if not 1 <= len(outline.modules) <= MAX_MODULES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"El curso debe tener entre 1 y {MAX_MODULES} módulos")
    if not outline.title.strip() or any(not module.title.strip() for module in outline.modules):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "El curso y cada módulo necesitan un título")


@router.put("/courses/{course_id}/outline", response_model=CourseDetail)
def apply_outline(course_id: int, outline: CourseOutlineIn, db: Session = Depends(get_db)):
    """Approve the outline: sets the course's title and description and creates its modules."""
    course = course_or_404(db, course_id)
    _check_outline(outline)
    # Locked: a double submit must not create the modules twice (both requests would see none).
    db.refresh(course, with_for_update=True)
    if any(not studio.is_untouched_ai_module(module) for module in course.modules):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "El curso ya tiene contenido (o se está generando); edita sus módulos uno por uno desde el editor",
        )
    for module in list(course.modules):
        db.delete(module)
    db.flush()
    for order, entry in enumerate(outline.modules, start=1):
        db.add(
            Module(
                course_id=course.id,
                title=entry.title.strip()[:MAX_TITLE_CHARS],
                description=entry.summary.strip(),
                order=order,
                source="ai",
                content_text="",
                generation_status="pending",
                storyboard={"outline": entry.model_dump(), "scenes": []},
            )
        )
    course.title = outline.title.strip()[:MAX_TITLE_CHARS]
    course.description = outline.description.strip()
    course.source = "ai"
    course.settings = {**(course.settings or {}), "outline": outline.model_dump(), "audience": outline.audience}
    db.flush()
    db.refresh(course)
    refresh_course_enrollments(db, course)
    db.commit()
    return course_detail(db, course)


# ── Storyboards ───────────────────────────────────────────────────────


def _require_ai_module(module: Module) -> None:
    """Only AI modules have a storyboard: drafting rewrites the module's reading text, never the admin's own content."""
    if module.source != "ai":
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"«{module.title}» tiene su propio contenido; el guion con IA es para módulos creados con IA"
        )


def _enqueue_draft(db: Session, module: Module, admin: User, feedback: str = "") -> Job:
    """Queue the module's storyboard job; the caller commits (several modules can be queued at once)."""
    job = queue.enqueue(
        db, "ai.module_draft", {"module_id": module.id, "feedback": feedback}, course_id=module.course_id,
        module_id=module.id, created_by=admin.id, dedupe_key=f"draft:{module.id}", max_attempts=AI_JOB_ATTEMPTS,
        commit=False,
    )
    if job.status == "queued":  # one already running keeps showing "generating"
        module.generation_status, module.generation_error = "queued", None
    return job


def _modules_to_draft(course: Course, module_ids: list[int] | None) -> list[Module]:
    """The requested modules, or (without a list) the AI modules that have no storyboard yet."""
    modules = ordered_modules(course)
    if module_ids is None:
        return [module for module in modules if module.source == "ai" and not studio.scenes_of(module)]
    wanted = set(module_ids)
    selected = [module for module in modules if module.id in wanted]
    if len(selected) != len(wanted):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Algún módulo no pertenece a este curso")
    for module in selected:
        _require_ai_module(module)
    return selected


@router.post("/courses/{course_id}/draft", response_model=list[JobOut], status_code=status.HTTP_202_ACCEPTED)
def draft_modules(
    course_id: int, payload: DraftRequest, db: Session = Depends(get_db), admin: User = Depends(require_admin)
):
    """Write the storyboard, reading and quiz of the course's modules (all AI modules by default)."""
    _require_ai()
    course = course_or_404(db, course_id)
    jobs = [_enqueue_draft(db, module, admin) for module in _modules_to_draft(course, payload.module_ids)]
    db.commit()
    return [job_out(job) for job in jobs]


@router.get("/modules/{module_id}/storyboard", response_model=Storyboard)
def get_storyboard(module_id: int, db: Session = Depends(get_db)):
    return Storyboard(scenes=studio.scenes_of(module_or_404(db, module_id)))


@router.put("/modules/{module_id}/storyboard", response_model=Storyboard)
def save_storyboard(module_id: int, payload: Storyboard, db: Session = Depends(get_db)):
    module = module_or_404(db, module_id)
    if module.generation_status in ACTIVE_GENERATION:  # the job would overwrite these edits when it finishes
        raise HTTPException(status.HTTP_409_CONFLICT, "El guion se está generando; edítalo cuando termine")
    if not payload.scenes:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "El guion necesita al menos una escena")
    ids = [scene.id for scene in payload.scenes]
    if len(set(ids)) != len(ids):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Hay escenas repetidas")
    module.storyboard = {**(module.storyboard or {}), "scenes": [scene.model_dump() for scene in payload.scenes]}
    db.commit()
    return Storyboard(scenes=studio.scenes_of(module))


@router.post("/modules/{module_id}/storyboard/regenerate", response_model=JobOut, status_code=status.HTTP_202_ACCEPTED)
def regenerate_storyboard(
    module_id: int, payload: StoryboardRegenerate, db: Session = Depends(get_db), admin: User = Depends(require_admin)
):
    _require_ai()
    module = module_or_404(db, module_id)
    _require_ai_module(module)
    job = _enqueue_draft(db, module, admin, feedback=payload.feedback)
    _same_request(job, {"module_id": module.id, "feedback": payload.feedback})
    db.commit()
    return job_out(job)


# ── Quiz suggestions ──────────────────────────────────────────────────


@router.post("/modules/{module_id}/evaluation/generate", response_model=JobOut, status_code=status.HTTP_202_ACCEPTED)
def suggest_quiz(
    module_id: int, payload: QuizGenerate, db: Session = Depends(get_db), admin: User = Depends(require_admin)
):
    """Suggest questions from the module's content; the result is reviewed in the editor before saving."""
    _require_ai()
    module = module_or_404(db, module_id)
    job = queue.enqueue(
        db, "ai.quiz", {"module_id": module.id, "count": payload.count}, course_id=module.course_id,
        module_id=module.id, created_by=admin.id, dedupe_key=f"quiz:{module.id}", max_attempts=AI_JOB_ATTEMPTS,
    )
    return job_out(job)


# ── Slides ────────────────────────────────────────────────────────────


# Rendering is CPU work in the API process: a few at a time, the rest wait their turn (an editor opening a
# long script asks for every scene at once) instead of piling up on the server's threads.
_PREVIEW_SLOTS = threading.BoundedSemaphore(2)
PREVIEW_WAIT_SECONDS = 15


@router.post("/slides/preview", response_class=Response)
def preview_slide(payload: SlidePreview):
    """The slide exactly as the video will show it (960x540 PNG)."""
    context = SlideContext(**payload.context.model_dump())
    if not _PREVIEW_SLOTS.acquire(timeout=PREVIEW_WAIT_SECONDS):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Hay muchas vistas previas en curso; intenta de nuevo.")
    try:
        png = render_png(payload.slide, context, scale=0.5)
    finally:
        _PREVIEW_SLOTS.release()
    return Response(png, media_type="image/png", headers={"Cache-Control": "no-store"})
