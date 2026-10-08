"""
Changes to a module's content by an observation (its script, its quiz or one of its activities): the AI proposes one
(a job), the admin reviews its diff and applies it (a script's, producing the video again if they want) or discards
it. Any applied change — proposed, saved by hand or rewritten by the AI — can be undone.
"""

import logging
from collections.abc import Callable
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session, defer

from app.api.deps import get_db, require_admin
from app.api.lookups import module_or_404
from app.api.routes.studio import (
    AI_JOB_ATTEMPTS,
    _check_presenters,
    _enqueue_render,
    _render_choices,
    _require_ai,
    _require_ai_module,
    _require_no_active,
    _require_voice,
)
from app.db.models import Activity, Job, Module, ModuleRevision, User
from app.schemas.revisions import (
    Revision,
    RevisionApplied,
    RevisionApply,
    RevisionCreate,
    RevisionLearners,
    RevisionProposal,
    RevisionScope,
    RevisionSummary,
    RevisionTarget,
)
from app.services import activity_revisions, quiz, quiz_revisions, revisions, studio, video_reviews
from app.services.activity_spec import SpecError
from app.services.course_views import ACTIVE_GENERATION
from app.services.media_views import job_out
from app.worker import queue

router = APIRouter(tags=["revisions"], dependencies=[Depends(require_admin)])
logger = logging.getLogger(__name__)

BUSY_WITH_OTHER_CHANGE = {
    "script": "Ya se está preparando otro cambio de este guion; espera a que termine para pedir otro.",
    "quiz": "Ya se está preparando otro cambio de esta evaluación; espera a que termine para pedir otro.",
    "activity": "Ya se está preparando otro cambio de esta actividad; espera a que termine para pedir otro.",
}
MAX_SUMMARY_CHARS = 300


def _revision_or_404(db: Session, revision_id: int) -> ModuleRevision:
    revision = db.get(ModuleRevision, revision_id)
    if revision is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Cambio no encontrado")
    return revision


def _summary_fields(revision: ModuleRevision, author: str | None) -> dict:
    return {
        "id": revision.id, "module_id": revision.module_id, "target": revision.target,
        "activity_id": revision.activity_id, "source": revision.source, "status": revision.status,
        "observation": revision.observation or "", "summary": revision.summary or "",
        "created_at": revision.created_at, "applied_at": revision.applied_at, "created_by_name": author,
    }


def _learners(db: Session, revision: ModuleRevision) -> RevisionLearners | None:
    """Who a pending change to a quiz or an activity would reach as soon as it is applied (a script's waits for its
    video)."""
    if revision.target == "script" or revision.status not in revisions.PENDING:
        return None
    module = db.get(Module, revision.module_id)
    if module is None:
        return None
    published = module.course.status == "published"
    if revision.target == "quiz":
        # An explanation fixed leaves every answer valid; while being proposed, it isn't known yet.
        changes_answers = revision.after is None or quiz_revisions.changes_answers(revision.before, revision.after)
        mid_quiz = quiz_revisions.learners_mid_quiz(db, module) if changes_answers else 0
        return RevisionLearners(published=published, mid_quiz=mid_quiz)
    activity = db.get(Activity, revision.activity_id) if revision.activity_id else None
    if activity is None:
        return None
    return RevisionLearners(published=published and activity.status == "ready",
                            mid_play=activity_revisions.learners_mid_play(db, activity))


def _changes_now(db: Session, revision: ModuleRevision) -> dict | None:
    """The revision's changes; while a script's is proposed, with what producing the video again takes told as of now:
    the module's video, what its videos keep and the course's voice and presenter can change after it was proposed
    (its video produced meanwhile keeps every scene's speech)."""
    changes = revision.changes
    if revision.target != "script" or revision.status != "proposed" or not changes or not revision.after:
        return changes
    module = db.get(Module, revision.module_id)
    if module is None:
        return changes
    return {**changes, "impact": revisions.impact(revision.before, revision.after, revisions.Production.of(module))}


