"""
Interactive activity jobs.

- ai.activities:     a module's script and materials -> 2 or 3 activities, saved as drafts for the admin to review
                     (drafts written earlier by the AI are replaced; what the admin published stays).
- activities.voice:  one voice clip per line of a published activity, in the course's voice (a client's lines in
                     the second presenter's voice, when there is one). A clip is kept by its text and voice: an
                     unchanged line is never paid for twice.
"""

import hashlib
import logging
import tempfile
from pathlib import Path

from sqlalchemy.orm import Session

from app.db.models import Activity, MediaAsset, Module
from app.core.config import get_settings
from app.services import activities as engine
from app.services import revisions, studio
from app.services.ai import designer
from app.services.ai.llm import get_llm
from app.services.media import discard_assets
from app.services.storage import get_storage
from app.services.video.voice import VoiceError, get_voice_provider
from app.worker.jobs.ai import MIN_QUIZ_CONTENT_CHARS, _llm_errors
from app.worker.runner import JobContext, JobError, Reschedule, handler, open_session

logger = logging.getLogger(__name__)

ACTIVITIES_PER_MODULE = 3
MAX_ACTIVITIES = 6  # the admin's limit too (app/api/routes/activities.py)


@handler("ai.activities")
def draft_activities(ctx: JobContext) -> dict:
    with open_session() as db:
        module = db.get(Module, ctx.module_id)
        if module is None:
            return {"skipped": "module deleted"}
        title, language = module.title, module.course.language or "es"
        script = studio.module_content_for_quiz(db, module)
        materials = studio.materials_text(db, module.course_id)
        kept = sum(1 for activity in module.activities if activity.source != "ai" or activity.status == "ready")
    if len(script.strip()) < MIN_QUIZ_CONTENT_CHARS:
        raise JobError("El módulo aún no tiene suficiente contenido para crear actividades.", permanent=True)
    count = min(ACTIVITIES_PER_MODULE, MAX_ACTIVITIES - kept)
    if count <= 0:
        return {"skipped": "the module has its activities"}

    ctx.progress(20, "Diseñando las actividades")
    with _llm_errors():
        drafts = designer.generate_activities(get_llm(), title=title, script=script, materials=materials,
                                              count=count, language=language)
    if not drafts:
        raise JobError("La IA no produjo actividades válidas. Intenta de nuevo.")

    ctx.progress(90, "Guardando las actividades")
    with open_session() as db:
        module = db.get(Module, ctx.module_id)
        if module is None:
            return {"skipped": "module deleted"}
        # The untouched proposals, held before their changes go (as applying one holds the activity first) and read
        # again: one the admin took over meanwhile (edited or published) stays.
        untouched = [a.id for a in module.activities if a.source == "ai" and a.status == "draft"]
        held = (
            db.query(Activity).filter(Activity.id.in_(untouched)).order_by(Activity.id)
            .populate_existing().with_for_update().all() if untouched else []
        )
        replaced = [a for a in held if a.source == "ai" and a.status == "draft"]
        clips = [clip.get("asset_id") for activity in replaced for clip in (activity.narration or {}).values()]
        revisions.forget_activities(db, [activity.id for activity in replaced])
        for activity in replaced:
            db.delete(activity)
        db.flush()
        db.refresh(module)
        kept = [a for a in module.activities]  # read again: the admin may have added some meanwhile
        drafts = drafts[: max(0, MAX_ACTIVITIES - len(kept))]
        start = max((a.order for a in kept), default=0)
        for position, draft in enumerate(drafts, start=start + 1):
            db.add(Activity(module_id=module.id, order=position, type=draft["type"], title=draft["title"],
                            intro=draft["intro"], spec=draft["spec"], status="draft", source="ai"))
        db.commit()
        discard_assets(db, clips)
    logger.info("activities_drafted", extra={"module_id": ctx.module_id, "activities": len(drafts)})
    return {"activities": [draft["type"] for draft in drafts]}


# ── Voice ─────────────────────────────────────────────────────────────


