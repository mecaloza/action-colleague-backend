"""
Recordings, captions and the previous app's media.

- video.compose_recording: the admin's camera recording + their PDF deck -> slides full screen,
  switched at the exact recorded times, with the camera in the presenter bubble.
- media.transcribe: speech-to-text (ElevenLabs Scribe) of a module's video -> WebVTT captions and
  a transcript the AI can use for quizzes.
- legacy.migrate: brings the previous app's videos into the private media bucket (from HeyGen while
  its v1 API lives, or from the public buckets the app used), then processes them like any upload;
  marks the ones that are lost; converts old evaluations to the canonical format.
"""

import logging
import tempfile
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import Enrollment, Evaluation, Job, MediaAsset, Module, ModuleProgress
from app.services import legacy_heygen, quiz
from app.services.legacy_heygen import RETIRED_ON as HEYGEN_RETIRED_ON
from app.services.media import SIGNED_URL_SECONDS, discard_assets
from app.services.storage import Storage, StorageError, get_storage
from app.services.video import captions, compose
from app.services.video.voice import VoiceError, get_voice_provider
from app.worker import queue
from app.worker.runner import JobCancelled, JobContext, JobError, Reschedule, handler, holds_job, open_session

logger = logging.getLogger(__name__)

# The `media.process` purpose of a module's existing video copied to private storage (it keeps the
# module's source), and the job type that processes it: one the previous release doesn't know, so
# during an overlapping deploy it can't take these jobs without knowing what to do with them.
LEGACY_VIDEO = "legacy_video"
LEGACY_VIDEO_JOB = "legacy.video"
LEGACY_BUCKETS = ("course-videos", "user-videos")  # the public buckets the previous app kept videos in
LOST_VIDEO = "El video de la app anterior se perdió; súbelo o prodúcelo de nuevo."
WAIT_FOR_PROCESSING_SECONDS = 10  # before checking again whether an upload finished processing
MAX_PROCESSING_WAIT_SECONDS = 3 * 3600  # an upload still not processed by then will not be
LEGACY_RECHECK_SECONDS = 6 * 3600  # how often to look again for HeyGen videos not finished yet
LEGACY_DOWNLOAD_TIMEOUT_SECONDS = 600
MAX_LEGACY_VIDEO_BYTES = 2 * 1024 * 1024 * 1024
TRANSCRIBE_ATTEMPTS = 2  # a second try covers a transient provider error
PUBLIC_PREFIX = "/storage/v1/object/public/"  # where Supabase serves the objects of a public bucket


def voice_available() -> bool:
    """Narration and transcription (ElevenLabs) are configured, or the offline fakes are on."""
    settings = get_settings()
    return settings.use_fake_providers or bool(settings.elevenlabs_api_key)


def enqueue_transcription(db: Session, module: Module, created_by: int | None = None, commit: bool = False) -> Job:
    """Queue the captions of the module's current video (one active job per module and video)."""
    return queue.enqueue(
        db, "media.transcribe", {"video_asset_id": module.video_asset_id}, course_id=module.course_id,
        module_id=module.id, created_by=created_by, dedupe_key=f"transcribe:{module.id}:{module.video_asset_id}",
        max_attempts=TRANSCRIBE_ATTEMPTS, commit=commit,
    )


def enqueue_captions(db: Session, module: Module, video: MediaAsset | None = None) -> None:
    """Captions for a module's new video, when speech-to-text is configured and the video has sound."""
    video = video or (db.get(MediaAsset, module.video_asset_id) if module.video_asset_id else None)
    if voice_available() and video is not None and (video.meta or {}).get("has_audio", True):
        enqueue_transcription(db, module)


def _delete_files(paths: list[str], module_id: int | None) -> None:
    """Best effort: a leftover file only costs storage."""
    try:
        get_storage().delete([path for path in paths if path])
    except StorageError as exc:
        logger.warning("recording_cleanup_failed", extra={"module_id": module_id, "error": str(exc)[:300]})


def _discard_quietly(db: Session, asset_ids: list[str | None], module_id: int | None) -> None:
    """After the commit: the new video is in place, leftovers only cost storage."""
    try:
        discard_assets(db, asset_ids)
    except Exception:
        db.rollback()
        logger.exception("recording_replaced_cleanup_failed", extra={"module_id": module_id})