def _revision_out(db: Session, revision: ModuleRevision) -> Revision:
    author = db.get(User, revision.created_by) if revision.created_by else None
    return Revision(
        **_summary_fields(revision, author.name if author else None),
        scope=RevisionScope.model_validate(revision.scope or {}), unresolved=revision.unresolved or "",
        changes=_changes_now(db, revision), error=revision.error, job_id=revision.job_id,
        learners=_learners(db, revision),
    )


def _require_editable(db: Session, module: Module, action: str) -> None:
    """As a manual save: a job would overwrite the change, or produce the video of the script before it."""
    if queue.active_with_key(db, f"render:{module.id}") is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"El video se está produciendo; {action} cuando termine")
    if module.generation_status in ACTIVE_GENERATION:
        raise HTTPException(status.HTTP_409_CONFLICT, f"El guion se está generando; {action} cuando termine")


def _require_kept(db: Session, activity: Activity, action: str) -> None:
    """An AI draft nobody touched is replaced when the module's activities are drafted again: not while that runs."""
    drafting = queue.active_with_key(db, f"activities:{activity.module_id}") is not None
    if activity.source == "ai" and activity.status == "draft" and drafting:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"Se están creando las actividades del módulo; {action} cuando termine"
        )


def _numbered(item_ids: list[str], lists: list[list[dict]], noun: str) -> str:
    """Scenes or questions by their number, as the admin knows them ("la escena 2", "las preguntas 2 y 5"), looked
    up in the first list that has each."""
    numbers = []
    for item_id in item_ids:
        for items in lists:
            ids = [item.get("id") for item in items]
            if item_id in ids:
                numbers.append(ids.index(item_id) + 1)
                break
    numbers = sorted(set(numbers))
    if not numbers:
        return ""
    if len(numbers) == 1:
        return f"la {noun} {numbers[0]}"
    return f"las {noun}s {', '.join(map(str, numbers[:-1]))} y {numbers[-1]}"


def _script_places(conflict: revisions.Conflict, *scripts: dict) -> str:
    """Where the script changed since, for the admin: "la escena 2", "las escenas 2 y 5 y la lectura"..."""
    places = [_numbered(conflict.ids, [script["scenes"] for script in scripts], "escena"),
              "la lectura" if conflict.reading else ""]
    return " y ".join(place for place in places if place) or "el guion"


def _quiz_places(conflict: revisions.Conflict, *quizzes: dict) -> str:
    return _numbered(conflict.ids, [each["questions"] for each in quizzes], "pregunta") or "la evaluación"


def _merged_or_409(
    merge: Callable[[], dict], *, code: str, message: str, places: Callable[[revisions.Conflict], str],
    detail: Callable[[revisions.Conflict], dict],
) -> dict:
    """A three-way merge (`merge`) with its refusals as 409s: where it conflicts goes into `message` ("{places}") and,
    by id, into the detail."""
    try:
        return merge()
    except revisions.Conflict as conflict:
        raise HTTPException(status.HTTP_409_CONFLICT, {
            "code": code, "message": message.format(places=places(conflict)), **detail(conflict),
        }) from conflict
    except revisions.RevisionError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


def _script_detail(conflict: revisions.Conflict) -> dict:
    return {"scene_ids": conflict.ids, "reading": conflict.reading}


def _quiz_detail(conflict: revisions.Conflict) -> dict:
    return {"question_ids": conflict.ids}


def _activity_detail(conflict: revisions.Conflict) -> dict:
    return {"paths": conflict.ids}


def _write_quiz(db: Session, module: Module, wanted: dict) -> None:
    try:
        questions = quiz.validate_questions(wanted["questions"])
    except quiz.InvalidQuestions as exc:  # each question was valid; together (merged) they are not
        raise HTTPException(status.HTTP_409_CONFLICT, f"La evaluación quedaría con un error: {exc}") from exc
    quiz_revisions.write(db, module, questions)


