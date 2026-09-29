"""
Recordings, captions and the previous app's media.

- video.compose_recording: the admin's camera recording + their PDF deck -> slides full screen,
  switched at the exact recorded times, with the camera in the presenter bubble.
- media.transcribe: speech-to-text (ElevenLabs Scribe) of a module's video -> WebVTT captions and
  a transcript the AI can use for quizzes.
- legacy.migrate: copies videos the previous app kept in public buckets into the private media
  bucket (then processed like any upload) and converts old evaluations to the canonical format.
"""

import logging
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import Enrollment, Evaluation, Job, MediaAsset, Module, ModuleProgress
from app.services import quiz
from app.services.legacy_heygen import RETIRED_ON as HEYGEN_RETIRED_ON
from app.services.media import SIGNED_URL_SECONDS, discard_assets
from app.services.storage import Storage, StorageError, get_storage
from app.services.video import captions, compose
from app.services.video.voice import VoiceError, get_voice_provider
from app.worker import queue
from app.worker.runner import JobContext, JobError, Reschedule, handler, open_session

logger = logging.getLogger(__name__)

# Internal purpose of the `media.process` jobs the legacy migration queues: a module's existing video,
# copied to private storage (it keeps the module's source and is processed like any upload).
LEGACY_VIDEO = "legacy_video"
WAIT_FOR_PROCESSING_SECONDS = 10  # before checking again whether an upload finished processing
LEGACY_RECHECK_SECONDS = 6 * 3600  # how often to look again for HeyGen videos copied to Storage meanwhile
TRANSCRIBE_ATTEMPTS = 2  # a second try covers a transient provider error
PUBLIC_PREFIX = "/storage/v1/object/public/"  # where Supabase serves the objects of a public bucket


def voice_available() -> bool:
    """Narration and transcription (ElevenLabs) are configured, or the offline fakes are on."""
    settings = get_settings()
    return settings.use_fake_providers or bool(settings.elevenlabs_api_key)


def enqueue_transcription(db: Session, module: Module, created_by: int | None = None, commit: bool = False) -> Job:
    """Queue the captions of the module's current video (one active job per video)."""
    return queue.enqueue(
        db, "media.transcribe", {"video_asset_id": module.video_asset_id}, course_id=module.course_id,
        module_id=module.id, created_by=created_by, dedupe_key=f"transcribe:{module.video_asset_id}",
        max_attempts=TRANSCRIBE_ATTEMPTS, commit=commit,
    )


def enqueue_captions(db: Session, module: Module) -> None:
    """Captions for a module's new video, when speech-to-text is configured (the caller commits)."""
    if voice_available() and module.video_asset_id:
        enqueue_transcription(db, module)


# ── Recording with slides ─────────────────────────────────────────────


def _recording_failed(db: Session, job: Job, error: str) -> None:
    module = db.get(Module, job.module_id) if job.module_id else None
    if module:
        module.generation_status, module.generation_error = "failed", error


def _ready_asset(db: Session, asset_id: str | None, what: str) -> MediaAsset | Reschedule | None:
    """The asset once processed; Reschedule while it still is; JobError if it failed or vanished."""
    if not asset_id:
        return None
    asset = db.get(MediaAsset, asset_id)
    if asset is None:
        raise JobError(f"No encontramos {what}; vuelve a subirla.", permanent=True)
    if asset.status == "failed":
        raise JobError(f"No pudimos procesar {what}: {asset.error or 'error desconocido'}", permanent=True)
    if asset.status != "ready":
        return Reschedule(WAIT_FOR_PROCESSING_SECONDS)
    return asset


@dataclass(frozen=True)
class _RecordingInputs:
    """What the composition needs, read up front so no database session stays open during FFmpeg."""

    course_id: int
    camera_id: str
    camera_path: str
    camera_poster_id: str | None
    duration: float
    pages: list[str]  # storage paths of the deck's page images; empty without a deck


