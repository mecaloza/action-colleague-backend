"""
Changes to a module's script by an observation: the AI proposes one (a job), the admin reviews its diff and applies it
(and, if they want, produces the video again) or discards it. Any applied change — proposed, saved by hand or
rewritten by the AI — can be undone.
"""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import update
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
from app.db.models import Job, Module, ModuleRevision, User
from app.schemas.revisions import (
    Revision,
    RevisionApplied,
    RevisionApply,
    RevisionCreate,
    RevisionProposal,
    RevisionScope,
    RevisionSummary,
    RevisionTarget,
)
from app.services import revisions, studio
from app.services.course_views import ACTIVE_GENERATION
from app.services.media_views import job_out
from app.worker import queue

router = APIRouter(tags=["revisions"], dependencies=[Depends(require_admin)])
logger = logging.getLogger(__name__)

BUSY_WITH_OTHER_CHANGE = "Ya se está preparando otro cambio de este guion; espera a que termine para pedir otro."
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


def _revision_out(db: Session, revision: ModuleRevision) -> Revision:
    author = db.get(User, revision.created_by) if revision.created_by else None
    return Revision(
        **_summary_fields(revision, author.name if author else None),
        scope=RevisionScope.model_validate(revision.scope or {}), unresolved=revision.unresolved or "",
        changes=revision.changes, error=revision.error, job_id=revision.job_id,
    )


def _require_editable(db: Session, module: Module, action: str) -> None:
    """As a manual save: a job would overwrite the change, or produce the video of the script before it."""
    if queue.active_with_key(db, f"render:{module.id}") is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"El video se está produciendo; {action} cuando termine")
    if module.generation_status in ACTIVE_GENERATION:
        raise HTTPException(status.HTTP_409_CONFLICT, f"El guion se está generando; {action} cuando termine")


def _scenes_label(scene_ids: list[str], *scripts: dict) -> str:
    """Scenes by their number, as the admin knows them ("la escena 2", "las escenas 2 y 5"), looked up in the
    first script that has each."""
    numbers = []
    for scene_id in scene_ids:
        for script in scripts:
            ids = [scene.get("id") for scene in script["scenes"]]
            if scene_id in ids:
                numbers.append(ids.index(scene_id) + 1)
                break
    numbers = sorted(set(numbers))
    if not numbers:
        return ""
    if len(numbers) == 1:
        return f"la escena {numbers[0]}"
    return f"las escenas {', '.join(map(str, numbers[:-1]))} y {numbers[-1]}"


def _conflict_places(conflict: revisions.Conflict, *scripts: dict) -> str:
    """Where the script changed since, for the admin: "la escena 2", "las escenas 2 y 5 y la lectura"..."""
    places = [_scenes_label(conflict.scene_ids, *scripts), "la lectura" if conflict.reading else ""]
    return " y ".join(place for place in places if place) or "el guion"


def _supersede(db: Session, module_id: int, statuses: tuple[str, ...], besides: int | None = None) -> None:
    """Older proposals for the module's script give way to the newer change."""
    conditions = [ModuleRevision.module_id == module_id, ModuleRevision.target == "script",
                  ModuleRevision.status.in_(statuses)]
    if besides is not None:
        conditions.append(ModuleRevision.id != besides)
    db.execute(update(ModuleRevision).where(*conditions).values(status="superseded"),
               execution_options={"synchronize_session": False})


# ── Proposals ─────────────────────────────────────────────────────────


@router.post("/modules/{module_id}/revisions", response_model=RevisionProposal,
             status_code=status.HTTP_202_ACCEPTED)