def _valid_activity(activity: Activity, wanted: dict) -> dict:
    try:
        return activity_revisions.validated(activity.type, wanted)
    except SpecError as exc:  # each version was valid; their parts together (merged) are not
        raise HTTPException(status.HTTP_409_CONFLICT, f"La actividad quedaría con un error: {exc}") from exc


def _held_activity(db: Session, activity_id: int | None) -> Activity | None:
    """The activity held as a save of it holds it (NO KEY UPDATE: plays starting meanwhile go on), and read as it is
    then: None when it is gone (deleted, or replaced by a new AI draft, while this waited for it). It is held before
    its changes, as everything that deletes it does."""
    if activity_id is None:
        return None
    return db.get(Activity, activity_id, with_for_update={"key_share": True}, populate_existing=True)


def _activity_of(db: Session, revision: ModuleRevision) -> Activity:
    activity = _held_activity(db, revision.activity_id)
    if activity is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "La actividad de este cambio ya no existe")
    return activity


def _applied_out(db: Session, revision: ModuleRevision, render_job: Job | None) -> RevisionApplied:
    return RevisionApplied(revision=_revision_out(db, revision), render_job=render_job and job_out(render_job))


# ── Proposals ─────────────────────────────────────────────────────────


def _check_scope(payload: RevisionCreate) -> None:
    """What is chosen belongs to the target: scenes to a script, questions to a quiz, an activity to itself."""
    if payload.target != "script" and payload.scene_ids:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Las escenas se eligen solo para cambiar el guion")
    if payload.target != "quiz" and payload.question_ids:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Las preguntas se eligen solo para cambiar la evaluación"
        )
    if payload.target != "activity" and payload.activity_id is not None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "La actividad se elige solo para cambiar una actividad"
        )


def _queue_proposal(
    db: Session, module: Module, admin: User, payload: RevisionCreate, content: dict, scope: dict, *, key: str,
    activity_id: int | None = None,
) -> RevisionProposal:
    """The proposal being prepared by the AI: a new one, or the same one when the same is asked again meanwhile."""
    active = queue.active_with_key(db, key)
    if active is not None:  # a double click gets the same proposal; another observation waits for it
        waiting = db.get(ModuleRevision, (active.payload or {}).get("revision_id"))
        if waiting is None or waiting.observation != payload.observation or waiting.scope != scope:
            raise HTTPException(status.HTTP_409_CONFLICT, BUSY_WITH_OTHER_CHANGE[payload.target])
        return RevisionProposal(revision=_revision_out(db, waiting), job=job_out(active))
    # "Pedir otro ajuste": the newest observation wins over the earlier proposals for the same (script, quiz, activity).
    revisions.supersede(db, module.id, target=payload.target, activity_id=activity_id)
    revision = ModuleRevision(
        module_id=module.id, target=payload.target, activity_id=activity_id,
        source="qa" if payload.qa_issue_id else "observation", status="proposing", observation=payload.observation,
        scope=scope, base_hash=revisions.content_hash(content), before=content, created_by=admin.id,
    )
    db.add(revision)
    db.flush()
    job = queue.enqueue(
        db, "ai.revise", {"revision_id": revision.id}, course_id=module.course_id, module_id=module.id,
        created_by=admin.id, dedupe_key=key, max_attempts=AI_JOB_ATTEMPTS, commit=False,
    )
    revision.job_id = job.id
    db.commit()
    return RevisionProposal(revision=_revision_out(db, revision), job=job_out(job))


def _chosen_ids(chosen: list[str] | None, items: list[dict], message: str) -> list[str]:
    """The scenes or questions the admin chose (without repeats), 422 `message` when one is no longer there."""
    chosen = list(dict.fromkeys(chosen or []))
    present = {item.get("id") for item in items}
    if any(item_id not in present for item_id in chosen):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, message)
    return chosen


