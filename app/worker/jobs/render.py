"""
video.render: storyboard -> narrated, captioned MP4 with the AI presenter in a bubble.

Phases (the state is saved between them, so a restart or a wait resumes where it left off):
  1. narrate   — ElevenLabs speech per scene, with character timings; then the master narration
                 (exact timings) and the captions, all kept in storage.
  2. presenter — HeyGen animates an avatar with that same narration. The job reschedules itself
                 while HeyGen works instead of holding a worker. Any presenter failure only
                 drops the bubble: the video is still produced, with a warning for the admin.
  3. compose   — branded slides + narration + bubble -> MP4, poster and WebVTT; attached to the
                 module, replacing its previous video.

Progress goes 5-48% while narrating, 50-70% with the presenter and 75-95% composing.
"""

import logging
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import Job, MediaAsset, Module
from app.services import studio
from app.services.media import SIGNED_URL_SECONDS, discard_assets
from app.services.slides.render import render as render_slide
from app.services.slides.spec import Slide, SlideContext
from app.services.storage import StorageError, get_storage
from app.services.video import captions, compose
from app.services.video.avatar import MAX_ASSET_BYTES, AvatarError, AvatarProvider, get_avatar_provider
from app.services.video.voice import VoiceError, get_voice_provider
from app.worker.runner import JobContext, JobError, Reschedule, handler, open_session

logger = logging.getLogger(__name__)

PRESENTER_POLL_SECONDS = 30
MAX_PRESENTER_WAIT_SECONDS = 45 * 60
MAX_PRESENTER_ERRORS = 5
PRESENTER_BACKGROUND = "#1D1D1D"
MAX_LOG_ERROR_CHARS = 800  # of an FFmpeg error kept in the log line


@dataclass
class RenderInput:
    """What a render needs from the database, read once at the start."""

    course_id: int
    course_title: str
    language: str
    module_label: str
    scenes: list[dict]
    voice_id: str
    avatar_id: str
    presenter: bool
    theme: str


def _render_failed(db: Session, job: Job, error: str) -> None:
    """The job gave up: show the failure on the module."""
    module = db.get(Module, job.module_id) if job.module_id else None
    if module:
        module.generation_status, module.generation_error = "failed", error


def _load(ctx: JobContext) -> RenderInput | None:
    """What the render needs, read up front so no session stays open; marks the module as generating."""
    settings = get_settings()
    with open_session() as db:
        module = db.get(Module, ctx.module_id)
        if module is None:
            return None
        scenes = studio.scenes_of(module)
        if not scenes:
            raise JobError("El módulo no tiene guion; genéralo antes de producir el video.")
        course = module.course
        options = {**(course.settings or {}), **ctx.payload}
        module.generation_status, module.generation_error = "generating", None
        db.commit()
        return RenderInput(
            course_id=course.id,
            course_title=course.title,
            language=course.language or "es",
            module_label=f"Módulo {module.order}",
            scenes=scenes,
            voice_id=options.get("voice_id") or settings.elevenlabs_voice_id,
            avatar_id=options.get("avatar_id") or "",
            presenter=bool(options.get("presenter", True)),
            theme=options.get("theme") or "dark",
        )


def _module_folder(render: RenderInput, ctx: JobContext) -> str:
    return f"courses/{render.course_id}/modules/{ctx.module_id}"


def _intermediate_prefix(render: RenderInput, ctx: JobContext) -> str:
    """Storage folder of the files the phases hand to each other; `_cleanup` deletes them at the end."""
    return f"{_module_folder(render, ctx)}/render/{ctx.job_id}"


def _local_copy(work: Path, name: str, storage_path: str) -> Path:
    """`name` inside the work folder: downloaded from storage unless this run already has it."""
    local = work / name
    if not local.exists():
        get_storage().download_file(storage_path, local)
    return local


# ── 1. Narration ──────────────────────────────────────────────────────


def _scene_file(index: int) -> str:
    return f"scene_{index:02d}.mp3"


