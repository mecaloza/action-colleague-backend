"""
video.render: storyboard -> narrated, captioned, animated MP4 with the AI presenter.

Phases (the state is saved between them, so a restart or a wait resumes where it left off):
  1. narrate   — ElevenLabs speech per scene, with character timings; then the master narration
                 (exact timings) and the captions, all kept in storage.
  2. presenter — HeyGen animates an avatar with that same narration. The job reschedules itself
                 while HeyGen works instead of holding a worker. Any presenter failure only
                 drops the bubble: the video is still produced, with a warning for the admin.
  3. compose   — branded slides revealed point by point as the narration reaches them, with the
                 presenter large on the cover and the closing and in a bubble in between -> MP4,
                 poster and WebVTT; attached to the module, replacing its previous video.

The first run freezes what it renders (scenes, voice, presenter and look) in the state, so a retry
or a wait never mixes two voices or pairs slides with another script's audio. Only the worker that
still holds the job publishes, into a folder of its own, and only while the module is still an AI
module: a video the admin uploads meanwhile wins.

Progress goes 5-48% while narrating, 50-70% with the presenter and 75-95% composing.
"""

import logging
import tempfile
import time
import uuid
from dataclasses import MISSING, asdict, dataclass, fields, replace
from pathlib import Path

import httpx
from PIL import Image, UnidentifiedImageError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import Job, MediaAsset, Module
from app.services import studio
from app.services.course_views import ACTIVE_GENERATION
from app.services.media import SIGNED_URL_SECONDS, discard_assets
from app.services.slides.render import beat_count, is_hero
from app.services.slides.render import render as render_slide
from app.services.slides.spec import Slide, SlideContext
from app.services.storage import StorageError, get_storage
from app.services.video import captions, compose, motion
from app.services.video.avatar import MAX_ASSET_BYTES, AvatarError, AvatarProvider, get_avatar_provider
from app.services.video.visuals import VisualError, VisualRequest, cache_path, get_visuals
from app.services.video.voice import VoiceError, get_voice_provider
from app.worker.runner import JobCancelled, JobContext, JobError, Reschedule, handler, holds_job, open_session

logger = logging.getLogger(__name__)

PRESENTER_POLL_SECONDS = 30
VISUAL_POLL_SECONDS = 20
MAX_VISUAL_WAIT_SECONDS = 15 * 60  # an animated clip that takes longer is left out (the scene keeps its slide)
VISUAL_MIME = {".mp4": "video/mp4", ".png": "image/png"}
MAX_PRESENTER_WAIT_SECONDS = 45 * 60  # at least: a long narration gets PRESENTER_WAIT_FACTOR times its length
PRESENTER_WAIT_FACTOR = 3
MAX_PRESENTER_ERRORS = 5
PRESENTER_BACKGROUND = "#1D1D1D"
MAX_LOG_ERROR_CHARS = 800  # of an FFmpeg error kept in the log line
MEDIA_ERROR = "No pudimos procesar el audio o el video. Intenta de nuevo; si sigue fallando, revisa el guion."
FRAMES_PER_PROGRESS = 100  # animation frames drawn between two progress reports
INTERMEDIATE_KEYS = ("master_path", "captions_path", "avatar_path", "narration_mp3_path")


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
    co_avatar_id: str = ""  # a second presenter takes every other scene, with co_voice_id
    co_voice_id: str = ""
    avatar_engine: str = ""  # "" = the server's HeyGen engine
    animation: str = ""  # "high": every visual is an animated clip


class _Skip(Exception):
    """Nothing to render: the module is gone, has the admin's own video now, or this job already published."""


def _delete_files(paths: list[str], module_id: int | None) -> None:
    """Best effort: a leftover file only costs storage."""
    if not paths:
        return
    try:
        get_storage().delete(paths)
    except StorageError as exc:
        logger.warning("render_cleanup_failed", extra={"module_id": module_id, "error": str(exc)[:MAX_LOG_ERROR_CHARS]})


def intermediate_paths(state: dict | None) -> list[str]:
    """The files a render's phases hand to each other, as its job state records them."""
    state = state or {}
    paths = [scene["path"] for scene in (state.get("narration") or {}).values() if scene.get("path")]
    paths += [state[key] for key in INTERMEDIATE_KEYS if state.get(key)] + list(state.get("take_paths") or [])
    return list(dict.fromkeys(paths))  # each once (a lone presenter's clip is also the video's presenter)