# ── Recording with slides ─────────────────────────────────────────────


def _recording_failed(db: Session, job: Job, error: str) -> None:
    module = db.get(Module, job.module_id) if job.module_id else None
    if module:
        module.generation_status, module.generation_error = "failed", error


def _asset_state(db: Session, asset_id: str | None, what: str) -> MediaAsset | None:
    """The asset (None without one); JobError if it can never be used."""
    if not asset_id:
        return None
    asset = db.get(MediaAsset, asset_id)
    if asset is None:
        raise JobError(f"No encontramos {what}; vuelve a subirla.", permanent=True)
    if asset.status == "failed":
        raise JobError(f"No pudimos procesar {what}: {asset.error or 'error desconocido'}", permanent=True)
    if asset.status == "pending":  # the upload was never confirmed: it is not coming
        raise JobError(f"{what.capitalize()} no terminó de subirse; vuelve a subirla.", permanent=True)
    return asset


@dataclass(frozen=True)
class _RecordingInputs:
    """What the composition needs, read up front so no database session stays open during FFmpeg."""

    course_id: int
    camera_id: str
    camera_path: str
    camera_poster_id: str | None
    camera_has_audio: bool
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
        camera = _asset_state(db, payload["recording_asset_id"], "la grabación")
        deck = _asset_state(db, payload.get("deck_asset_id"), "la presentación")
        if any(asset is not None and asset.status != "ready" for asset in (camera, deck)):
            waited = ctx.state["waited_s"] = ctx.state.get("waited_s", 0) + WAIT_FOR_PROCESSING_SECONDS
            if waited > MAX_PROCESSING_WAIT_SECONDS:
                raise JobError("La grabación o la presentación tardó demasiado en procesarse; vuelve a subirla.", permanent=True)
            return Reschedule(WAIT_FOR_PROCESSING_SECONDS)
        module.generation_status, module.generation_error = "generating", None
        db.commit()
        return _RecordingInputs(
            course_id=module.course_id,
            camera_id=camera.id,
            camera_path=camera.path,
            camera_poster_id=(camera.meta or {}).get("poster_asset_id"),
            camera_has_audio=(camera.meta or {}).get("has_audio", True),
            duration=camera.duration_seconds or 0,
            pages=list((deck.meta or {}).get("pages", [])) if deck else [],
        )


@dataclass(frozen=True)
class _Composition:
    """The video and poster made from the deck and the camera, already in storage."""

    video_path: str
    video_bytes: int
    poster_path: str
    poster_bytes: int
    poster_size: tuple[int, int]


def _compose_with_deck(ctx: JobContext, inputs: _RecordingInputs) -> _Composition:
    """Deck pages full screen + the camera in the bubble -> a new video and poster, stored."""
    storage = get_storage()
    timeline = [(float(point["at"]), int(point["slide"])) for point in ctx.payload.get("timeline", [])]
    shown = {page for page, _ in compose.slide_segments(timeline, len(inputs.pages), inputs.duration)}
    with tempfile.TemporaryDirectory(prefix="recording-") as tmp:
        work = Path(tmp)
        ctx.progress(10, "Descargando la grabación")
        camera = work / "camera.mp4"
        storage.download_file(inputs.camera_path, camera)
        pages = [work / f"deck_{index:03d}.png" for index in range(len(inputs.pages))]
        for index in sorted(shown):  # only the pages the recording shows
            storage.download_file(inputs.pages[index], pages[index])
        ctx.progress(30, "Combinando tus diapositivas con la cámara")
        video = work / "video.mp4"
        try:
            first_slide = compose.compose_recording(
                pages, timeline, camera, inputs.duration, video, work, camera_has_audio=inputs.camera_has_audio
            )
        except compose.ComposeTimeout as exc:
            raise JobError("La grabación es demasiado larga para combinarla con las diapositivas.", permanent=True) from exc
        except compose.ComposeError as exc:
            logger.error("recording_compose_failed", extra={"module_id": ctx.module_id, "error": str(exc)[:800]})
            raise JobError("No pudimos combinar la grabación con las diapositivas. Intenta de nuevo.") from exc
        poster = work / "poster.jpg"
        poster_size = compose.poster(first_slide, poster)
        # A folder per run: a worker that lost the job only ever deletes its own copies.
        folder = f"courses/{inputs.course_id}/modules/{ctx.module_id}/recording-{ctx.job_id}-{uuid.uuid4().hex[:8]}"
        made = _Composition(
            f"{folder}/video.mp4", video.stat().st_size, f"{folder}/poster.jpg", poster.stat().st_size, poster_size
        )
        ctx.progress(90, "Guardando el video")
        storage.upload_file(made.video_path, video, "video/mp4")
        storage.upload_file(made.poster_path, poster, "image/jpeg")
        return made