def propose_revision(
    module_id: int, payload: RevisionCreate, db: Session = Depends(get_db), admin: User = Depends(require_admin)
):
    """Ask the AI to change the module's script as the observation says (only the chosen scenes, when given). Nothing
    changes until the proposal is applied."""
    _require_ai()
    module = module_or_404(db, module_id)
    _require_ai_module(module, "pedir cambios al guion con IA")
    db.refresh(module, with_for_update=True)  # two asks at once: one waits and sees the other
    content = revisions.script_content(module)
    if not content["scenes"]:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"«{module.title}» no tiene guion todavía")
    _require_no_active(db, f"draft:{module.id}", "El guion se está escribiendo; pide el cambio cuando termine")
    scene_ids = list(dict.fromkeys(payload.scene_ids or []))
    present = {scene.get("id") for scene in content["scenes"]}
    if any(scene_id not in present for scene_id in scene_ids):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Alguna escena elegida ya no está en el guion; vuelve a cargarlo"
        )
    scope = {**revisions.empty_scope(), "scene_ids": scene_ids, "qa_issue_id": payload.qa_issue_id}
    dedupe_key = f"revise:script:{module.id}"
    active = queue.active_with_key(db, dedupe_key)
    if active is not None:  # a double click gets the same proposal; another observation waits for it
        waiting = db.get(ModuleRevision, (active.payload or {}).get("revision_id"))
        if waiting is None or waiting.observation != payload.observation or waiting.scope != scope:
            raise HTTPException(status.HTTP_409_CONFLICT, BUSY_WITH_OTHER_CHANGE)
        return RevisionProposal(revision=_revision_out(db, waiting), job=job_out(active))
    _supersede(db, module.id, revisions.PENDING)  # "Pedir otro ajuste": the newest observation wins
    revision = ModuleRevision(
        module_id=module.id, target="script", source="qa" if payload.qa_issue_id else "observation",
        status="proposing", observation=payload.observation, scope=scope, base_hash=revisions.content_hash(content),
        before=content, created_by=admin.id,
    )
    db.add(revision)
    db.flush()
    job = queue.enqueue(
        db, "ai.revise", {"revision_id": revision.id}, course_id=module.course_id, module_id=module.id,
        created_by=admin.id, dedupe_key=dedupe_key, max_attempts=AI_JOB_ATTEMPTS, commit=False,
    )
    revision.job_id = job.id
    db.commit()
    return RevisionProposal(revision=_revision_out(db, revision), job=job_out(job))