def leftover_paths(state: dict | None) -> list[str]:
    """Everything a render that will not publish any more left in storage: its intermediates, and the copy
    of the finished video a run was uploading when it lost the job (e.g. a deploy stopped its process)."""
    return intermediate_paths(state) + list((state or {}).get("uploading") or [])


def _render_failed(db: Session, job: Job, error: str) -> None:
    """The job gave up: show the failure on the module (unless it has its own video now) and drop its files."""
    module = db.get(Module, job.module_id) if job.module_id else None
    if module and module.source == "ai":
        module.generation_status, module.generation_error = "failed", error
    _delete_files(leftover_paths(job.state), job.module_id)


def _thaw(frozen: dict) -> RenderInput | None:
    """The input the first run froze; None when a new release needs a field it lacks (it is read again).

    A field added later with a default keeps it: a render started before (e.g. narrated with one presenter)
    must not pick up the course's newer choices halfway."""
    all_fields = fields(RenderInput)
    required = {field.name for field in all_fields if field.default is MISSING and field.default_factory is MISSING}
    if not required <= frozen.keys():
        return None
    return RenderInput(**{field.name: frozen[field.name] for field in all_fields if field.name in frozen})


def _read_input(module: Module, choices: dict) -> RenderInput:
    scenes = studio.scenes_of(module)
    if not scenes:
        raise JobError("El módulo no tiene guion; genéralo antes de producir el video.", permanent=True)
    course = module.course
    options = {**(course.settings or {}), **choices}  # what the render was asked with wins
    return RenderInput(
        course_id=course.id,
        course_title=course.title,
        language=course.language or "es",
        module_label=f"Módulo {module.order}",
        scenes=scenes,
        voice_id=options.get("voice_id") or get_settings().elevenlabs_voice_id,
        avatar_id=options.get("avatar_id") or "",
        presenter=bool(options.get("presenter", True)),
        theme=options.get("theme") or "dark",
        co_avatar_id=options.get("co_avatar_id") or "",
        co_voice_id=options.get("co_voice_id") or "",
        avatar_engine=options.get("avatar_engine") or "",
        animation=options.get("animation") or "",
    )


def _load(ctx: JobContext) -> RenderInput:
    """What the render works on, frozen in the state by the first run; marks the module as generating."""
    with open_session() as db:
        module = db.get(Module, ctx.module_id)
        if module is None:
            raise _Skip("module deleted")
        if ((module.storyboard or {}).get("render") or {}).get("job_id") == ctx.job_id:
            if module.generation_status in ACTIVE_GENERATION:  # e.g. asked again while this job was handed back
                module.generation_status = "completed"
                db.commit()
            raise _Skip("already published")  # run again after its result was saved
        if module.source != "ai":
            raise _Skip("module has its own video")
        frozen = ctx.state.get("input")
        render = (_thaw(frozen) if frozen else None) or _read_input(module, ctx.payload)
        module.generation_status, module.generation_error = "generating", None
        db.commit()
    ctx.state["input"] = asdict(render)
    return render


def _module_folder(render: RenderInput, ctx: JobContext) -> str:
    return f"courses/{render.course_id}/modules/{ctx.module_id}"


def _intermediate_prefix(render: RenderInput, ctx: JobContext) -> str:
    """Storage folder of the files the phases hand to each other; they are deleted at the end."""
    return f"{_module_folder(render, ctx)}/render/{ctx.job_id}"


def _local_copy(work: Path, name: str, storage_path: str) -> Path:
    """`name` inside the work folder: downloaded from storage unless this run already has it."""
    local = work / name
    if not local.exists():
        get_storage().download_file(storage_path, local)
    return local


# ── 1. Narration ──────────────────────────────────────────────────────


def scene_speakers(render: RenderInput) -> list[int]:
    """Who presents each scene: 0, or with a second presenter 0 and 1 taking turns (the first opens)."""
    two = render.presenter and bool(render.avatar_id and render.co_avatar_id)
    return [index % 2 if two else 0 for index in range(len(render.scenes))]


def _scene_voices(render: RenderInput) -> list[str]:
    """Each scene's voice: the presenter's own (the second presenter's, or the first's if it has none)."""
    voices = (render.voice_id, render.co_voice_id or render.voice_id)
    return [voices[speaker] for speaker in scene_speakers(render)]