def _voices(activity: Activity) -> tuple[str, str]:
    """The narrator's voice and the client's (the course's second voice, or the same)."""
    settings = activity.module.course.settings or {}
    narrator = settings.get("voice_id") or get_settings().elevenlabs_voice_id
    return narrator, settings.get("co_voice_id") or narrator


def _client_lines(activity: Activity) -> set[str]:
    """What the client says in a conversation (its situations): in the second voice."""
    if activity.type != "decision":
        return set()
    return {f"{node['id']}.situation" for node in activity.spec.get("nodes", [])}


def clip_path(course_id: int, voice_id: str, text: str) -> str:
    digest = hashlib.sha256(f"{voice_id}|{get_settings().elevenlabs_model}|{text.strip()}".encode()).hexdigest()[:24]
    return f"courses/{course_id}/activity-audio/{digest}.mp3"


def _clip_asset(db: Session, course_id: int, path: str, local: Path | None) -> MediaAsset:
    """The asset of a clip (one per file: two activities saying the same line share it)."""
    asset = db.query(MediaAsset).filter(MediaAsset.path == path, MediaAsset.kind == "audio").first()
    if asset is None:
        size = local.stat().st_size if local else None
        asset = MediaAsset(kind="audio", status="ready", bucket=get_storage().bucket, path=path,
                           mime_type="audio/mpeg", course_id=course_id, size_bytes=size)
        db.add(asset)
        db.flush()
    return asset


def _voice_failed(db: Session, job, error: str) -> None:
    activity = db.get(Activity, (job.payload or {}).get("activity_id"))
    if activity is not None:
        activity.voice_status = "failed"


@handler("activities.voice", on_failure=_voice_failed)
def voice_activity(ctx: JobContext) -> dict | Reschedule:
    activity_id = ctx.payload["activity_id"]
    with open_session() as db:
        activity = db.get(Activity, activity_id)
        if activity is None:
            return {"skipped": "activity deleted"}
        version, course_id = activity.version, activity.module.course_id
        lines = engine.line_keys(activity)
        narrator, client = _voices(activity)
        clients = _client_lines(activity)
        activity.voice_status = "pending"
        db.commit()

    storage, provider = get_storage(), None
    clips: dict[str, dict] = {}
    with tempfile.TemporaryDirectory(prefix="activity-voice-") as tmp:
        for number, (key, text) in enumerate(lines.items()):
            voice = client if key in clients else narrator
            path = clip_path(course_id, voice, text)
            local = None
            if not storage.size(path):
                provider = provider or get_voice_provider()
                try:
                    speech = provider.speak(text, voice)
                except VoiceError as exc:
                    raise JobError(str(exc), permanent=exc.permanent) from exc
                local = Path(tmp) / f"{number}.mp3"
                local.write_bytes(speech.audio)
                storage.upload_file(path, local, "audio/mpeg")
            clips[key] = {"path": path, "local": local, "text_hash": engine.text_hash(text)}
            ctx.progress(10 + 85 * (number + 1) // max(1, len(lines)), "Grabando la voz de la actividad")

        with open_session() as db:
            # Locked: an edit committed between this check and the write would be voiced with its old text.
            activity = db.query(Activity).filter(Activity.id == activity_id).with_for_update().first()
            if activity is None:
                return {"skipped": "activity deleted"}
            if activity.version != version:  # edited meanwhile: voice the new text
                return Reschedule(0)
            previous = {clip.get("asset_id") for clip in (activity.narration or {}).values()}
            activity.narration = {
                key: {"asset_id": _clip_asset(db, course_id, clip["path"], clip["local"]).id,
                      "text_hash": clip["text_hash"]}
                for key, clip in clips.items()
            }
            activity.voice_status = "ready"
            db.commit()
            unused = previous - {clip["asset_id"] for clip in activity.narration.values()}
            discard_assets(db, unused)  # the clips of lines that changed, unless another activity says them
    return {"lines": len(clips)}