def _speak_scenes(ctx: JobContext, render: RenderInput, work: Path) -> None:
    """Narrate the scenes not done yet; the state remembers the finished ones across restarts."""
    storage, voice = get_storage(), get_voice_provider()
    done = ctx.state.setdefault("narration", {})
    narrations = [scene["narration"] for scene in render.scenes]
    for index, text in enumerate(narrations):
        if str(index) in done:
            continue
        ctx.progress(5 + int(40 * index / len(narrations)), f"Narrando la escena {index + 1} de {len(narrations)}")
        try:
            speech = voice.speak(
                text,
                render.voice_id,
                previous_text=narrations[index - 1] if index else "",
                next_text=narrations[index + 1] if index + 1 < len(narrations) else "",
            )
        except VoiceError as exc:
            raise JobError(str(exc)) from exc
        local = work / _scene_file(index)
        local.write_bytes(speech.audio)
        path = f"{_intermediate_prefix(render, ctx)}/{_scene_file(index)}"
        storage.upload_file(path, local, "audio/mpeg")
        done[str(index)] = {"path": path, "alignment": asdict(speech.alignment) if speech.alignment else None}


def _caption_words(scene: dict, alignment: dict | None, start: float, length: float) -> list[captions.TimedWord]:
    """The scene's words with their time in the video: from the speech alignment, else spread over the scene."""
    if alignment:
        return captions.words_from_alignment(alignment["characters"], alignment["starts"], alignment["ends"], start)
    return captions.words_from_text(scene["narration"], start, start + length)


def _narrate(ctx: JobContext, render: RenderInput, work: Path) -> None:
    _speak_scenes(ctx, render, work)

    ctx.progress(47, "Uniendo la narración")
    done = ctx.state["narration"]
    scene_audio = [
        _local_copy(work, _scene_file(index), done[str(index)]["path"]) for index in range(len(render.scenes))
    ]
    narration = compose.build_narration(scene_audio, work)

    words = []
    for index, scene in enumerate(render.scenes):
        alignment = done[str(index)]["alignment"]
        words += _caption_words(scene, alignment, narration.starts[index], narration.lengths[index])
    vtt = work / "captions.vtt"
    vtt.write_text(captions.to_vtt(words), encoding="utf-8")

    storage, prefix = get_storage(), _intermediate_prefix(render, ctx)
    master_path, captions_path = f"{prefix}/master.flac", f"{prefix}/captions.vtt"
    storage.upload_file(master_path, narration.audio, "audio/flac")
    storage.upload_file(captions_path, vtt, "text/vtt")
    ctx.state.update(
        master_path=master_path,
        captions_path=captions_path,
        starts=narration.starts,
        lengths=narration.lengths,
        total=narration.total,
    )


def _narration_from_state(ctx: JobContext, work: Path) -> compose.Narration:
    """The master narration (downloaded unless this run made it) with the timings saved in the state."""
    master = _local_copy(work, "master.flac", ctx.state["master_path"])
    return compose.Narration(master, ctx.state["starts"], ctx.state["lengths"], ctx.state["total"])


# ── 2. Presenter ──────────────────────────────────────────────────────


def _drop_presenter(ctx: JobContext, reason: str) -> None:
    """Go on to the composition without the presenter bubble, telling the admin why."""
    ctx.state["warning"] = f"{reason} El video se produjo sin presentador."
    ctx.state["phase"] = "compose"


def _skip_presenter(ctx: JobContext, reason: str) -> None:
    """The presenter failed: log it and drop the bubble."""
    logger.warning("presenter_skipped", extra={"module_id": ctx.module_id, "reason": reason})
    _drop_presenter(ctx, reason)


def _plan_presenter(ctx: JobContext, render: RenderInput) -> None:
    """After the narration: the presenter phase runs only if one was chosen and can be rendered."""
    configured = get_avatar_provider() is not None
    if render.presenter and render.avatar_id and configured:
        ctx.state["phase"] = "presenter"
    elif render.presenter:
        _drop_presenter(ctx, "No elegiste presentador." if configured else "No hay un presentador configurado.")
    else:
        ctx.state["phase"] = "compose"


def _start_presenter(ctx: JobContext, render: RenderInput, work: Path, provider: AvatarProvider) -> Reschedule:
    """Send the narration to HeyGen and start waiting for the presenter."""
    ctx.progress(50, "Preparando al presentador")
    audio = compose.to_mp3(_narration_from_state(ctx, work).audio, work / "narration.mp3")
    audio_url = None
    if audio.stat().st_size > MAX_ASSET_BYTES:  # too big to upload to HeyGen: let it download it
        storage = get_storage()
        path = f"{_intermediate_prefix(render, ctx)}/narration.mp3"
        storage.upload_file(path, audio, "audio/mpeg")
        ctx.state["narration_mp3_path"] = path  # intermediate too: `_cleanup` deletes it
        audio_url = storage.signed_urls([path], SIGNED_URL_SECONDS).get(path)
    ctx.state["heygen"].update(
        video_id=provider.start(audio, render.avatar_id, PRESENTER_BACKGROUND, audio_url),
        started_at=time.time(),
    )
    ctx.progress(55, "El presentador se está grabando")
    return Reschedule(PRESENTER_POLL_SECONDS)