def _propose_script(db: Session, module: Module, payload: RevisionCreate, admin: User) -> RevisionProposal:
    _require_ai_module(module, "pedir cambios al guion con IA")
    db.refresh(module, with_for_update=True)  # two asks at once: one waits and sees the other
    content = revisions.script_content(module)
    if not content["scenes"]:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"«{module.title}» no tiene guion todavía")
    _require_no_active(db, f"draft:{module.id}", "El guion se está escribiendo; pide el cambio cuando termine")
    scene_ids = _chosen_ids(
        payload.scene_ids, content["scenes"], "Alguna escena elegida ya no está en el guion; vuelve a cargarlo"
    )
    scope = {**revisions.empty_scope(), "scene_ids": scene_ids, "qa_issue_id": payload.qa_issue_id}
    return _queue_proposal(db, module, admin, payload, content, scope, key=f"revise:script:{module.id}")


def _propose_quiz(db: Session, module: Module, payload: RevisionCreate, admin: User) -> RevisionProposal:
    db.refresh(module, with_for_update={"key_share": True})  # as a save of the quiz: two asks at once, one waits
    content = quiz_revisions.content(module)
    if not content["questions"]:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"«{module.title}» no tiene evaluación todavía")
    question_ids = _chosen_ids(
        payload.question_ids, content["questions"],
        "Alguna pregunta elegida ya no está en la evaluación; vuelve a cargarla",
    )
    scope = {**revisions.empty_scope(), "question_ids": question_ids, "qa_issue_id": payload.qa_issue_id}
    return _queue_proposal(db, module, admin, payload, content, scope, key=f"revise:quiz:{module.id}")


def _propose_activity(db: Session, module: Module, payload: RevisionCreate, admin: User) -> RevisionProposal:
    if payload.activity_id is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Elige la actividad que quieres cambiar")
    activity = _held_activity(db, payload.activity_id)  # as a save of the activity: one waits for the other
    if activity is None or activity.module_id != module.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Actividad no encontrada")
    _require_kept(db, activity, "pide el cambio")
    scope = {**revisions.empty_scope(), "qa_issue_id": payload.qa_issue_id}
    return _queue_proposal(db, module, admin, payload, activity_revisions.content(activity), scope,
                           key=f"revise:activity:{activity.id}", activity_id=activity.id)


@router.post("/modules/{module_id}/revisions", response_model=RevisionProposal,
             status_code=status.HTTP_202_ACCEPTED)
def propose_revision(
    module_id: int, payload: RevisionCreate, db: Session = Depends(get_db), admin: User = Depends(require_admin)
):
    """Ask the AI to change the module's script, its quiz or one of its activities as the observation says (only the
    chosen scenes or questions, when given). Nothing changes until the proposal is applied."""
    _require_ai()
    module = module_or_404(db, module_id)
    _check_scope(payload)
    if payload.target == "quiz":
        return _propose_quiz(db, module, payload, admin)
    if payload.target == "activity":
        return _propose_activity(db, module, payload, admin)
    return _propose_script(db, module, payload, admin)


@router.get("/modules/{module_id}/revisions", response_model=list[RevisionSummary])
def list_revisions(
    module_id: int, target: RevisionTarget | None = None, activity_id: int | None = None,
    limit: int = Query(50, ge=1, le=200), db: Session = Depends(get_db),
):
    """The module's changes, newest first: proposals and every change applied to it (and undone)."""
    module_or_404(db, module_id)
    query = (
        db.query(ModuleRevision, User.name)
        .outerjoin(User, User.id == ModuleRevision.created_by)
        .options(*(defer(column) for column in (ModuleRevision.before, ModuleRevision.after,
                                                ModuleRevision.changes, ModuleRevision.usage)))
        .filter(ModuleRevision.module_id == module_id)
    )
    if target is not None:
        query = query.filter(ModuleRevision.target == target)
    if activity_id is not None:
        query = query.filter(ModuleRevision.activity_id == activity_id)
    rows = query.order_by(ModuleRevision.created_at.desc(), ModuleRevision.id.desc()).limit(limit).all()
    return [RevisionSummary(**_summary_fields(revision, author)) for revision, author in rows]