def _scene_file(index: int) -> str:
    return f"scene_{index:02d}.mp3"


def _speak_scenes(ctx: JobContext, render: RenderInput, work: Path) -> None:
    """Narrate the scenes not done yet; the state remembers the finished ones across restarts."""
    storage, voice = get_storage(), get_voice_provider()
    done = ctx.state.setdefault("narration", {})
    narrations = [scene["narration"] for scene in render.scenes]
    voices = _scene_voices(render)

    def same_voice(other: int) -> str:  # continuity hints only make sense within one voice
        return narrations[other] if 0 <= other < len(narrations) and voices[other] == voices[index] else ""

    for index, text in enumerate(narrations):
        if str(index) in done:
            continue
        ctx.progress(5 + int(40 * index / len(narrations)), f"Narrando la escena {index + 1} de {len(narrations)}")
        try:
            speech = voice.speak(text, voices[index], previous_text=same_voice(index - 1), next_text=same_voice(index + 1))
        except VoiceError as exc:
            raise JobError(str(exc), permanent=exc.permanent) from exc
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


def _beats(scene: dict, words: list[captions.TimedWord], start: float, length: float) -> list[float]:
    """When each part of the scene's slide appears (see `motion.beat_times`)."""
    return motion.beat_times(Slide.model_validate(scene["slide"]), words, start, length)


def _narrate(ctx: JobContext, render: RenderInput, work: Path) -> None:
    _speak_scenes(ctx, render, work)

    ctx.progress(47, "Uniendo la narración")
    done = ctx.state["narration"]
    scene_audio = [
        _local_copy(work, _scene_file(index), done[str(index)]["path"]) for index in range(len(render.scenes))
    ]
    narration = compose.build_narration(scene_audio, work)

    words, beats = [], []
    for index, scene in enumerate(render.scenes):
        alignment = done[str(index)]["alignment"]
        scene_words = _caption_words(scene, alignment, narration.starts[index], narration.lengths[index])
        words += scene_words
        beats.append(_beats(scene, scene_words, narration.starts[index], narration.lengths[index]))
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
        beats=beats,
    )
    for scene in done.values():
        scene.pop("alignment", None)  # only the captions needed it: keep the saved state small


def _narration_from_state(ctx: JobContext, work: Path) -> compose.Narration:
    """The master narration (downloaded unless this run made it) with the timings saved in the state."""
    master = _local_copy(work, "master.flac", ctx.state["master_path"])
    return compose.Narration(master, ctx.state["starts"], ctx.state["lengths"], ctx.state["total"])


# ── 2. Visuals ──────────────────────────────────────────────────────


FALLBACKS = {"clip": ("clip", "image", "stock"), "image": ("image", "stock"), "stock": ("stock", "image")}
MAX_VISUAL_RETRIES = 6  # a busy provider (rate limit) is asked again this many times, then the next kind is tried


def _visual_options(render: RenderInput) -> dict[int, list[VisualRequest]]:
    """For each scene with a visual, what to try in order: its kind, then cheaper ones this server offers
    (a clip falls back to an image, then to stock; an image to stock). Clips are capped per module here, where
    they are paid for, whatever the script asks."""
    settings = get_settings()
    clips_left = settings.max_clips_high if render.animation == "high" else settings.max_clips_per_module
    available = get_visuals().available()
    options = {}
    for index, scene in enumerate(render.scenes):
        visual = scene.get("visual") or {}
        kind, query, prompt = visual.get("kind") or "none", visual.get("query") or "", visual.get("prompt") or ""
        if render.animation == "high" and kind != "none":  # the course pays for animation everywhere
            kind = "clip"
        if not prompt.strip() and query.strip():  # keywords alone make a poor prompt: add what the slide says
            title = ((scene.get("slide") or {}).get("title") or "").strip()
            prompt = f"{title}. {query}" if title else query
        chain = [kind for kind in FALLBACKS.get(kind, ()) if kind != "clip" or clips_left > 0]
        tries = [
            VisualRequest(candidate, query, prompt)
            for candidate in chain
            if candidate in available and (query if candidate == "stock" else prompt).strip()
        ]
        if tries:
            options[index] = tries
            clips_left -= tries[0].kind == "clip"
    return options