def _new_asset(course_id: int, kind: str, path: str, mime_type: str, size_bytes: int, **fields) -> MediaAsset:
    """The row of a file this job just stored: already processed, so `ready`."""
    return MediaAsset(
        kind=kind, status="ready", bucket=get_storage().bucket, path=path, mime_type=mime_type,
        size_bytes=size_bytes, course_id=course_id, **fields,
    )


def _composition_assets(db: Session, inputs: _RecordingInputs, made: _Composition) -> tuple[MediaAsset, MediaAsset]:
    video = _new_asset(
        inputs.course_id, "video", made.video_path, "video/mp4", made.video_bytes,
        duration_seconds=round(inputs.duration, 2), width=compose.WIDTH, height=compose.HEIGHT,
        meta={"has_audio": inputs.camera_has_audio},
    )
    poster = _new_asset(
        inputs.course_id, "image", made.poster_path, "image/jpeg", made.poster_bytes,
        width=made.poster_size[0], height=made.poster_size[1],
    )
    db.add_all([video, poster])
    db.flush()
    return video, poster


def _attach_recording(ctx: JobContext, inputs: _RecordingInputs, made: _Composition | None) -> dict:
    """Make the recording (or its composition) the module's video, replacing the previous take."""
    payload = ctx.payload
    made_paths = [made.video_path, made.poster_path] if made else []
    with open_session() as db:
        if not holds_job(db, ctx):  # handed back or reaped meanwhile: its next owner attaches
            db.rollback()
            _delete_files(made_paths, ctx.module_id)
            raise JobCancelled(ctx.job_id)
        module = db.get(Module, ctx.module_id, with_for_update=True)
        if module is None:  # deleted meanwhile: nothing will show the composition
            db.rollback()
            _delete_files(made_paths, ctx.module_id)
            return {"skipped": "module deleted"}
        if made:
            video, poster = _composition_assets(db, inputs, made)
        else:  # camera only: the processed recording is the video
            video, poster = db.get(MediaAsset, inputs.camera_id), None
        video_id = video.id
        poster_id = poster.id if poster else inputs.camera_poster_id
        same_video = video_id == module.video_asset_id  # combined again the same way: its captions still fit
        previous = (module.storyboard or {}).get("recording") or {}
        # This take's pieces stay (the camera can be combined again); the previous take's go, like an
        # earlier composition, its poster and captions.
        keep = {inputs.camera_id, inputs.camera_poster_id, payload.get("deck_asset_id"), video_id, poster_id}
        if same_video:
            keep.add(module.captions_asset_id)
        current = [module.video_asset_id, module.poster_asset_id, module.captions_asset_id]
        earlier = [previous.get("recording_asset_id"), previous.get("deck_asset_id")]
        replaced = [asset_id for asset_id in current + earlier if asset_id and asset_id not in keep]
        module.video_asset_id, module.poster_asset_id = video_id, poster_id
        if not same_video:
            module.captions_asset_id = None
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
        if not same_video:
            enqueue_captions(db, module, video)
        db.commit()
        _discard_quietly(db, replaced, ctx.module_id)
    return {"duration_seconds": round(inputs.duration, 2), "slides": len(inputs.pages)}