@router.get("/modules/{module_id}/revisions", response_model=list[RevisionSummary])
def list_revisions(
    module_id: int, target: RevisionTarget | None = None, limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
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
    rows = query.order_by(ModuleRevision.created_at.desc(), ModuleRevision.id.desc()).limit(limit).all()
    return [RevisionSummary(**_summary_fields(revision, author)) for revision, author in rows]


@router.get("/revisions/{revision_id}", response_model=Revision)
def get_revision(revision_id: int, db: Session = Depends(get_db)):
    return _revision_out(db, _revision_or_404(db, revision_id))


# ── Applying, discarding and undoing ──────────────────────────────────


@router.post("/revisions/{revision_id}/apply", response_model=RevisionApplied)
def apply_revision(
    revision_id: int, payload: RevisionApply, db: Session = Depends(get_db), admin: User = Depends(require_admin)
):
    """Make the proposal the module's script: over edits made since, as long as they are in other scenes. With
    `render`, the module's video is produced again too."""
    revision = _revision_or_404(db, revision_id)
    module = module_or_404(db, revision.module_id)
    choices = None
    if payload.render:  # checked before changing anything: the change and its video go together
        _require_voice()
        choices = _render_choices(module.course)
        _check_presenters(choices)
    db.refresh(module, with_for_update=True)  # as a manual save, and before the revision (the job's own lock)
    db.refresh(revision, with_for_update=True)
    if revision.status == "applied":  # a double click: what the first one did
        render_job = queue.active_with_key(db, f"render:{module.id}") if payload.render else None
        return RevisionApplied(revision=_revision_out(db, revision), render_job=render_job and job_out(render_job))
    if revision.status == "proposing":
        raise HTTPException(status.HTTP_409_CONFLICT, "La propuesta aún se está preparando; espera a que termine")
    if revision.status != "proposed":
        raise HTTPException(status.HTTP_409_CONFLICT, "Esta propuesta ya no está vigente; pide el cambio de nuevo")
    _require_ai_module(module, "aplicar cambios al guion con IA")
    _require_editable(db, module, "aplica el cambio")
    current = revisions.script_content(module)
    try:
        result = revisions.three_way(current, revision.before, revision.after)
    except revisions.Conflict as conflict:
        places = _conflict_places(conflict, current, revision.after)
        raise HTTPException(status.HTTP_409_CONFLICT, {
            "code": "revision_stale",
            "message": f"El guion cambió en {places} desde la propuesta; pide el cambio de nuevo.",
            "scene_ids": conflict.scene_ids, "reading": conflict.reading,
        }) from conflict
    except revisions.RevisionError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    revisions.write_script(module, result)
    proposed = revision.changes or {}
    reasons = {change["scene_id"]: change.get("reason", "") for change in proposed.get("scenes", [])}
    changes = revisions.describe(current, result, revisions.Production.of(module), reasons)
    changes.update(ignored=proposed.get("ignored", []), affected_question_ids=proposed.get("affected_question_ids", []),
                   rebased=revisions.content_hash(current) != revision.base_hash)
    revision.before, revision.after, revision.changes = current, result, changes
    revision.status, revision.applied_at, revision.applied_by = "applied", datetime.now(timezone.utc), admin.id
    _supersede(db, module.id, ("proposed",), besides=revision.id)
    studio.queue_visuals(db, module)  # a new infographic is made now, for the preview
    render_job = None
    if payload.render and (module.video_asset_id or module.video_url):
        db.flush()  # the render's lock reads the module again: with the change
        render_job = _enqueue_render(db, module, admin, choices)
    db.commit()
    logger.info("revision_applied", extra={"revision_id": revision.id, "module_id": module.id,
                                           "scenes": len(changes["scenes"]), "rebased": changes["rebased"],
                                           "render": render_job is not None})
    return RevisionApplied(revision=_revision_out(db, revision), render_job=render_job and job_out(render_job))


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


@router.post("/revisions/{revision_id}/revert", response_model=Revision)
def revert_revision(revision_id: int, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    """Undo an applied change: the scenes (and reading) it changed go back to what they were, and edits made since in
    other places stay. The undo is a change of its own (and can be undone)."""
    revision = _revision_or_404(db, revision_id)
    module = module_or_404(db, revision.module_id)
    db.refresh(module, with_for_update=True)
    db.refresh(revision, with_for_update=True)
    if revision.status == "reverted":
        raise HTTPException(status.HTTP_409_CONFLICT, "Este cambio ya se deshizo")
    if revision.status != "applied":
        raise HTTPException(status.HTTP_409_CONFLICT, "Solo se puede deshacer un cambio aplicado")
    _require_ai_module(module, "deshacer cambios del guion")
    _require_editable(db, module, "deshaz el cambio")
    current = revisions.script_content(module)
    try:
        restored = revisions.three_way(current, revision.after, revision.before)
    except revisions.Conflict as conflict:
        places = _conflict_places(conflict, current, revision.after)
        raise HTTPException(status.HTTP_409_CONFLICT, {
            "code": "revision_conflict",
            "message": f"No se puede deshacer: después cambió {places}. Edítalo a mano o pide otro cambio.",
            "scene_ids": conflict.scene_ids, "reading": conflict.reading,
        }) from conflict
    except revisions.RevisionError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    title = (revision.summary or revision.observation or "un cambio").strip()
    undo = revisions.record(
        db, module, source="revert", before=current, after=restored, user_id=admin.id,
        summary=f"Deshace: {title}"[:MAX_SUMMARY_CHARS],
    )
    if undo is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "No hay nada que deshacer: el guion ya está como antes del cambio")
    undo.scope = {**revisions.empty_scope(), "scene_ids": [change["scene_id"] for change in undo.changes["scenes"]],
                  "reverted_id": revision.id}
    revisions.write_script(module, restored)
    revision.status = "reverted"
    studio.queue_visuals(db, module)
    db.commit()
    logger.info("revision_reverted", extra={"revision_id": revision.id, "undo_id": undo.id, "module_id": module.id})
    return _revision_out(db, undo)