def _visual_requests(render: RenderInput) -> dict[int, VisualRequest]:
    """The first choice of every scene with a visual."""
    return {index: tries[0] for index, tries in _visual_options(render).items()}


def _cached_visual(render: RenderInput, request: VisualRequest) -> str | None:
    """A file a previous render already got for the same request (stock may have been a photo)."""
    storage = get_storage()
    base = cache_path(render.course_id, request).rsplit(".", 1)[0]
    for extension in (request.extension, "png" if request.extension == "mp4" else "mp4"):
        path = f"{base}.{extension}"
        if storage.size(path):
            return path
    return None


def _fetch(ctx: JobContext, request: VisualRequest, entry: dict, local_name: Path) -> Path | None:
    """One try at a visual: the file, or None while an animated clip is still being made."""
    provider = get_visuals()
    prompt = request.prompt or request.query
    if request.kind == "clip":
        if not entry.get("operation"):
            entry.update(operation=provider.start_clip(prompt), started_at=time.time())
            ctx.progress(49, "Animando los conceptos clave")  # saved: a retry follows the same clip
            return None
        status = provider.clip_status(entry["operation"])
        if not status.done:
            if time.time() - entry["started_at"] > MAX_VISUAL_WAIT_SECONDS:
                raise VisualError("El clip animado tardó demasiado.")
            return None
        if status.error or not status.uri:
            raise VisualError(status.error or "Veo no devolvió el clip.")
        return provider.download_clip(status.uri, local_name)
    if request.kind == "image":
        return provider.image(prompt, local_name)
    return provider.stock(request.query, local_name)


def _check_visual(path: Path) -> None:
    """A downloaded visual FFmpeg can use: a picture that opens, or a video with a length (else VisualError)."""
    if path.suffix == ".png":
        try:
            with Image.open(path) as image:
                image.verify()
        except (UnidentifiedImageError, OSError) as exc:
            raise VisualError("La imagen no se pudo leer.") from exc
    elif not compose.clip_seconds(path) > 0:
        raise VisualError("El video no se pudo leer.")


def _get_visual(
    ctx: JobContext, render: RenderInput, work: Path, index: int, tries: list[VisualRequest]
) -> bool:
    """Advance one scene's visual: True when settled (in storage, or none could be had), False while waiting."""
    entry = ctx.state["visuals"].setdefault(str(index), {})
    while not entry.get("path") and entry.get("try", 0) < len(tries):
        request = tries[entry.get("try", 0)]
        cached = _cached_visual(render, request)
        if cached:
            entry["path"] = cached
            break
        if entry.get("retry_at", 0) > time.time():
            return False
        try:
            local = _fetch(ctx, request, entry, work / f"visual_{index:02d}")
            if local is not None:
                _check_visual(local)
        except VisualError as exc:
            retries = entry.get("retries", 0) + 1
            if exc.retryable and retries <= MAX_VISUAL_RETRIES:
                entry.update(retries=retries, retry_at=time.time() + VISUAL_POLL_SECONDS * retries)
                return False
            logger.warning("visual_skipped", extra={"module_id": ctx.module_id, "scene": index, "kind": request.kind,
                                                    "reason": str(exc)[:200]})
            for key in ("operation", "started_at", "retries", "retry_at"):
                entry.pop(key, None)
            entry["try"] = entry.get("try", 0) + 1  # the next, cheaper kind
            continue
        if local is None:
            return False
        path = f"{cache_path(render.course_id, request).rsplit('.', 1)[0]}{local.suffix}"
        get_storage().upload_file(path, local, VISUAL_MIME[local.suffix])
        entry["path"] = path
    return True


def _visuals(ctx: JobContext, render: RenderInput, work: Path) -> Reschedule | None:
    """Get every scene's visual. Returns Reschedule while a clip is being made or a busy provider must wait."""
    ctx.state.setdefault("visuals", {})
    done = [_get_visual(ctx, render, work, index, tries) for index, tries in _visual_options(render).items()]
    if not all(done):
        ctx.progress(49, "Animando los conceptos clave")
        return Reschedule(VISUAL_POLL_SECONDS)
    return None


# ── 3. Presenter ──────────────────────────────────────────────────────


def _drop_presenter(ctx: JobContext, reason: str) -> None:
    """Go on to the composition without the presenter bubble, telling the admin why."""
    ctx.state["warning"] = f"{reason} El video se produjo sin presentador."
    ctx.state["phase"] = "compose"


