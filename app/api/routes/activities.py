"""Admin endpoints of the interactive activities: write, order, publish them, set their picture, try them."""

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_admin
from app.api.lookups import module_or_404
from app.core.config import get_settings
from app.db.models import Activity, ActivityAttempt, MediaAsset, User
from app.schemas.activities import ActivityAdmin, ActivityCreate, ActivityImage, ActivityOrder, ActivityUpdate
from app.schemas.learn import ActivityMoveIn, ActivityMoveOut, ActivityPlay
from app.schemas.media import JobOut
from app.services import activities as engine
from app.services import progress
from app.services import studio
from app.services.activity_spec import SpecError, parse_spec, ready_problems
from app.services.media import MediaResolver, discard_assets
from app.services.media_views import job_out
from app.worker import queue

router = APIRouter(tags=["activities"], dependencies=[Depends(require_admin)])

MAX_ACTIVITIES_PER_MODULE = 6


def _secret() -> str:
    return engine.activity_secret(get_settings().jwt_secret)


def _activity_or_404(db: Session, activity_id: int) -> Activity:
    activity = db.get(Activity, activity_id)
    if activity is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Actividad no encontrada")
    return activity


def _image_ready(db: Session, activity: Activity) -> bool:
    asset = db.get(MediaAsset, activity.image_asset_id) if activity.image_asset_id else None
    return bool(asset and asset.status == "ready")


def _problems(db: Session, activity: Activity) -> list[str]:
    try:
        spec = parse_spec(activity.type, activity.spec)
    except SpecError as exc:
        return [str(exc)]
    return ready_problems(activity.type, spec, _image_ready(db, activity))


def _admin_view(db: Session, activity: Activity, media: MediaResolver | None = None) -> ActivityAdmin:
    media = media or MediaResolver(db).prepare(extra_asset_ids=[activity.image_asset_id])
    return ActivityAdmin(
        id=activity.id, module_id=activity.module_id, order=activity.order, type=activity.type,
        title=activity.title, intro=activity.intro, spec=activity.spec, version=activity.version,
        status=activity.status, image_asset_id=activity.image_asset_id, image_url=media.url(activity.image_asset_id),
        voice_status=activity.voice_status, max_score=_max_score(activity), problems=_problems(db, activity),
        updated_at=activity.updated_at,
    )


def _max_score(activity: Activity) -> int:
    try:
        return engine.max_score(activity)
    except engine.GameError:  # a spec edited by hand that no longer validates: its problems say why
        return 0


def _validated_spec(activity_type: str, data: dict) -> dict:
    try:
        return parse_spec(activity_type, data).model_dump()
    except SpecError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc


def _publishable(db: Session, activity: Activity) -> None:
    if activity.status == "ready" and (problems := _problems(db, activity)):
        raise HTTPException(status.HTTP_409_CONFLICT, "No se puede publicar todavía: " + " ".join(problems))


@router.get("/modules/{module_id}/activities", response_model=list[ActivityAdmin])
def list_activities(module_id: int, db: Session = Depends(get_db)):
    module = module_or_404(db, module_id)
    media = MediaResolver(db).prepare(extra_asset_ids=[a.image_asset_id for a in module.activities])
    return [_admin_view(db, activity, media) for activity in module.activities]


@router.post("/modules/{module_id}/activities", response_model=ActivityAdmin, status_code=status.HTTP_201_CREATED)
def create_activity(module_id: int, payload: ActivityCreate, db: Session = Depends(get_db)):
    module = module_or_404(db, module_id)
    if len(module.activities) >= MAX_ACTIVITIES_PER_MODULE:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Un módulo admite hasta {MAX_ACTIVITIES_PER_MODULE} actividades")
    activity = Activity(module_id=module.id, type=payload.type, title=payload.title.strip(), intro=payload.intro.strip(),
                        spec=_validated_spec(payload.type, payload.spec), status=payload.status,
                        order=max((a.order for a in module.activities), default=0) + 1)
    db.add(activity)
    db.flush()
    _publishable(db, activity)
    if activity.status == "ready":
        studio.queue_activity_voice(db, activity)
    db.commit()
    return _admin_view(db, activity)


@router.post("/modules/{module_id}/activities/generate", response_model=JobOut, status_code=status.HTTP_202_ACCEPTED)
def generate_activities(module_id: int, db: Session = Depends(get_db)):
    """The AI drafts 2 or 3 activities from the module's script (its earlier drafts are replaced)."""
    module = module_or_404(db, module_id)
    studio.queue_activities(db, module)
    db.commit()
    return job_out(queue.active_with_key(db, f"activities:{module.id}"))


