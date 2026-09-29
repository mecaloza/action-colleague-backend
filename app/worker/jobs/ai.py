"""
AI studio jobs.

- ai.outline:       brief + course materials -> proposed outline (stored in the course settings).
- ai.module_draft:  outline entry -> storyboard (slides + narration), reading summary and quiz.
- ai.quiz:          a module's content -> suggested questions (returned for review, not saved).
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy.orm import Session

from app.db.models import Course, Evaluation, Job, Module
from app.schemas.courses import MAX_AUDIENCE_CHARS
from app.schemas.studio import OutlineGenerate, QuizGenerate
from app.services import studio
from app.services.ai import designer
from app.services.ai.llm import LLMError, get_llm
from app.services.course_views import ACTIVE_GENERATION
from app.services.progress import ordered_modules
from app.worker.runner import JobContext, JobError, handler, open_session

logger = logging.getLogger(__name__)

MIN_QUIZ_CONTENT_CHARS = 80  # less than this is not enough to write questions from


@contextmanager
def _llm_errors() -> Iterator[None]:
    """LLM errors are worded for the admin: show them as the job's error."""
    try:
        yield
    except LLMError as exc:
        raise JobError(str(exc), permanent=exc.permanent) from exc


# ── Outline ───────────────────────────────────────────────────────────


@handler("ai.outline")
def generate_outline(ctx: JobContext) -> dict:
    request = OutlineGenerate.model_validate(ctx.payload)  # the payload is the request the route accepted
    with open_session() as db:
        course = db.get(Course, ctx.course_id)
        if course is None:
            return {"skipped": "course deleted"}
        language = course.language or "es"
        materials = studio.materials_text(db, course.id)
        previous = studio.stored_outline(course) if request.feedback.strip() else None  # what the feedback is about
    ctx.progress(15, "Leyendo tus materiales")
    with _llm_errors():
        outline = designer.generate_outline(
            get_llm(),
            brief=request.brief,
            language=language,
            audience=request.audience,
            tone=request.tone,
            target_modules=request.modules,
            minutes=request.minutes,
            materials=materials,
            feedback=request.feedback,
            previous=previous,
        )
    if not outline.modules:
        raise JobError("La IA no propuso módulos. Intenta de nuevo con más detalle en el brief.")
    ctx.progress(90, "Guardando la propuesta")
    with open_session() as db:
        course = db.get(Course, ctx.course_id)
        if course is None:
            return {"skipped": "course deleted"}
        course.settings = {
            **(course.settings or {}),
            "outline": outline.model_dump(),
            "brief": request.brief,
            # The model's audience has no length limit; the course settings do.
            "audience": (request.audience or outline.audience).strip()[:MAX_AUDIENCE_CHARS],
            "tone": request.tone,
            "minutes": request.minutes,
        }
        db.commit()
    return {"outline": outline.model_dump()}


# ── Module storyboard ─────────────────────────────────────────────────


def _module_failed(db: Session, job: Job, error: str) -> None:
    module = db.get(Module, job.module_id) if job.module_id else None
    if module:
        module.generation_status, module.generation_error = "failed", error


@handler("ai.module_draft", on_failure=_module_failed)
def draft_module(ctx: JobContext) -> dict:
    with open_session() as db:
        module = db.get(Module, ctx.module_id)
        if module is None:
            return {"skipped": "module deleted"}
        if module.source != "ai":  # it got its own video or document meanwhile: never overwrite that content
            if module.generation_status in ACTIVE_GENERATION:
                module.generation_status = "pending"
                db.commit()
            return {"skipped": "not an AI module"}
        course = module.course
        module.generation_status, module.generation_error = "generating", None
        outline = studio.course_outline(course)
        entry = studio.module_outline(module)
        number = [m.id for m in ordered_modules(course)].index(module.id) + 1
        language, tone = course.language or "es", (course.settings or {}).get("tone", "")
        materials = studio.materials_text(db, course.id)
        feedback = ctx.payload.get("feedback", "")
        previous_scenes = studio.scenes_of(module) if feedback.strip() else []  # what the feedback is about
        db.commit()

    ctx.progress(10, "Escribiendo el guion")
    with _llm_errors():
        draft = designer.generate_module(
            get_llm(), outline, entry, number,
            language=language, tone=tone, materials=materials, feedback=feedback, previous_scenes=previous_scenes,
        )
    scenes = designer.to_scenes(draft.scenes)
    if not scenes:
        raise JobError("La IA no produjo un guion utilizable. Intenta de nuevo.")
    questions = designer.to_questions(draft.quiz) if entry.include_quiz else []

    ctx.progress(90, "Guardando el guion")
    with open_session() as db:
        module = db.get(Module, ctx.module_id)
        if module is None:
            return {"skipped": "module deleted"}
        if module.source != "ai":  # a video or document was attached while the model wrote
            return {"skipped": "not an AI module"}
        render = (module.storyboard or {}).get("render")  # about the video still attached: it stays
        module.storyboard = {"outline": entry.model_dump(), "scenes": scenes, **({"render": render} if render else {})}
        module.content_text = designer.plain_markdown(draft.reading_summary)
        # Ready to produce the video; the storyboard can be reviewed and edited first.
        module.generation_status, module.generation_error = "pending", None
        if questions and module.evaluation is None:  # a quiz the admin already has is never overwritten
            db.add(Evaluation(module_id=module.id, spec=questions))
        db.commit()
    logger.info("module_drafted", extra={"module_id": ctx.module_id, "scenes": len(scenes), "questions": len(questions)})
    return {"scenes": len(scenes), "questions": len(questions)}


# ── Quiz suggestions ──────────────────────────────────────────────────


@handler("ai.quiz")
def suggest_quiz(ctx: JobContext) -> dict:
    with open_session() as db:
        module = db.get(Module, ctx.module_id)
        if module is None:
            return {"skipped": "module deleted"}
        title, language = module.title, module.course.language or "es"
        content = studio.module_content_for_quiz(db, module)
    if len(content.strip()) < MIN_QUIZ_CONTENT_CHARS:  # a retry would find the same content
        raise JobError(
            "El módulo aún no tiene suficiente contenido (texto, guion o documento) para crear preguntas.", permanent=True
        )
    ctx.progress(20, "Creando preguntas")
    count = QuizGenerate.model_validate(ctx.payload).count
    with _llm_errors():
        questions = designer.generate_quiz(get_llm(), title=title, content=content, count=count, language=language)
    if not questions:
        raise JobError("La IA no produjo preguntas válidas. Intenta de nuevo.")
    return {"questions": questions}