def _load_inputs(ctx: JobContext) -> _RecordingInputs | Reschedule | None:
    """Mark the module as generating and read the recording (and deck) it is made from.

    None when the module was deleted; Reschedule while either upload is still being processed.
    """
    payload = ctx.payload
    with open_session() as db:
        module = db.get(Module, ctx.module_id)
        if module is None:
            return None
        camera = _ready_asset(db, payload["recording_asset_id"], "la grabación")
        deck = _ready_asset(db, payload.get("deck_asset_id"), "la presentación")
        for waiting in (camera, deck):
            if isinstance(waiting, Reschedule):
                return waiting
        module.generation_status, module.generation_error = "generating", None
        db.commit()
        return _RecordingInputs(
            course_id=module.course_id,
            camera_id=camera.id,
            camera_path=camera.path,
            camera_poster_id=(camera.meta or {}).get("poster_asset_id"),
            duration=camera.duration_seconds or 0,
            pages=list((deck.meta or {}).get("pages", [])) if deck else [],
        )


def _new_asset(course_id: int, kind: str, path: str, mime_type: str, size_bytes: int, **fields) -> MediaAsset:
    """The row of a file this job just stored: already processed, so `ready`."""
    return MediaAsset(
        kind=kind, status="ready", bucket=get_storage().bucket, path=path, mime_type=mime_type,
        size_bytes=size_bytes, course_id=course_id, **fields,
    )


def _compose_with_deck(ctx: JobContext, inputs: _RecordingInputs) -> tuple[str, str]:
    """Deck pages full screen + the camera in the bubble -> a new video and poster. Returns their asset ids."""
    storage = get_storage()
    timeline = [(float(point["at"]), int(point["slide"])) for point in ctx.payload.get("timeline", [])]
    with tempfile.TemporaryDirectory(prefix="recording-") as tmp:
        work = Path(tmp)
        ctx.progress(10, "Descargando la grabación")
        camera = work / "camera.mp4"
        storage.download_file(inputs.camera_path, camera)
        pages = []
        for index, path in enumerate(inputs.pages):
            page = work / f"deck_{index:03d}.png"
            storage.download_file(path, page)
            pages.append(page)
        ctx.progress(30, "Combinando tus diapositivas con la cámara")
        video = work / "video.mp4"
        try:
            compose.compose_recording(pages, timeline, camera, inputs.duration, video, work)
        except compose.ComposeError as exc:
            logger.error("recording_compose_failed", extra={"module_id": ctx.module_id, "error": str(exc)[:800]})
            raise JobError("No pudimos combinar la grabación con las diapositivas. Intenta de nuevo.") from exc
        poster = work / "poster.jpg"
        first_page = compose.slide_segments(timeline, len(pages), inputs.duration)[0][0]
        poster_size = compose.poster(pages[first_page], poster)
        folder = f"courses/{inputs.course_id}/modules/{ctx.module_id}/recording-{ctx.job_id}"
        video_path, poster_path = f"{folder}/video.mp4", f"{folder}/poster.jpg"
        ctx.progress(90, "Guardando el video")
        storage.upload_file(video_path, video, "video/mp4")
        storage.upload_file(poster_path, poster, "image/jpeg")
        video_asset = _new_asset(
            inputs.course_id, "video", video_path, "video/mp4", video.stat().st_size,
            duration_seconds=round(inputs.duration, 2), width=compose.WIDTH, height=compose.HEIGHT,
        )
        poster_asset = _new_asset(
            inputs.course_id, "image", poster_path, "image/jpeg", poster.stat().st_size,
            width=poster_size[0], height=poster_size[1],
        )
        with open_session() as db:
            db.add_all([video_asset, poster_asset])
            db.commit()
            return video_asset.id, poster_asset.id