def _skip_presenter(ctx: JobContext, reason: str, take: dict | None = None) -> None:
    """A presenter failed: log it and drop the presenter. With two, either one failing drops both (each clip
    only has its own scenes, so the other can't present the whole video)."""
    logger.warning("presenter_skipped", extra={"module_id": ctx.module_id, "reason": reason,
                                               "take": take["key"] if take else None})
    _drop_presenter(ctx, reason)


def _who(take: dict) -> str:
    return "el segundo presentador" if take["key"] == "heygen_co" else "el presentador"


def _plan_presenter(ctx: JobContext, render: RenderInput) -> None:
    """After the narration: the presenter phase runs only if one was chosen and can be rendered."""
    configured = get_avatar_provider() is not None
    if render.presenter and render.avatar_id and configured:
        ctx.state["phase"] = "presenter"
    elif render.presenter:
        _drop_presenter(ctx, "No elegiste presentador." if configured else "No hay un presentador configurado.")
    else:
        ctx.state["phase"] = "compose"


def _segments(ctx: JobContext) -> list[tuple[float, float]]:
    """Each scene's stretch of the video, end to end: from its transition to the next one's (seconds)."""
    starts, total = ctx.state["starts"], ctx.state["total"]
    bounds = [0.0, *(max(0.0, start - motion.TRANSITION_SECONDS) for start in starts[1:]), total]
    return list(zip(bounds[:-1], bounds[1:]))


def _takes(render: RenderInput) -> list[dict]:
    """One HeyGen video per presenter, with the scenes it presents ("key" is its record in the state)."""
    speakers = scene_speakers(render)
    takes = [{"key": "heygen", "avatar_id": render.avatar_id, "scenes": [i for i, s in enumerate(speakers) if s == 0]}]
    if 1 in speakers:
        takes.append({"key": "heygen_co", "avatar_id": render.co_avatar_id,
                      "scenes": [i for i, s in enumerate(speakers) if s == 1]})
    return takes


def _hand_over_narration(
    ctx: JobContext, render: RenderInput, work: Path, provider: AvatarProvider, take: dict
) -> dict:
    """The presenter's narration as HeyGen takes it (only its scenes when it shares the video), handed over
    once and saved: a retry sends the very same request."""
    heygen = ctx.state[take["key"]]
    if heygen.get("audio_asset_id") or heygen.get("audio_url"):
        return heygen
    name = f"{take['key']}.mp3"
    if len(take["scenes"]) == len(render.scenes):
        audio = compose.to_mp3(_narration_from_state(ctx, work).audio, work / name)
    else:
        segments = _segments(ctx)
        audio = compose.cut_audio(
            _narration_from_state(ctx, work).audio, [segments[index] for index in take["scenes"]], work / name
        )
    if audio.stat().st_size > MAX_ASSET_BYTES:  # too big to upload to HeyGen: it downloads it from a link
        storage = get_storage()
        path = f"{_intermediate_prefix(render, ctx)}/{name}"
        storage.upload_file(path, audio, "audio/mpeg")
        ctx.state.setdefault("take_paths", []).append(path)  # an intermediate too: deleted with the others
        heygen["audio_url"] = storage.signed_urls([path], SIGNED_URL_SECONDS).get(path)
    else:
        heygen["audio_asset_id"] = provider.upload_audio(audio)
    ctx.progress(52, "Preparando al presentador")  # saved before asking for the video
    return heygen


def _start_take(ctx: JobContext, render: RenderInput, work: Path, provider: AvatarProvider, take: dict) -> None:
    """Send the presenter's narration to HeyGen and start its video."""
    heygen = _hand_over_narration(ctx, render, work, provider, take)
    suffix = "" if take["key"] == "heygen" else "-co"
    heygen["video_id"] = provider.start(
        take["avatar_id"],
        PRESENTER_BACKGROUND,
        idempotency_key=f"ac-presenter-{ctx.job_id}{suffix}",  # this job's one video per presenter
        audio_asset_id=heygen.get("audio_asset_id"),
        audio_url=heygen.get("audio_url"),
        engine=render.avatar_engine or None,
    )
    heygen["started_at"] = time.time()
    ctx.progress(55, "El presentador se está grabando")