@router.get("/revisions/{revision_id}", response_model=Revision)
def get_revision(revision_id: int, db: Session = Depends(get_db)):
    return _revision_out(db, _revision_or_404(db, revision_id))


# ── Applying, discarding and undoing ──────────────────────────────────


def _appliable(revision: ModuleRevision) -> bool:
    """Whether the proposal is to be applied now: not when it already was (a double click gets what the first one
    did); 409 when it can't be."""
    if revision.status == "applied":
        return False
    if revision.status == "proposing":
        raise HTTPException(status.HTTP_409_CONFLICT, "La propuesta aún se está preparando; espera a que termine")
    if revision.status != "proposed":
        raise HTTPException(status.HTTP_409_CONFLICT, "Esta propuesta ya no está vigente; pide el cambio de nuevo")
    return True


def _mark_applied(revision: ModuleRevision, before: dict, after: dict, changes: dict, admin: User) -> None:
    """The proposal, as it was applied: over the content then (`before`), with what changed then."""
    revision.before, revision.after, revision.changes = before, after, changes
    revision.status, revision.applied_at, revision.applied_by = "applied", datetime.now(timezone.utc), admin.id


def _qa_issue(db: Session, revision: ModuleRevision) -> tuple[str | None, bool]:
    """The video review's finding a change is about, and whether making the change fixes it: a change asked for the
    finding fixes it, its undo undoes the fix, the undo of that undo fixes it again..."""
    fixes, seen = True, set()
    while revision is not None and revision.id not in seen:
        seen.add(revision.id)
        scope = revision.scope or {}
        if scope.get("qa_issue_id"):
            return scope["qa_issue_id"], fixes
        if not scope.get("reverted_id"):
            break
        revision, fixes = db.get(ModuleRevision, scope["reverted_id"]), not fixes
    return None, fixes


def _follow_qa(db: Session, revision: ModuleRevision, *, undone: bool = False) -> None:
    """The finding the change (applied now, or `undone` now) was made for: fixed while the change stands, open again
    once it is undone. The caller commits."""
    issue_id, fixes = _qa_issue(db, revision)
    if issue_id:
        fixed = not fixes if undone else fixes  # undoing a change undoes what it did for the finding
        video_reviews.mark_issue(db, revision.module_id, issue_id, video_reviews.FIXED if fixed else video_reviews.OPEN)


def _apply_script(db: Session, revision: ModuleRevision, module: Module, render: bool, admin: User) -> RevisionApplied:
    # Only a video the module has is produced again (its first one is made from the studio), and what it takes is
    # checked before changing anything: the change and its video go together.
    render = render and bool(module.video_asset_id or module.video_url)
    choices = None
    if render:
        _require_voice()
        choices = _render_choices(module.course)
        _check_presenters(choices)
    db.refresh(module, with_for_update=True)  # as a manual save, and before the revision (the job's own lock)
    db.refresh(revision, with_for_update=True)
    if not _appliable(revision):
        render_job = queue.active_with_key(db, f"render:{module.id}") if render else None
        return _applied_out(db, revision, render_job)
    _require_ai_module(module, "aplicar cambios al guion con IA")
    _require_editable(db, module, "aplica el cambio")
    current = revisions.script_content(module)
    result = _merged_or_409(
        lambda: revisions.three_way(current, revision.before, revision.after), code="revision_stale",
        message="El guion cambió en {places} desde la propuesta; pide el cambio de nuevo.",
        places=lambda conflict: _script_places(conflict, current, revision.after), detail=_script_detail,
    )
    revisions.write_script(module, result)
    proposed = revision.changes or {}
    reasons = {change["scene_id"]: change.get("reason", "") for change in proposed.get("scenes", [])}
    changes = revisions.describe(current, result, revisions.Production.of(module), reasons)
    changes.update(ignored=proposed.get("ignored", []), affected_question_ids=proposed.get("affected_question_ids", []),
                   rebased=revisions.content_hash(current) != revision.base_hash)
    _mark_applied(revision, current, result, changes, admin)
    _follow_qa(db, revision)
    revisions.supersede(db, module.id, ("proposed",), besides=revision.id)
    studio.queue_visuals(db, module)  # a new infographic is made now, for the preview
    render_job = None
    if render and (module.video_asset_id or module.video_url):  # its video, read again under the lock
        db.flush()  # the render's lock reads the module again: with the change
        render_job = _enqueue_render(db, module, admin, choices)
    db.commit()
    logger.info("revision_applied", extra={"revision_id": revision.id, "module_id": module.id, "target": "script",
                                           "scenes": len(changes["scenes"]), "rebased": changes["rebased"],
                                           "render": render_job is not None})
    return _applied_out(db, revision, render_job)