@router.put("/activities/{activity_id}", response_model=ActivityAdmin)
def update_activity(activity_id: int, payload: ActivityUpdate, db: Session = Depends(get_db)):
    """A change to what is played (title, intro, spec) is a new version: open plays must start again."""
    activity = _activity_or_404(db, activity_id)
    changed = False
    if payload.title is not None and payload.title.strip() != activity.title:
        activity.title, changed = payload.title.strip(), True
    if payload.intro is not None and payload.intro.strip() != activity.intro:
        activity.intro, changed = payload.intro.strip(), True
    if payload.spec is not None:
        spec = _validated_spec(activity.type, payload.spec)
        if spec != activity.spec:
            activity.spec, changed = spec, True
    if changed:
        activity.version += 1
        activity.source = "admin"  # the admin's now: a new AI proposal never replaces it
    published = payload.status == "ready" and activity.status != "ready"
    unpublished = payload.status == "draft" and activity.status == "ready"
    if payload.status is not None:
        activity.status = payload.status
    _publishable(db, activity)
    if unpublished:  # learners who only waited for it complete the module
        progress.recheck_module(db, activity.module)
    if activity.status == "ready" and (published or changed):  # its lines, voiced (unchanged ones are reused)
        studio.queue_activity_voice(db, activity)
    db.commit()
    return _admin_view(db, activity)


@router.post("/activities/{activity_id}/narration", response_model=JobOut, status_code=status.HTTP_202_ACCEPTED)
def voice_activity(activity_id: int, db: Session = Depends(get_db)):
    """Voice the activity's lines again (e.g. after the course's voice changed)."""
    activity = _activity_or_404(db, activity_id)
    studio.queue_activity_voice(db, activity)
    db.commit()
    return job_out(queue.active_with_key(db, f"activity-voice:{activity.id}"))


@router.delete("/activities/{activity_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_activity(activity_id: int, db: Session = Depends(get_db)):
    """Its plays and voice clips go with it; learners who only waited for it complete the module."""
    activity = _activity_or_404(db, activity_id)
    module, clips = activity.module, [clip.get("asset_id") for clip in (activity.narration or {}).values()]
    db.delete(activity)
    progress.recheck_module(db, module)
    db.commit()
    discard_assets(db, clips)  # unless another activity says the same lines
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/modules/{module_id}/activities/order", response_model=list[ActivityAdmin])
def order_activities(module_id: int, payload: ActivityOrder, db: Session = Depends(get_db)):
    module = module_or_404(db, module_id)
    by_id = {activity.id: activity for activity in module.activities}
    if sorted(payload.ids) != sorted(by_id):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Envía todas las actividades del módulo, una vez cada una")
    for position, activity_id in enumerate(payload.ids, start=1):
        by_id[activity_id].order = position
    db.commit()
    db.refresh(module)
    return list_activities(module_id, db)


@router.put("/activities/{activity_id}/image", response_model=ActivityAdmin)
def set_activity_image(activity_id: int, payload: ActivityImage, db: Session = Depends(get_db)):
    activity = _activity_or_404(db, activity_id)
    if payload.asset_id is not None:
        asset = db.get(MediaAsset, payload.asset_id)
        if asset is None or asset.kind != "image" or asset.status != "ready":
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Elige una imagen ya subida")
    if payload.asset_id != activity.image_asset_id:
        activity.image_asset_id = payload.asset_id
        activity.version += 1  # the zones were placed on the other picture
        if activity.type == "inspection":  # they must be placed again on this one
            activity.spec = {**activity.spec, "zones": [{**zone, "placed": False} for zone in activity.spec["zones"]]}
    _publishable(db, activity)
    db.commit()
    return _admin_view(db, activity)


# ── Preview: the admin plays it as a learner would (never counts for anyone) ──


def _preview_attempt(db: Session, admin: User, attempt_id: int) -> tuple[ActivityAttempt, Activity]:
    attempt = db.query(ActivityAttempt).filter(ActivityAttempt.id == attempt_id).with_for_update().first()
    if attempt is None or attempt.user_id != admin.id or attempt.enrollment_id is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Vista previa no encontrada")
    return attempt, _activity_or_404(db, attempt.activity_id)


@router.post("/activities/{activity_id}/preview", response_model=ActivityPlay)
def preview_activity(activity_id: int, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    activity = _activity_or_404(db, activity_id)
    try:
        attempt = ActivityAttempt(activity_id=activity.id, user_id=admin.id, enrollment_id=None,
                                  version=activity.version, state=engine.start(activity),
                                  max_score=engine.max_score(activity), score=0)
        db.add(attempt)
        db.flush()
        view = engine.view(activity, attempt, _secret())
    except engine.GameError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    db.commit()
    return ActivityPlay(attempt_id=attempt.id, activity_id=activity.id, view=view,
                        narration=engine.narration_urls(db, activity, ["intro", *engine.revealed_lines(view)]),
                        image_url=engine.image_url(db, activity))


@router.post("/activity-previews/{attempt_id}/moves", response_model=ActivityMoveOut)
def preview_move(attempt_id: int, payload: ActivityMoveIn, db: Session = Depends(get_db),
                 admin: User = Depends(require_admin)):
    attempt, activity = _preview_attempt(db, admin, attempt_id)
    try:
        outcome = engine.move(activity, attempt, payload.move, _secret())
    except engine.GameError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    db.commit()
    return ActivityMoveOut(result=outcome.result, done=outcome.done,
                           narration=engine.narration_urls(db, activity, engine.revealed_lines(outcome.result)))