def _attach_recording(ctx: JobContext, inputs: _RecordingInputs, video_id: str, poster_id: str | None) -> bool:
    """Make the video the module's, replacing an earlier composition. False if the module was deleted meanwhile."""
    payload = ctx.payload
    with open_session() as db:
        module = db.get(Module, ctx.module_id)
        if module is None:
            return False
        # A previous composition goes; the camera recording (and its poster) stays: it can be combined again.
        keep = {inputs.camera_id, inputs.camera_poster_id, video_id, poster_id}
        current = (module.video_asset_id, module.poster_asset_id, module.captions_asset_id)
        replaced = [asset_id for asset_id in current if asset_id not in keep]
        module.video_asset_id, module.poster_asset_id, module.captions_asset_id = video_id, poster_id, None
        module.duration_seconds = round(inputs.duration, 2)
        module.video_url, module.source = "", "recording"
        module.generation_status, module.generation_error = "completed", None
        module.storyboard = {
            **(module.storyboard or {}),
            "recording": {
                "recording_asset_id": inputs.camera_id,
                "deck_asset_id": payload.get("deck_asset_id"),
                "timeline": payload.get("timeline", []),
            },
        }
        enqueue_captions(db, module)
        db.commit()
        discard_assets(db, replaced)
    return True


@handler("video.compose_recording", on_failure=_recording_failed)
def compose_recording(ctx: JobContext) -> dict | Reschedule:
    inputs = _load_inputs(ctx)
    if inputs is None:
        return {"skipped": "module deleted"}
    if isinstance(inputs, Reschedule):
        return inputs
    if inputs.pages:
        video_id, poster_id = _compose_with_deck(ctx, inputs)
    else:  # camera only: the processed recording is the video
        video_id, poster_id = inputs.camera_id, inputs.camera_poster_id
    if not _attach_recording(ctx, inputs, video_id, poster_id):
        if inputs.pages:  # the composition was made for a module that no longer exists
            with open_session() as db:
                discard_assets(db, [video_id, poster_id])
        return {"skipped": "module deleted"}
    return {"duration_seconds": round(inputs.duration, 2), "slides": len(inputs.pages)}


# ── Captions from speech ──────────────────────────────────────────────


def _current_video(db: Session, module_id: int | None, asset_id: str) -> tuple[Module, MediaAsset] | None:
    """The module and its video asset, unless the module was deleted or now shows another video."""
    module, asset = db.get(Module, module_id), db.get(MediaAsset, asset_id)
    if module is None or asset is None or module.video_asset_id != asset_id:
        return None
    return module, asset


@handler("media.transcribe")
def transcribe(ctx: JobContext) -> dict:
    asset_id = ctx.payload["video_asset_id"]
    with open_session() as db:
        current = _current_video(db, ctx.module_id, asset_id)
        if current is None:
            return {"skipped": "video replaced or deleted"}
        module, video = current
        language, course_id, video_path = module.course.language or "es", module.course_id, video.path
    storage = get_storage()
    url = storage.signed_urls([video_path], SIGNED_URL_SECONDS).get(video_path)
    if not url:
        raise JobError("No encontramos el video para transcribirlo.", permanent=True)
    ctx.progress(20, "Transcribiendo el audio")
    try:
        words = get_voice_provider().transcribe(url, language)
    except VoiceError as exc:
        raise JobError(str(exc)) from exc
    timed = [captions.TimedWord(word.text, word.start, word.end) for word in words]
    if not timed:
        return {"captions": False, "reason": "no speech"}

    with tempfile.TemporaryDirectory(prefix="captions-") as tmp:
        vtt = Path(tmp) / "captions.vtt"
        vtt.write_text(captions.to_vtt(timed), encoding="utf-8")
        path = f"courses/{course_id}/modules/{ctx.module_id}/captions-{asset_id}.vtt"
        storage.upload_file(path, vtt, "text/vtt")
        size = vtt.stat().st_size
    transcript = " ".join(word.text for word in timed)
    with open_session() as db:
        current = _current_video(db, ctx.module_id, asset_id)
        if current is None:
            storage.delete([path])
            return {"skipped": "video replaced meanwhile"}
        module, video = current
        previous = module.captions_asset_id
        captions_asset = _new_asset(course_id, "captions", path, "text/vtt", size, meta={"language": language})
        db.add(captions_asset)
        db.flush()
        module.captions_asset_id = captions_asset.id
        video.meta = {**(video.meta or {}), "transcript": transcript}
        db.commit()
        discard_assets(db, [previous])
    return {"captions": True, "words": len(timed)}