def _apply_quiz(db: Session, revision: ModuleRevision, module: Module, admin: User) -> RevisionApplied:
    db.refresh(module, with_for_update={"key_share": True})  # as a save of the quiz, and before the revision
    db.refresh(revision, with_for_update=True)
    if not _appliable(revision):
        return _applied_out(db, revision, None)
    if module.evaluation is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"«{module.title}» ya no tiene evaluación; pide el cambio de nuevo"
        )
    current = quiz_revisions.content(module)
    proposed = revision.changes or {}
    renamed = quiz_revisions.renames_of(proposed)
    result = _merged_or_409(
        lambda: quiz_revisions.three_way(current, revision.before, revision.after, renamed), code="revision_stale",
        message="La evaluación cambió en {places} desde la propuesta; pide el cambio de nuevo.",
        places=lambda conflict: _quiz_places(conflict, current, revision.before, revision.after),
        detail=_quiz_detail,
    )
    _write_quiz(db, module, result)
    changes = quiz_revisions.describe(current, result, quiz_revisions.reasons_of(proposed), renamed)
    changes.update(ignored=proposed.get("ignored", []), rebased=revisions.content_hash(current) != revision.base_hash)
    _mark_applied(revision, current, result, changes, admin)
    _follow_qa(db, revision)
    revisions.supersede(db, module.id, ("proposed",), besides=revision.id, target="quiz")
    db.commit()
    logger.info("revision_applied", extra={"revision_id": revision.id, "module_id": module.id, "target": "quiz",
                                           "questions": len(changes["questions"]), "rebased": changes["rebased"]})
    return _applied_out(db, revision, None)


def _apply_activity(db: Session, revision: ModuleRevision, admin: User) -> RevisionApplied:
    activity = _activity_of(db, revision)  # held as a save of the activity holds it, and before the revision
    db.refresh(revision, with_for_update=True)
    if not _appliable(revision):
        return _applied_out(db, revision, None)
    _require_kept(db, activity, "aplica el cambio")
    current = activity_revisions.content(activity)
    result = _valid_activity(activity, _merged_or_409(
        lambda: activity_revisions.three_way(current, revision.before, revision.after), code="revision_stale",
        message="La actividad cambió en {places} desde la propuesta; pide el cambio de nuevo.",
        places=lambda conflict: activity_revisions.places(conflict.ids, current, revision.before),
        detail=_activity_detail,
    ))
    activity_revisions.write(activity, result)  # a new version: open plays start again
    changes = activity_revisions.describe(activity.type, current, result)
    changes["rebased"] = revisions.content_hash(current) != revision.base_hash
    _mark_applied(revision, current, result, changes, admin)
    _follow_qa(db, revision)
    revisions.supersede(db, revision.module_id, ("proposed",), besides=revision.id, target="activity",
                        activity_id=activity.id)
    if activity.status == "ready":  # its new lines, voiced (the others keep their clips)
        studio.queue_activity_voice(db, activity)
    db.commit()
    logger.info("revision_applied", extra={"revision_id": revision.id, "module_id": revision.module_id,
                                           "target": "activity", "activity_id": activity.id,
                                           "parts": len(changes["activity"]["entities"]),
                                           "rebased": changes["rebased"]})
    return _applied_out(db, revision, None)