def _poll_take(ctx: JobContext, render: RenderInput, work: Path, provider: AvatarProvider, take: dict) -> bool | None:
    """Check on one presenter's video: True when downloaded, False while HeyGen works, None if it gave up."""
    heygen = ctx.state[take["key"]]
    status = provider.status(heygen["video_id"])
    heygen["errors"] = 0  # HeyGen answered: only errors in a row give up on the presenter
    if status.status == "failed":
        _skip_presenter(ctx, f"HeyGen no pudo generar {_who(take)} ({status.error or 'sin detalle'}).", take)
        return None
    if status.status != "completed" or not status.video_url:
        patience = max(MAX_PRESENTER_WAIT_SECONDS, PRESENTER_WAIT_FACTOR * ctx.state.get("total", 0))
        if time.time() - heygen["started_at"] > patience:
            _skip_presenter(ctx, f"{_who(take).capitalize()} tardó demasiado en generarse.", take)
            return None
        return False
    local = work / f"{take['key']}.mp4"
    provider.download(status.video_url, local)
    path = f"{_intermediate_prefix(render, ctx)}/{take['key']}.mp4"
    get_storage().upload_file(path, local, "video/mp4")
    heygen["path"] = path
    ctx.state.setdefault("take_paths", []).append(path)
    return True


def _assemble_presenters(ctx: JobContext, render: RenderInput, work: Path, takes: list[dict]) -> str:
    """The presenter's track for the whole video: the one clip, or both presenters' clips scene by scene."""
    if len(takes) == 1:
        return ctx.state[takes[0]["key"]]["path"]
    clips = [_local_copy(work, f"{take['key']}.mp4", ctx.state[take["key"]]["path"]) for take in takes]
    track = compose.presenter_track(clips, scene_speakers(render), _segments(ctx), work / "presenter.mp4")
    path = f"{_intermediate_prefix(render, ctx)}/presenter.mp4"
    get_storage().upload_file(path, track, "video/mp4")
    return path


def _presenter(ctx: JobContext, render: RenderInput, work: Path) -> Reschedule | None:
    """Advance the HeyGen renders one step. Returns Reschedule while they are still working."""
    provider = get_avatar_provider()
    takes = _takes(render)
    try:
        waiting = False
        for take in takes:
            heygen = ctx.state.setdefault(take["key"], {})
            if heygen.get("path"):
                continue
            if not heygen.get("video_id"):
                _start_take(ctx, render, work, provider, take)
                waiting = True
                continue
            done = _poll_take(ctx, render, work, provider, take)
            if done is None:
                return None  # the presenter was dropped
            waiting = waiting or not done
        if waiting:
            ctx.progress(60, "El presentador se está grabando")
            return Reschedule(PRESENTER_POLL_SECONDS)
        ctx.progress(70, "Descargando al presentador")
        ctx.state.update(avatar_path=_assemble_presenters(ctx, render, work, takes), phase="compose")
        return None
    except AvatarError as exc:
        errors = max(ctx.state.get(take["key"], {}).get("errors", 0) for take in takes) + 1
        for take in takes:
            ctx.state.setdefault(take["key"], {})["errors"] = errors
        if exc.retryable and errors < MAX_PRESENTER_ERRORS:
            return Reschedule(PRESENTER_POLL_SECONDS * errors)
        _skip_presenter(ctx, str(exc))
        return None
    except (JobCancelled, StorageError, SQLAlchemyError, httpx.HTTPError):
        raise  # our lease, database or storage failed: the whole render retries and keeps the presenter
    except Exception:  # anything else about the presenter must not cost the video
        logger.exception("presenter_failed", extra={"module_id": ctx.module_id})
        _skip_presenter(ctx, "No pudimos preparar al presentador.")
        return None


# ── 4. Composition ────────────────────────────────────────────────────


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


def _hero_scenes(render: RenderInput, presenter: bool) -> list[bool]:
    """The scenes where the presenter is large: the opening cover and the closing slide."""
    total = len(render.scenes)
    return [
        is_hero(Slide.model_validate(scene["slide"]), index + 1, total, presenter)
        for index, scene in enumerate(render.scenes)
    ]


