"""
ai.revise: an admin's observation about a module's script, quiz or activity -> a proposed change. The model writes
a patch (a script's or quiz's: only the scenes or questions, and their fields, it changes; an activity's: the whole
activity back); the server merges or validates it against the content as it was when the change was asked, and keeps
the result, its diff and (a script's) what producing it costs on the revision, for the admin to apply or discard.
Nothing in the module changes here.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import Activity, Job, Module, ModuleRevision
from app.services import activity_revisions, quiz, quiz_revisions, revisions, studio
from app.services.ai import designer
from app.services.ai.llm import LLM, get_llm
from app.services.progress import ordered_modules
from app.worker.jobs.ai import _llm_errors
from app.worker.runner import JobContext, JobError, handler, open_session

logger = logging.getLogger(__name__)


def _revision_failed(db: Session, job: Job, error: str) -> None:
    revision = db.get(ModuleRevision, (job.payload or {}).get("revision_id"))
    if revision is not None and revision.status == "proposing":
        revision.status, revision.error = "failed", error


@dataclass
class _Ask:
    """What the model is asked for a revision (read while the database is open, sent once it is closed)."""

    patch: Callable[[LLM, dict], BaseModel]  # the model's answer (it fills the usage it is given)
    propose: Callable[[BaseModel], revisions.Proposal]  # the proposal from it
    comparing: str  # the job's progress while the proposal is made


def _script_ask(db: Session, revision: ModuleRevision, module: Module) -> _Ask:
    course = module.course
    before, scope, observation = revision.before, revision.scope or {}, revision.observation
    outline, entry = studio.course_outline(course), studio.module_outline(module)
    number = [m.id for m in ordered_modules(course)].index(module.id) + 1
    language, tone = course.language or "es", (course.settings or {}).get("tone", "")
    materials = studio.materials_text(db, course.id)
    questions = [(question.id, question.prompt) for question in quiz.evaluation_questions(module.evaluation)]
    production = revisions.Production.of(module)
    scene_ids = scope.get("scene_ids") or None

    def patch(llm: LLM, usage: dict) -> designer.StoryboardPatchDraft:
        return designer.generate_storyboard_patch(
            llm, outline, entry, number, language=language, tone=tone, materials=materials,
            scenes=before["scenes"], reading=before["reading"], questions=questions, scene_ids=scene_ids,
            observation=observation, usage=usage,
        )

    def propose(answer: designer.StoryboardPatchDraft) -> revisions.Proposal:
        return revisions.propose(
            before, answer, scene_ids=scene_ids, production=production,
            question_ids=[question_id for question_id, _ in questions], max_clips=get_settings().max_clips_per_module,
        )

    return _Ask(patch, propose, "Comparando con el guion")


def _quiz_ask(db: Session, revision: ModuleRevision, module: Module) -> _Ask:
    before, question_ids = revision.before, (revision.scope or {}).get("question_ids") or None
    course_title, module_title = module.course.title, module.title
    language, observation = module.course.language or "es", revision.observation
    content = studio.module_content_for_quiz(db, module)

    def patch(llm: LLM, usage: dict) -> designer.QuizPatchDraft:
        return designer.generate_quiz_patch(
            llm, language=language, course_title=course_title, module_title=module_title, content=content,
            questions=before["questions"], question_ids=question_ids, observation=observation, usage=usage,
        )

    return _Ask(patch, lambda answer: quiz_revisions.propose(before, answer, question_ids=question_ids),
                "Comparando con la evaluación")


def _activity_ask(db: Session, revision: ModuleRevision, module: Module) -> _Ask | None:
    activity = db.get(Activity, revision.activity_id) if revision.activity_id else None
    if activity is None:
        return None
    before, kind, observation = revision.before, activity.type, revision.observation
    course_title, module_title = module.course.title, module.title
    language = module.course.language or "es"
    script = studio.module_content_for_quiz(db, module)  # what the learner studied before playing it
    materials = studio.materials_text(db, module.course_id)

    def patch(llm: LLM, usage: dict) -> designer.ActivityPatchDraft:
        return designer.generate_activity_patch(
            llm, language=language, course_title=course_title, module_title=module_title, script=script,
            materials=materials, activity={"type": kind, **before}, observation=observation, usage=usage,
        )

    return _Ask(patch, lambda answer: activity_revisions.propose(kind, before, answer), "Comparando con la actividad")


ASKS = {"script": _script_ask, "quiz": _quiz_ask, "activity": _activity_ask}


@handler("ai.revise", on_failure=_revision_failed)
def revise(ctx: JobContext) -> dict:
    revision_id = ctx.payload["revision_id"]
    with open_session() as db:
        revision = db.get(ModuleRevision, revision_id)
        if revision is None or revision.status != "proposing":  # discarded, or another observation replaced it
            return {"skipped": "revision no longer waiting"}
        module = db.get(Module, revision.module_id)
        if module is None:
            return {"skipped": "module deleted"}
        ask = ASKS[revision.target](db, revision, module)
        if ask is None:  # its activity was deleted meanwhile
            revision.status, revision.error = "failed", "La actividad ya no existe."
            db.commit()
            return {"skipped": "activity deleted"}
        db.commit()

    ctx.progress(10, "Preparando el cambio")
    usage: dict = {}
    with _llm_errors():
        answer = ask.patch(get_llm(), usage)
    ctx.progress(90, ask.comparing)
    try:
        proposal = ask.propose(answer)
    except revisions.ProposalError as exc:  # asking the same again would get the same answer
        raise JobError(str(exc), permanent=True) from exc

    with open_session() as db:
        revision = db.get(ModuleRevision, revision_id, with_for_update=True)
        if revision is None or revision.status != "proposing":  # discarded while the model wrote
            return {"skipped": "revision no longer waiting"}
        revision.after, revision.changes = proposal.after, proposal.changes
        revision.summary, revision.unresolved = answer.summary.strip(), answer.unresolved.strip()
        revision.usage = usage or None
        revision.status = "proposed"
        target = revision.target
        db.commit()
    logger.info("revision_proposed", extra={"revision_id": revision_id, "module_id": ctx.module_id, "target": target,
                                            "ignored": len(proposal.changes["ignored"])})
    return {"revision_id": revision_id, "target": target}