def _poll_presenter(
    ctx: JobContext, render: RenderInput, work: Path, provider: AvatarProvider
) -> Reschedule | None:
    """Check on HeyGen: keep waiting (Reschedule), give up on the presenter, or fetch the finished video."""
    heygen = ctx.state["heygen"]
    status = provider.status(heygen["video_id"])
    if status.status == "failed":
        _skip_presenter(ctx, f"HeyGen no pudo generar el presentador ({status.error or 'sin detalle'}).")
        return None
    if status.status != "completed" or not status.video_url:
        if time.time() - heygen["started_at"] > MAX_PRESENTER_WAIT_SECONDS:
            _skip_presenter(ctx, "El presentador tardó demasiado en generarse.")
            return None
        ctx.progress(60, "El presentador se está grabando")
        return Reschedule(PRESENTER_POLL_SECONDS)

    ctx.progress(70, "Descargando al presentador")
    local = work / "presenter.mp4"
    provider.download(status.video_url, local)
    path = f"{_intermediate_prefix(render, ctx)}/presenter.mp4"
    get_storage().upload_file(path, local, "video/mp4")
    ctx.state.update(avatar_path=path, phase="compose")
    return None


def _presenter(ctx: JobContext, render: RenderInput, work: Path) -> Reschedule | None:
    """Advance the HeyGen render one step. Returns Reschedule while it is still working."""
    provider = get_avatar_provider()
    heygen = ctx.state.setdefault("heygen", {})
    try:
        if not heygen.get("video_id"):
            return _start_presenter(ctx, render, work, provider)
        return _poll_presenter(ctx, render, work, provider)
    except AvatarError as exc:
        errors = heygen.get("errors", 0) + 1
        heygen["errors"] = errors
        if exc.retryable and errors < MAX_PRESENTER_ERRORS:
            return Reschedule(PRESENTER_POLL_SECONDS * errors)
        _skip_presenter(ctx, str(exc))
        return None


# ── 3. Composition ────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Upload:
    """A finished file: where it goes in storage, its type and its local copy."""

    path: str
    mime: str
    local: Path


@dataclass
class _Composition:
    """The files the composition leaves in the work folder."""

    video: Path
    poster: Path
    poster_size: tuple[int, int]
    vtt: Path
    duration: float
    presenter: bool  # whether the video carries the presenter bubble


def _draw_slides(render: RenderInput, work: Path, presenter: bool) -> list[Path]:
    """One PNG per scene; with a presenter, the layouts leave its corner free."""
    paths = []
    for index, scene in enumerate(render.scenes):
        context = SlideContext(
            course_title=render.course_title, module_label=render.module_label, index=index + 1,
            total=len(render.scenes), theme=render.theme, presenter=presenter,
        )
        path = work / f"slide_{index:02d}.png"
        render_slide(Slide.model_validate(scene["slide"]), context).save(path)
        paths.append(path)
    return paths


def _compose(ctx: JobContext, render: RenderInput, work: Path) -> _Composition:
    """Slides + narration + presenter bubble -> video, poster and captions files."""
    avatar_path = ctx.state.get("avatar_path")
    ctx.progress(75, "Dibujando las diapositivas")
    slides = _draw_slides(render, work, presenter=bool(avatar_path))
    avatar = _local_copy(work, "presenter.mp4", avatar_path) if avatar_path else None

    ctx.progress(82, "Componiendo el video")
    video, poster = work / "video.mp4", work / "poster.jpg"
    duration = compose.compose_video(slides, _narration_from_state(ctx, work), avatar, video, work)
    poster_size = compose.poster(slides[0], poster)
    vtt = _local_copy(work, "captions.vtt", ctx.state["captions_path"])
    return _Composition(video, poster, poster_size, vtt, duration, presenter=avatar is not None)