def _draw_scenes(
    ctx: JobContext, render: RenderInput, work: Path, presenter: bool, visuals: bool = True
) -> list[motion.Scene]:
    """Each scene's slide drawn beat by beat; with a presenter, the layouts leave its place free."""
    narration_starts, lengths = ctx.state["starts"], ctx.state["lengths"]
    saved_beats = ctx.state.get("beats")  # a render narrated before beats existed spreads them evenly
    hero = _hero_scenes(render, presenter)
    backdrops = _backdrops(ctx, render, work) if visuals else {}
    mode = "RGBA" if backdrops else "RGB"  # one pixel format for every image of the video (see motion.image_mode)
    scenes = []
    for index, scene in enumerate(render.scenes):
        slide = Slide.model_validate(scene["slide"])
        context = SlideContext(
            course_title=render.course_title, module_label=render.module_label, index=index + 1,
            total=len(render.scenes), theme=render.theme, presenter=presenter and not hero[index], hero=hero[index],
            backdrop=index in backdrops,
        )
        images = []
        for shown in range(1, beat_count(slide) + 1):
            path = work / f"slide_{index:02d}_{shown:02d}.png"
            render_slide(slide, replace(context, reveal=shown)).convert(mode).save(path)
            images.append(path)
        start = narration_starts[index]
        times = saved_beats[index] if saved_beats else _beats(scene, [], start, lengths[index])
        # Into or out of a backdrop the slides fade, in step with the backdrops' cross-fade (a push would slide
        # the text away from the picture it belongs to).
        plain = index not in backdrops and index - 1 not in backdrops
        scenes.append(motion.Scene(
            images, start, times[: len(images) - 1], push=plain and index not in (0, len(render.scenes) - 1),
            backdrop=backdrops.get(index),
        ))
    return scenes


def _backdrops(ctx: JobContext, render: RenderInput, work: Path) -> dict[int, Path]:
    """The scenes' visuals the visuals phase got, as local files (none for a render from before visuals)."""
    found = {}
    for key, entry in (ctx.state.get("visuals") or {}).items():
        if entry.get("path"):
            found[int(key)] = _local_copy(work, f"backdrop_{int(key):02d}{Path(entry['path']).suffix}", entry["path"])
    return found


def _compose(
    ctx: JobContext, render: RenderInput, work: Path, with_presenter: bool = True, with_visuals: bool = True
) -> _Composition:
    """Animated slides + narration + presenter -> video, poster and captions files."""
    avatar_path = ctx.state.get("avatar_path") if with_presenter else None
    ctx.progress(75, "Dibujando las diapositivas")
    scenes = _draw_scenes(ctx, render, work, presenter=bool(avatar_path), visuals=with_visuals)
    avatar = _local_copy(work, "presenter.mp4", avatar_path) if avatar_path else None

    ctx.progress(82, "Componiendo el video")
    video, poster = work / "video.mp4", work / "poster.jpg"
    hero = _hero_scenes(render, avatar is not None)
    frames = 0

    def on_frame() -> None:  # drawing the animation takes a while: keep reporting (a cancel is seen there)
        nonlocal frames
        frames += 1
        if frames % FRAMES_PER_PROGRESS == 0:
            ctx.progress(82, "Componiendo el video")

    duration = compose.compose_video(
        scenes, _narration_from_state(ctx, work), avatar, video, work, hero=hero, on_frame=on_frame
    )
    # The poster has no presenter: the first slide as it is drawn without one.
    first = render.scenes[0]
    still = work / "poster_slide.png"
    render_slide(Slide.model_validate(first["slide"]), SlideContext(
        course_title=render.course_title, module_label=render.module_label, index=1, total=len(render.scenes),
        theme=render.theme,
    )).save(still)
    poster_size = compose.poster(still, poster)
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
    # A folder per run: a worker that lost the job only ever deletes its own copies.
    folder = f"{_module_folder(render, ctx)}/video-{ctx.job_id}-{uuid.uuid4().hex[:8]}"
    video = _Upload(f"{folder}/video.mp4", "video/mp4", made.video)
    poster = _Upload(f"{folder}/poster.jpg", "image/jpeg", made.poster)
    vtt = _Upload(f"{folder}/captions.vtt", "text/vtt", made.vtt)
    uploads = (video, poster, vtt)
    published = [upload.path for upload in uploads]
    # Saved before uploading: if this process dies meanwhile, whoever comes next deletes the copy.
    abandoned = ctx.state.get("uploading") or []  # an earlier run's copy: that run never published it
    ctx.state["uploading"] = published
    ctx.progress(95, "Guardando el video")
    _delete_files(abandoned, ctx.module_id)
    storage = get_storage()
    for upload in uploads:
        storage.upload_file(upload.path, upload.local, upload.mime)

    seconds = round(made.duration, 2)
    with open_session() as db:
        if not holds_job(db, ctx):  # handed back or reaped while uploading: its next owner publishes
            db.rollback()
            _delete_files(published, ctx.module_id)
            raise JobCancelled(ctx.job_id)
        module = db.get(Module, ctx.module_id, with_for_update=True)
        if module is None or module.source != "ai":  # deleted, or the admin gave it their own video meanwhile
            db.rollback()
            _delete_files(published + intermediate_paths(ctx.state), ctx.module_id)
            return {"skipped": "module deleted" if module is None else "module has its own video"}
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
                "job_id": ctx.job_id,  # a run of this job after the commit knows it has nothing left to do
                "voice_id": render.voice_id,
                "avatar_id": render.avatar_id if made.presenter else "",
                "warning": ctx.state.get("warning"),
                "duration_seconds": seconds,
            },
        }
        db.commit()
        ctx.state.pop("uploading", None)  # the module's video now
        try:
            discard_assets(db, replaced)  # the previous video, poster and captions, unless still in use
        except Exception:  # the new video is in place; leftovers only cost storage
            db.rollback()
            logger.exception("render_replaced_cleanup_failed", extra={"module_id": ctx.module_id})

    _delete_files(intermediate_paths(ctx.state), ctx.module_id)
    return {"duration_seconds": seconds, "presenter": made.presenter, "warning": ctx.state.get("warning")}