@router.post("/revisions/{revision_id}/apply", response_model=RevisionApplied)
def apply_revision(
    revision_id: int, payload: RevisionApply, db: Session = Depends(get_db), admin: User = Depends(require_admin)
):
    """Make the proposal the module's script, quiz or activity: over edits made since, as long as they are elsewhere.
    With `render`, a script's change produces the module's video again too."""
    revision = _revision_or_404(db, revision_id)
    module = module_or_404(db, revision.module_id)
    if revision.target == "quiz":
        return _apply_quiz(db, revision, module, admin)
    if revision.target == "activity":
        return _apply_activity(db, revision, admin)
    return _apply_script(db, revision, module, payload.render, admin)


@router.post("/revisions/{revision_id}/discard", response_model=Revision)
def discard_revision(revision_id: int, db: Session = Depends(get_db)):
    """Leave a proposal aside (one still being prepared stops)."""
    revision = _revision_or_404(db, revision_id)
    db.refresh(revision, with_for_update=True)
    if revision.status == "discarded":
        return _revision_out(db, revision)
    if revision.status == "applied":
        raise HTTPException(status.HTTP_409_CONFLICT, "Este cambio ya se aplicó; para quitarlo usa «Deshacer»")
    if revision.status not in (*revisions.PENDING, "failed"):
        raise HTTPException(status.HTTP_409_CONFLICT, "Esta propuesta ya no está pendiente")
    job = db.get(Job, revision.job_id) if revision.status == "proposing" and revision.job_id else None
    revision.status = "discarded"
    if job is not None and job.status in queue.ACTIVE:
        queue.cancel(db, job)  # commits the discard with it
    else:
        db.commit()
    return _revision_out(db, revision)


def _check_revertible(revision: ModuleRevision) -> None:
    if revision.status == "reverted":
        raise HTTPException(status.HTTP_409_CONFLICT, "Este cambio ya se deshizo")
    if revision.status != "applied":
        raise HTTPException(status.HTTP_409_CONFLICT, "Solo se puede deshacer un cambio aplicado")


def _undo_summary(revision: ModuleRevision) -> str:
    title = (revision.summary or revision.observation or "un cambio").strip()
    return f"Deshace: {title}"[:MAX_SUMMARY_CHARS]


def _revert_script(db: Session, revision: ModuleRevision, module: Module, admin: User) -> Revision:
    db.refresh(module, with_for_update=True)
    db.refresh(revision, with_for_update=True)
    _check_revertible(revision)
    _require_ai_module(module, "deshacer cambios del guion")
    _require_editable(db, module, "deshaz el cambio")
    current = revisions.script_content(module)
    restored = _merged_or_409(
        lambda: revisions.three_way(current, revision.after, revision.before), code="revision_conflict",
        message="No se puede deshacer: el guion cambió después en {places}. Edítalo a mano o pide otro cambio.",
        places=lambda conflict: _script_places(conflict, current, revision.after), detail=_script_detail,
    )
    undo = revisions.record(db, module, source="revert", before=current, after=restored, user_id=admin.id,
                            summary=_undo_summary(revision))
    if undo is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "No hay nada que deshacer: el guion ya está como antes del cambio")
    undo.scope = {**revisions.empty_scope(), "scene_ids": [change["scene_id"] for change in undo.changes["scenes"]],
                  "reverted_id": revision.id}
    revisions.write_script(module, restored)
    revision.status = "reverted"
    _follow_qa(db, revision, undone=True)
    studio.queue_visuals(db, module)
    db.commit()
    logger.info("revision_reverted", extra={"revision_id": revision.id, "undo_id": undo.id, "module_id": module.id,
                                            "target": "script"})
    return _revision_out(db, undo)


