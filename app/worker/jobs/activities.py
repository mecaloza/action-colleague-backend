"""
Interactive activity jobs.

- ai.activities: a module's script and materials -> 2 or 3 activities, saved as drafts for the admin to review
  (drafts written earlier by the AI are replaced; what the admin published stays).
"""

import logging

from sqlalchemy.orm import Session

from app.db.models import Activity, MediaAsset, Module
from app.services import studio
from app.services.ai import designer
from app.services.ai.llm import get_llm
from app.services.storage import StorageError, get_storage
from app.services.video.visuals import VisualRequest, cache_path
from app.worker import queue
from app.worker.jobs.ai import MIN_QUIZ_CONTENT_CHARS, _llm_errors
from app.worker.jobs.render import scene_prompt
from app.worker.runner import JobContext, JobError, Reschedule, handler, open_session

logger = logging.getLogger(__name__)

ACTIVITIES_PER_MODULE = 3
MAX_ACTIVITIES = 6  # the admin's limit too (app/api/routes/activities.py)
VISUALS_WAIT_SECONDS = 20


def _infographic_scenes(module: Module) -> list[dict]:
    return [scene for scene in studio.scenes_of(module) if (scene.get("visual") or {}).get("kind") == "infographic"]


def _picture(db: Session, module: Module, scene: dict) -> MediaAsset | None:
    """The infographic made for this scene (see visuals.prepare), as an image the activity can point at."""
    visual = scene.get("visual") or {}
    request = VisualRequest("infographic", visual.get("query") or "", scene_prompt(scene), int(visual.get("variant") or 0))
    path = cache_path(module.course_id, request)
    try:
        if not get_storage().size(path):
            return None
    except StorageError:
        return None
    asset = db.query(MediaAsset).filter(MediaAsset.path == path, MediaAsset.kind == "image").first()
    if asset is None:
        asset = MediaAsset(kind="image", status="ready", bucket=get_storage().bucket, path=path,
                           mime_type="image/png", course_id=module.course_id, original_filename="infografia.png")
        db.add(asset)
        db.flush()
    return asset


@handler("ai.activities")
def draft_activities(ctx: JobContext) -> dict | Reschedule:
    with open_session() as db:
        module = db.get(Module, ctx.module_id)
        if module is None:
            return {"skipped": "module deleted"}
        if queue.active_with_key(db, f"visuals:{module.id}"):  # its infographics are being drawn
            ctx.progress(5, "Esperando las infografías")
            return Reschedule(VISUALS_WAIT_SECONDS)
        title, language = module.title, module.course.language or "es"
        script = studio.module_content_for_quiz(db, module)
        materials = studio.materials_text(db, module.course_id)
        infographics = _infographic_scenes(module)
        kept = sum(1 for activity in module.activities if activity.status == "ready")
    if len(script.strip()) < MIN_QUIZ_CONTENT_CHARS:
        raise JobError("El módulo aún no tiene suficiente contenido para crear actividades.", permanent=True)
    count = min(ACTIVITIES_PER_MODULE, MAX_ACTIVITIES - kept)
    if count <= 0:
        return {"skipped": "the module has its activities"}

    ctx.progress(20, "Diseñando las actividades")
    described = [f"{(scene.get('slide') or {}).get('title', '')}: {scene_prompt(scene)[:300]}" for scene in infographics]
    with _llm_errors():
        drafts = designer.generate_activities(get_llm(), title=title, script=script, materials=materials,
                                              infographics=described, count=count, language=language)
    if not drafts:
        raise JobError("La IA no produjo actividades válidas. Intenta de nuevo.")

    ctx.progress(90, "Guardando las actividades")
    with open_session() as db:
        module = db.get(Module, ctx.module_id)
        if module is None:
            return {"skipped": "module deleted"}
        for activity in [a for a in module.activities if a.status == "draft"]:  # the AI's earlier proposals
            db.delete(activity)
        db.flush()
        start = max((a.order for a in module.activities if a.status == "ready"), default=0)
        for position, draft in enumerate(drafts, start=start + 1):
            picture = None
            if 0 <= draft["infographic"] < len(infographics):
                picture = _picture(db, module, infographics[draft["infographic"]])
            db.add(Activity(module_id=module.id, order=position, type=draft["type"], title=draft["title"],
                            intro=draft["intro"], spec=draft["spec"], status="draft",
                            image_asset_id=picture.id if picture else None))
        db.commit()
    logger.info("activities_drafted", extra={"module_id": ctx.module_id, "activities": len(drafts)})
    return {"activities": [draft["type"] for draft in drafts]}