@handler("video.render", on_failure=_render_failed)
def render_module(ctx: JobContext) -> dict | Reschedule:
    try:
        render = _load(ctx)
    except _Skip as skip:
        # What an earlier run left, if anything; after publishing, "uploading" is the module's video itself.
        published = str(skip) == "already published"
        _delete_files(intermediate_paths(ctx.state) if published else leftover_paths(ctx.state), ctx.module_id)
        return {"skipped": str(skip)}
    with tempfile.TemporaryDirectory(prefix="render-") as tmp:
        work = Path(tmp)
        try:
            if ctx.state.get("phase", "narrate") == "narrate":
                _narrate(ctx, render, work)
                ctx.state["phase"] = "visuals"
                ctx.progress(48, "Narración lista")
            if ctx.state["phase"] == "visuals":
                waiting = _visuals(ctx, render, work)
                if waiting is not None:
                    return waiting
                _plan_presenter(ctx, render)
                ctx.progress(50, "Visuales listos")
            if ctx.state["phase"] == "presenter":
                waiting = _presenter(ctx, render, work)
                if waiting is not None:
                    return waiting
            made = _compose_video(ctx, render, work)
        except compose.ComposeError as exc:
            logger.error("compose_failed", extra={"module_id": ctx.module_id, "error": str(exc)[:MAX_LOG_ERROR_CHARS]})
            raise JobError(MEDIA_ERROR) from exc
        return _publish(ctx, render, made)


def _compose_video(ctx: JobContext, render: RenderInput, work: Path) -> _Composition:
    """The video with its presenter and visuals; if FFmpeg can't use the visuals, then the presenter's clip,
    the same video without them (with a warning for the admin)."""
    has_visuals = any(entry.get("path") for entry in (ctx.state.get("visuals") or {}).values())
    attempts = [(True, True)]
    if has_visuals:
        attempts.append((True, False))
    if ctx.state.get("avatar_path"):
        attempts.append((False, False))
    for number, (presenter, visuals) in enumerate(attempts):
        try:
            return _compose(ctx, render, work, with_presenter=presenter, with_visuals=visuals)
        except compose.ComposeError as exc:
            if number == len(attempts) - 1:
                raise
            logger.warning("compose_fallback", extra={"module_id": ctx.module_id, "presenter": presenter,
                                                      "visuals": visuals, "error": str(exc)[:MAX_LOG_ERROR_CHARS]})
            next_presenter, next_visuals = attempts[number + 1]
            ctx.state["warning"] = (
                "No pudimos usar el video del presentador. El video se produjo sin presentador."
                if not next_presenter
                else "No pudimos usar las imágenes de fondo. El video se produjo con el diseño de marca."
            )
    raise AssertionError("unreachable")