# ── The previous app's media ──────────────────────────────────────────


def _public_object(url: str | None) -> tuple[str, str] | None:
    """(bucket, path) of a public Storage URL of this project, or None."""
    supabase_url = get_settings().supabase_url
    if not url or not supabase_url:
        return None
    parsed, ours = urlparse(url), urlparse(supabase_url)
    if parsed.hostname != ours.hostname or not parsed.path.startswith(PUBLIC_PREFIX):
        return None
    bucket, _, path = parsed.path[len(PUBLIC_PREFIX):].partition("/")
    return (bucket, unquote(path)) if bucket and path else None


def _date_old_completions(db: Session) -> int:
    """Courses completed before `completed_at` existed: the last module they completed dates them."""
    last_module = (
        select(func.max(ModuleProgress.completed_at))
        .where(ModuleProgress.enrollment_id == Enrollment.id)
        .scalar_subquery()
    )
    result = db.execute(
        update(Enrollment)
        .where(Enrollment.status == "completed", Enrollment.completed_at.is_(None))
        .values(completed_at=func.coalesce(last_module, Enrollment.enrolled_at))
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return result.rowcount or 0


def _convert_legacy_evaluations(db: Session) -> int:
    """Give the evaluations saved in the previous app's format a canonical `spec`; returns how many."""
    converted = 0
    for evaluation in db.query(Evaluation).filter(Evaluation.spec.is_(None)).all():
        questions = quiz.normalize_legacy_questions(evaluation.questions)
        if questions:
            evaluation.spec = quiz.dump_questions(questions)
            converted += 1
    db.commit()
    return converted


def _copy_legacy_video(db: Session, storage: Storage, module: Module, bucket: str, path: str) -> bool:
    """Copy the module's public video into the private bucket and queue its processing. False if the copy failed."""
    filename = Path(path).name
    dest = f"courses/{module.course_id}/modules/{module.id}/legacy/{filename}"
    try:
        storage.copy_from(bucket, path, dest)
    except StorageError as exc:
        logger.warning("legacy_copy_failed", extra={"module_id": module.id, "error": str(exc)})
        return False
    asset = MediaAsset(
        kind="video", status="uploaded", bucket=storage.bucket, path=dest, mime_type="video/mp4",
        course_id=module.course_id, original_filename=filename,
    )
    db.add(asset)
    db.flush()
    module.generation_status = "queued"
    queue.enqueue(
        db, "media.process", {"asset_id": asset.id, "purpose": LEGACY_VIDEO, "module_id": module.id},
        course_id=module.course_id, module_id=module.id, dedupe_key=f"media:{asset.id}", commit=False,
    )
    db.commit()
    return True


@handler("legacy.migrate")
def migrate_legacy(ctx: JobContext) -> dict | Reschedule:
    storage = get_storage()
    copied = waiting = 0
    with open_session() as db:
        dated = _date_old_completions(db)
        converted = _convert_legacy_evaluations(db)
        modules = db.query(Module).filter(Module.video_asset_id.is_(None), Module.video_url.isnot(None)).all()
        for module in modules:
            url = module.video_url or ""
            if url.startswith("heygen://"):
                waiting += 1  # the HeyGen persister copies it to Storage first
                continue
            source = _public_object(url)
            if source is not None and _copy_legacy_video(db, storage, module, *source):
                copied += 1
    logger.info(
        "legacy_migration_pass",
        extra={"videos_copied": copied, "evaluations_converted": converted, "completions_dated": dated, "waiting": waiting},
    )
    if waiting:
        if datetime.now(UTC).date() <= HEYGEN_RETIRED_ON:
            return Reschedule(LEGACY_RECHECK_SECONDS)
        logger.warning("legacy_heygen_videos_unrecoverable", extra={"modules": waiting})
    return {"videos_copied": copied, "evaluations_converted": converted, "unrecoverable": waiting}