def _new_asset(db: Session, render: RenderInput, kind: str, upload: _Upload, **fields) -> MediaAsset:
    asset = MediaAsset(
        kind=kind, status="ready", bucket=get_storage().bucket, path=upload.path, mime_type=upload.mime,
        size_bytes=upload.local.stat().st_size, course_id=render.course_id, **fields,
    )
    db.add(asset)
    return asset


def _publish(ctx: JobContext, render: RenderInput, made: _Composition) -> dict:
    """Store the files, make them the module's video (replacing the previous one) and clean up."""
    ctx.progress(95, "Guardando el video")
    folder = f"{_module_folder(render, ctx)}/video-{ctx.job_id}"
    video = _Upload(f"{folder}/video.mp4", "video/mp4", made.video)
    poster = _Upload(f"{folder}/poster.jpg", "image/jpeg", made.poster)
    vtt = _Upload(f"{folder}/captions.vtt", "text/vtt", made.vtt)
    storage = get_storage()
    for upload in (video, poster, vtt):
        storage.upload_file(upload.path, upload.local, upload.mime)

    seconds = round(made.duration, 2)
    with open_session() as db:
        module = db.get(Module, ctx.module_id)
        if module is None:  # deleted while rendering: nothing will show these files
            _cleanup(ctx, [upload.path for upload in (video, poster, vtt)])
            return {"skipped": "module deleted"}
        replaced = [module.video_asset_id, module.poster_asset_id, module.captions_asset_id]
        video_asset = _new_asset(
            db, render, "video", video, duration_seconds=seconds, width=compose.WIDTH, height=compose.HEIGHT
        )
        poster_asset = _new_asset(db, render, "image", poster, width=made.poster_size[0], height=made.poster_size[1])
        captions_asset = _new_asset(db, render, "captions", vtt, meta={"language": render.language})
        db.flush()
        module.video_asset_id, module.poster_asset_id = video_asset.id, poster_asset.id
        module.captions_asset_id, module.duration_seconds = captions_asset.id, seconds
        module.video_url = ""
        module.generation_status, module.generation_error = "completed", None
        module.storyboard = {
            **(module.storyboard or {}),
            "render": {
                "voice_id": render.voice_id,
                "avatar_id": render.avatar_id if made.presenter else "",
                "warning": ctx.state.get("warning"),
                "duration_seconds": seconds,
            },
        }
        db.commit()
        try:
            discard_assets(db, replaced)  # the previous video, poster and captions, unless still in use
        except Exception:  # the new video is in place; leftovers only cost storage
            db.rollback()
            logger.exception("render_replaced_cleanup_failed", extra={"module_id": ctx.module_id})

    _cleanup(ctx)
    return {"duration_seconds": seconds, "presenter": made.presenter, "warning": ctx.state.get("warning")}


INTERMEDIATE_KEYS = ("master_path", "captions_path", "avatar_path", "narration_mp3_path")


def _cleanup(ctx: JobContext, extra_paths: list[str] | None = None) -> None:
    """The intermediate files the phases handed to each other are no longer needed (best effort)."""
    state = ctx.state
    paths = [scene["path"] for scene in state.get("narration", {}).values()]
    paths += [state[key] for key in INTERMEDIATE_KEYS if state.get(key)]
    try:
        get_storage().delete(paths + (extra_paths or []))
    except StorageError as exc:
        logger.warning("render_cleanup_failed", extra={"module_id": ctx.module_id, "error": str(exc)})


@handler("video.render", on_failure=_render_failed)
def render_module(ctx: JobContext) -> dict | Reschedule:
    render = _load(ctx)
    if render is None:
        return {"skipped": "module deleted"}
    with tempfile.TemporaryDirectory(prefix="render-") as tmp:
        work = Path(tmp)
        if ctx.state.get("phase", "narrate") == "narrate":
            _narrate(ctx, render, work)
            _plan_presenter(ctx, render)
            ctx.progress(48, "Narración lista")
        if ctx.state["phase"] == "presenter":
            waiting = _presenter(ctx, render, work)
            if waiting is not None:
                return waiting
        try:
            made = _compose(ctx, render, work)
        except compose.ComposeError as exc:
            logger.error("compose_failed", extra={"module_id": ctx.module_id, "error": str(exc)[:MAX_LOG_ERROR_CHARS]})
            raise JobError("No pudimos componer el video. Intenta de nuevo; si sigue fallando, revisa el guion.") from exc
        return _publish(ctx, render, made)