def _revert_quiz(db: Session, revision: ModuleRevision, module: Module, admin: User) -> Revision:
    db.refresh(module, with_for_update={"key_share": True})
    db.refresh(revision, with_for_update=True)
    _check_revertible(revision)
    if module.evaluation is None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"«{module.title}» ya no tiene evaluación")
    current = quiz_revisions.content(module)
    # The questions it gave a new id get their old one back (an update of each, as the change was).
    renamed = {new_id: old_id for old_id, new_id in quiz_revisions.renames_of(revision.changes).items()}
    restored = _merged_or_409(
        lambda: quiz_revisions.three_way(current, revision.after, revision.before, renamed),
        code="revision_conflict",
        message="No se puede deshacer: la evaluación cambió después en {places}. Edítala a mano o pide otro cambio.",
        places=lambda conflict: _quiz_places(conflict, current, revision.after), detail=_quiz_detail,
    )
    _write_quiz(db, module, restored)
    undo = quiz_revisions.record(db, module, source="revert", before=current, after=restored, user_id=admin.id,
                                 summary=_undo_summary(revision), renamed=renamed)
    if undo is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "No hay nada que deshacer: la evaluación ya está como antes del cambio"
        )
    restored_ids = [(change["after"] or change["before"])["id"] for change in undo.changes["questions"]]
    undo.scope = {**revisions.empty_scope(), "question_ids": restored_ids, "reverted_id": revision.id}
    revision.status = "reverted"
    _follow_qa(db, revision, undone=True)
    db.commit()
    logger.info("revision_reverted", extra={"revision_id": revision.id, "undo_id": undo.id, "module_id": module.id,
                                            "target": "quiz"})
    return _revision_out(db, undo)


def _revert_activity(db: Session, revision: ModuleRevision, admin: User) -> Revision:
    activity = _activity_of(db, revision)  # held, then the revision
    db.refresh(revision, with_for_update=True)
    _check_revertible(revision)
    current = activity_revisions.content(activity)
    restored = _valid_activity(activity, _merged_or_409(
        lambda: activity_revisions.three_way(current, revision.after, revision.before), code="revision_conflict",
        message="No se puede deshacer: la actividad cambió después en {places}. Edítala a mano o pide otro cambio.",
        places=lambda conflict: activity_revisions.places(conflict.ids, current, revision.after),
        detail=_activity_detail,
    ))
    undo = activity_revisions.record(db, activity, source="revert", before=current, after=restored,
                                     user_id=admin.id, summary=_undo_summary(revision))
    if undo is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "No hay nada que deshacer: la actividad ya está como antes del cambio"
        )
    undo.scope = {**revisions.empty_scope(), "reverted_id": revision.id}
    activity_revisions.write(activity, restored)  # a new version: open plays start again
    revision.status = "reverted"
    _follow_qa(db, revision, undone=True)
    if activity.status == "ready":
        studio.queue_activity_voice(db, activity)
    db.commit()
    logger.info("revision_reverted", extra={"revision_id": revision.id, "undo_id": undo.id,
                                            "module_id": revision.module_id, "target": "activity",
                                            "activity_id": activity.id})
    return _revision_out(db, undo)


@router.post("/revisions/{revision_id}/revert", response_model=Revision)
def revert_revision(revision_id: int, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    """Undo an applied change: what it changed (scenes and reading, questions, parts of an activity) goes back to what
    it was, and edits made since elsewhere stay. The undo is a change of its own (and can be undone)."""
    revision = _revision_or_404(db, revision_id)
    module = module_or_404(db, revision.module_id)
    if revision.target == "quiz":
        return _revert_quiz(db, revision, module, admin)
    if revision.target == "activity":
        return _revert_activity(db, revision, admin)
    return _revert_script(db, revision, module, admin)