@handler("video.compose_recording", on_failure=_recording_failed)
def compose_recording(ctx: JobContext) -> dict | Reschedule:
    inputs = _load_inputs(ctx)
    if inputs is None:
        return {"skipped": "module deleted"}
    if isinstance(inputs, Reschedule):
        return inputs
    made = _compose_with_deck(ctx, inputs) if inputs.pages else None
    return _attach_recording(ctx, inputs, made)


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
        raise JobError(str(exc), permanent=exc.permanent) from exc
    timed = [captions.TimedWord(word.text, word.start, word.end) for word in words]
    if not timed:
        return {"captions": False, "reason": "no speech"}

    with tempfile.TemporaryDirectory(prefix="captions-") as tmp:
        vtt = Path(tmp) / "captions.vtt"
        vtt.write_text(captions.to_vtt(timed), encoding="utf-8")
        path = f"courses/{course_id}/modules/{ctx.module_id}/captions-{asset_id}-{uuid.uuid4().hex[:8]}.vtt"
        storage.upload_file(path, vtt, "text/vtt")
        size = vtt.stat().st_size
    transcript = " ".join(word.text for word in timed)
    with open_session() as db:
        current = _current_video(db, ctx.module_id, asset_id)
        if current is None or not holds_job(db, ctx):
            db.rollback()
            _delete_files([path], ctx.module_id)
            return {"skipped": "video replaced meanwhile"}
        module, video = current
        previous = module.captions_asset_id
        captions_asset = _new_asset(course_id, "captions", path, "text/vtt", size, meta={"language": language})
        db.add(captions_asset)
        db.flush()
        module.captions_asset_id = captions_asset.id
        video.meta = {**(video.meta or {}), "transcript": transcript}
        db.commit()
        _discard_quietly(db, [previous], ctx.module_id)
    return {"captions": True, "words": len(timed)}


# ── The previous app's media ──────────────────────────────────────────


def _public_object(url: str | None) -> tuple[str, str] | None:
    """(bucket, path) of a video the previous app kept in one of its public buckets of this project."""
    supabase_url = get_settings().supabase_url
    if not url or not supabase_url:
        return None
    parsed, ours = urlparse(url), urlparse(supabase_url)
    if parsed.hostname != ours.hostname or not parsed.path.startswith(PUBLIC_PREFIX):
        return None
    bucket, _, path = parsed.path[len(PUBLIC_PREFIX):].partition("/")
    # The project is shared with other apps: only the buckets this app used.
    return (bucket, unquote(path)) if bucket in LEGACY_BUCKETS and path else None


