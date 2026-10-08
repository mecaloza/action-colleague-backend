"""
ai.revise: an admin's observation about a module's script -> a proposed change. The model writes a patch (only the
scenes and fields it changes); the server merges it into the script as it was when the change was asked and keeps
the result, its diff and what producing it costs on the revision, for the admin to apply or discard. Nothing in the
module changes here.
"""

import logging

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import Job, Module, ModuleRevision
from app.services import quiz, revisions, studio
from app.services.ai import designer
from app.services.ai.llm import get_llm
from app.services.progress import ordered_modules
from app.worker.jobs.ai import _llm_errors
from app.worker.runner import JobContext, JobError, handler, open_session

logger = logging.getLogger(__name__)


def _revision_failed(db: Session, job: Job, error: str) -> None:
    revision = db.get(ModuleRevision, (job.payload or {}).get("revision_id"))
    if revision is not None and revision.status == "proposing":
        revision.status, revision.error = "failed", error


@handler("ai.revise", on_failure=_revision_failed)
def revise_script(ctx: JobContext) -> dict:
    revision_id = ctx.payload["revision_id"]
    with open_session() as db:
        revision = db.get(ModuleRevision, revision_id)
        if revision is None or revision.status != "proposing":  # discarded, or another observation replaced it
            return {"skipped": "revision no longer waiting"}
        module = db.get(Module, revision.module_id)
        if module is None:
            return {"skipped": "module deleted"}
        course = module.course
        before, scope, observation = revision.before, revision.scope or {}, revision.observation
        outline, entry = studio.course_outline(course), studio.module_outline(module)
        number = [m.id for m in ordered_modules(course)].index(module.id) + 1
        language, tone = course.language or "es", (course.settings or {}).get("tone", "")
        materials = studio.materials_text(db, course.id)
        questions = [(question.id, question.prompt) for question in quiz.evaluation_questions(module.evaluation)]
        production = revisions.Production.of(module)
        db.commit()

    ctx.progress(10, "Preparando el cambio")
    usage: dict = {}
    with _llm_errors():
        patch = designer.generate_storyboard_patch(
            get_llm(), outline, entry, number, language=language, tone=tone, materials=materials,
            scenes=before["scenes"], reading=before["reading"], questions=questions,
            scene_ids=scope.get("scene_ids") or None, observation=observation, usage=usage,
        )
    ctx.progress(90, "Comparando con el guion")
    try:
        proposal = revisions.propose(
            before, patch, scene_ids=scope.get("scene_ids") or None, production=production,
            question_ids=[question_id for question_id, _ in questions],
            max_clips=get_settings().max_clips_per_module,
        )
    except revisions.ProposalError as exc:  # asking the same again would get the same answer
        raise JobError(str(exc), permanent=True) from exc

    with open_session() as db:
        revision = db.get(ModuleRevision, revision_id, with_for_update=True)
        if revision is None or revision.status != "proposing":  # discarded while the model wrote
            return {"skipped": "revision no longer waiting"}
        revision.after, revision.changes = proposal.after, proposal.changes
        revision.summary, revision.unresolved = patch.summary.strip(), patch.unresolved.strip()
        revision.usage = usage or None
        revision.status = "proposed"
        db.commit()
    logger.info("revision_proposed", extra={
        "revision_id": revision_id, "module_id": ctx.module_id, "scenes": len(proposal.changes["scenes"]),
        "ignored": len(proposal.changes["ignored"]),
    })
    return {"revision_id": revision_id, "scenes": len(proposal.changes["scenes"])}