def _date_old_completions(db: Session) -> int:
    """Courses completed before `completed_at` existed: the last module they completed dates them."""
    last_module = (
        select(func.max(ModuleProgress.completed_at))
        .where(ModuleProgress.enrollment_id == Enrollment.id)
        .scalar_subquery()
    )
    result = db.execute(
        update(Enrollment)
        .where(Enrollment.status == "completed", Enrollment.completed_at.is_(None), last_module.isnot(None))
        .values(completed_at=last_module)
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return result.rowcount or 0


def _convert_legacy_evaluations(db: Session) -> int:
    """Give the evaluations saved in the previous app's format a canonical `spec`; returns how many."""
    converted = 0
    for evaluation in db.query(Evaluation).filter(Evaluation.spec.is_(None)).all():
        try:
            questions = quiz.normalize_legacy_questions(evaluation.questions)
        except ValueError:  # not JSON: quizzes already show it as empty (quiz.evaluation_questions)
            logger.warning("legacy_evaluation_unreadable", extra={"evaluation_id": evaluation.id})
            continue
        if questions:
            evaluation.spec = quiz.dump_questions(questions)
            converted += 1
    db.commit()
    return converted


def _download(url: str, dest: Path) -> None:
    """A finished HeyGen video (a short-lived signed link) to a local file."""
    written = 0
    with httpx.stream("GET", url, follow_redirects=True, timeout=LEGACY_DOWNLOAD_TIMEOUT_SECONDS) as response:
        response.raise_for_status()
        with dest.open("wb") as fh:
            for chunk in response.iter_bytes(1024 * 1024):
                written += len(chunk)
                if written > MAX_LEGACY_VIDEO_BYTES:
                    raise StorageError("El video de HeyGen es demasiado grande")
                fh.write(chunk)


def _queue_legacy_video(db: Session, module: Module, dest: str) -> str:
    """Process the copied file like an upload; it replaces the module's video only if that is still the old one."""
    asset = db.query(MediaAsset).filter(MediaAsset.path == dest).order_by(MediaAsset.created_at.desc()).first()
    if asset is not None and asset.status in ("ready", "failed"):
        return "processed" if asset.status == "ready" else "failed"  # attached, or it could not be used
    if asset is None:
        asset = MediaAsset(
            kind="video", status="uploaded", bucket=get_storage().bucket, path=dest, mime_type="video/mp4",
            course_id=module.course_id, original_filename=Path(dest).name, meta={"legacy_url": module.video_url},
        )
        db.add(asset)
        db.flush()
    queue.enqueue(
        db, LEGACY_VIDEO_JOB,
        {"asset_id": asset.id, "purpose": LEGACY_VIDEO, "module_id": module.id, "legacy_url": module.video_url},
        course_id=module.course_id, module_id=module.id, dedupe_key=f"media:{asset.id}", commit=False,
    )
    db.commit()
    return "copied"


def _legacy_folder(module: Module) -> str:
    return f"courses/{module.course_id}/modules/{module.id}/legacy"


def _copy_from_heygen(db: Session, storage: Storage, module: Module, heygen_id: str) -> str:
    """Download a finished HeyGen video into the private bucket, while HeyGen's v1 API still answers."""
    dest = f"{_legacy_folder(module)}/heygen-{heygen_id}.mp4"
    if not storage.size(dest):  # a previous pass may have copied it already
        url = legacy_heygen.fresh_url(heygen_id)
        if not url:
            return "waiting"  # still rendering, or HeyGen did not answer: the next pass tries again
        with tempfile.TemporaryDirectory(prefix="legacy-") as tmp:
            local = Path(tmp) / "video.mp4"
            _download(url, local)
            storage.upload_file(dest, local, "video/mp4")
    return _queue_legacy_video(db, module, dest)


def _copy_from_bucket(db: Session, storage: Storage, module: Module, bucket: str, path: str) -> str:
    dest = f"{_legacy_folder(module)}/{Path(path).name}"
    if not storage.size(dest):  # copying over an existing object fails: a previous pass copied it
        storage.copy_from(bucket, path, dest)
    return _queue_legacy_video(db, module, dest)


def _mark_lost(db: Session, module: Module) -> str:
    if module.generation_error != LOST_VIDEO:
        module.generation_status, module.generation_error = "failed", LOST_VIDEO
        db.commit()
    return "lost"


def _migrate_video(db: Session, storage: Storage, module: Module, today) -> str:
    url = module.video_url or ""
    heygen_id = legacy_heygen.video_id(url)
    if heygen_id:
        return _copy_from_heygen(db, storage, module, heygen_id) if today <= HEYGEN_RETIRED_ON else _mark_lost(db, module)
    if url.startswith("/uploads/"):  # the previous app kept these in its container: gone with it
        return _mark_lost(db, module)
    source = _public_object(url)
    return _copy_from_bucket(db, storage, module, *source) if source else "foreign"


def _migrate_videos(db: Session) -> Counter:
    """Every module still on a video of the previous app, one at a time (a failure never stops the others)."""
    storage, today, outcomes = get_storage(), datetime.now(UTC).date(), Counter()
    modules = db.query(Module).filter(Module.video_asset_id.is_(None), Module.video_url.isnot(None), Module.video_url != "")
    for module in modules.all():
        try:
            outcomes[_migrate_video(db, storage, module, today)] += 1
        except Exception as exc:  # StorageError, a HeyGen download...: the next pass tries again
            db.rollback()
            logger.warning("legacy_video_failed", extra={"module_id": module.id, "error": str(exc)[:300]})
            outcomes["error"] += 1
    return outcomes


def _step(db: Session, step: Callable[[Session], int]) -> int | None:
    """One migration step; its failure is logged and never keeps the others from running."""
    try:
        return step(db)
    except Exception:
        db.rollback()
        logger.exception("legacy_step_failed", extra={"step": step.__name__})
        return None


@handler("legacy.migrate")
def migrate_legacy(ctx: JobContext) -> dict | Reschedule:
    with open_session() as db:
        videos = _migrate_videos(db)  # first: HeyGen's v1 API retires on 2026-10-31
        converted = _step(db, _convert_legacy_evaluations)
        dated = _step(db, _date_old_completions)
    logger.info(
        "legacy_migration_pass",
        extra={"videos": dict(videos), "evaluations_converted": converted, "completions_dated": dated},
    )
    if videos["lost"]:
        logger.warning("legacy_videos_lost", extra={"modules": videos["lost"]})
    if videos["waiting"] or videos["error"]:  # e.g. a HeyGen render not finished yet, or a network error
        return Reschedule(LEGACY_RECHECK_SECONDS)
    return {"videos": dict(videos), "evaluations_converted": converted, "completions_dated": dated}
