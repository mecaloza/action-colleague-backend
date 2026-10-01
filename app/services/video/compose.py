"""
Video composition with FFmpeg: animated slide timeline + narration + optional presenter.

Timing model (all from decoded sample counts, never from metadata):
  narration i starts at s_i = LEAD_IN + sum(a_j + GAP for j < i)
  slide i arrives during [s_i - TRANSITION, s_i] and reveals its beats as the narration reaches them
  (see `motion`); the frames are one concat input, so memory stays flat however long the video.
The presenter is a round bubble in the corner, and large on the right during the cover and the closing.
"""

import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw

from app.services.slides.render import BUBBLE_MARGIN, HERO_BOX, HERO_SIZE, HEIGHT, WIDTH
from app.services.slides.render import BUBBLE_SIZE as BUBBLE  # the bottom-right corner the slides leave free
from app.services.video import motion

FPS = 30
SAMPLE_RATE = 48000
CHANNELS, SAMPLE_WIDTH = 1, 2  # the narration is mono, 16-bit (2 bytes per sample)
LEAD_IN, GAP, TAIL = 0.4, 0.7, 1.0
RING = 6  # thickness of the presenter bubble's ring, in pixels
SUPERSAMPLE = 4  # the bubble's circles are drawn this many times larger, then shrunk to smooth their edge
PRESENTER_HOLD_SECONDS = 5  # a presenter clip shorter than the narration freezes on its last frame this long
ACCENT = (255, 76, 1)
FFMPEG = ("ffmpeg", "-nostdin", "-y", "-loglevel", "error")
LOUDNORM = "loudnorm=I=-16:TP=-1.5:LRA=11"  # loudness target of every narration and recording
FFMPEG_TIMEOUT_SECONDS = 3600
ERROR_TAIL_BYTES = 800  # of FFmpeg's stderr kept in the error message


class ComposeError(RuntimeError):
    """FFmpeg failed, or the pieces don't fit together; the job logs it and shows the admin its own message."""


class ComposeTimeout(ComposeError):
    """FFmpeg did not finish in time: trying again with the same input won't help."""


@dataclass
class Narration:
    """The master narration and where each scene's speech starts in it (seconds)."""

    audio: Path
    starts: list[float]
    lengths: list[float]
    total: float


def run(cmd: list[str], timeout: int = FFMPEG_TIMEOUT_SECONDS) -> None:
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=timeout)
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or b"")[-ERROR_TAIL_BYTES:].decode(errors="replace")
        raise ComposeError(f"FFmpeg falló: {stderr}") from exc
    except subprocess.TimeoutExpired as exc:  # subprocess already killed it
        raise ComposeTimeout(f"FFmpeg no terminó en {timeout} s") from exc


def to_wav(src: Path, dest: Path) -> None:
    run([*FFMPEG, "-i", str(src), "-ac", str(CHANNELS), "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le", str(dest)])


def _silence(seconds: float) -> bytes:
    return b"\x00" * SAMPLE_WIDTH * round(seconds * SAMPLE_RATE)


def _read_frames(path: Path) -> bytes:
    """The samples of a scene's WAV, which `to_wav` must have left in the master's format."""
    with wave.open(str(path), "rb") as wav:
        if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) != (SAMPLE_RATE, CHANNELS, SAMPLE_WIDTH):
            raise ComposeError("La narración no quedó en el formato esperado")
        return wav.readframes(wav.getnframes())


def _concatenate(scene_wavs: list[Path], dest: Path) -> tuple[list[float], list[float], float]:
    """Scene narrations joined with silences at sample level. Returns (starts, lengths, total)."""
    starts, lengths = [], []
    cursor = LEAD_IN
    with wave.open(str(dest), "wb") as out:
        out.setnchannels(CHANNELS)
        out.setsampwidth(SAMPLE_WIDTH)
        out.setframerate(SAMPLE_RATE)
        out.writeframes(_silence(LEAD_IN))
        for index, path in enumerate(scene_wavs):
            frames = _read_frames(path)
            length = len(frames) / SAMPLE_WIDTH / SAMPLE_RATE
            starts.append(cursor)
            lengths.append(length)
            out.writeframes(frames)
            pad = GAP if index < len(scene_wavs) - 1 else TAIL
            out.writeframes(_silence(pad))
            cursor += length + pad
    return starts, lengths, cursor


def build_narration(scene_audio: list[Path], work: Path) -> Narration:
    """Master narration (FLAC, lossless, loudness-normalized) with exact scene timings."""
    wavs = []
    for index, audio in enumerate(scene_audio):
        wav = work / f"scene_{index:02d}.wav"
        to_wav(audio, wav)
        wavs.append(wav)
    master = work / "master.wav"
    starts, lengths, total = _concatenate(wavs, master)
    normalized = work / "master.flac"
    run([*FFMPEG, "-i", str(master), "-af", LOUDNORM, "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS),
         "-c:a", "flac", str(normalized)])
    return Narration(normalized, starts, lengths, total)


def to_mp3(src: Path, dest: Path) -> Path:
    run([*FFMPEG, "-i", str(src), "-c:a", "libmp3lame", "-b:a", "128k", str(dest)])
    return dest


def _circle(dest: Path, mode: str, background: int | tuple[int, ...], diameter: int = BUBBLE, **style) -> Path:
    """A square image filled by a circle, drawn SUPERSAMPLE times larger and shrunk (anti-aliased)."""
    size = diameter * SUPERSAMPLE
    image = Image.new(mode, (size, size), background)
    ImageDraw.Draw(image).ellipse([0, 0, size - 1, size - 1], **style)
    image.resize((diameter, diameter), Image.LANCZOS).save(dest)
    return dest


def _bubble_assets(work: Path, diameter: int = BUBBLE, ring: int = RING) -> tuple[Path, Path]:
    """The presenter bubble's circular alpha mask and its orange ring."""
    mask = _circle(work / f"bubble_mask_{diameter}.png", "L", 0, diameter, fill=255)
    outline = _circle(work / f"bubble_ring_{diameter}.png", "RGBA", (0, 0, 0, 0), diameter,
                      outline=ACCENT + (255,), width=ring * SUPERSAMPLE)
    return mask, outline


def _enable(intervals: list[tuple[float, float]]) -> str:
    """An FFmpeg `enable` expression true during any of the intervals (seconds)."""
    return "+".join(f"between(t,{start:.3f},{end:.3f})" for start, end in intervals) or "0"


def hero_intervals(scenes: list[motion.Scene], hero: list[bool], total: float) -> list[tuple[float, float]]:
    """When the presenter is large: only while its scene is fully on screen (the transitions show the bubble,
    so the large presenter never covers the next slide sliding in)."""
    ends = [scene.start - motion.TRANSITION_SECONDS for scene in scenes[1:]] + [total]
    intervals: list[tuple[float, float]] = []
    for index, is_hero in enumerate(hero):
        if not is_hero:
            continue
        start = 0.0 if index == 0 else scenes[index].start
        if intervals and abs(intervals[-1][1] - start) < 1e-6:
            intervals[-1] = (intervals[-1][0], ends[index])
        else:
            intervals.append((start, ends[index]))
    return intervals


def _bubble_inputs(avatar: Path, mask: Path, ring: Path, duration: float) -> list[str]:
    """FFmpeg inputs of the presenter clip, then its still mask and ring (see `_bubble_filters`)."""
    still = ["-loop", "1", "-t", f"{duration:.3f}"]
    return ["-i", str(avatar), *still, "-i", str(mask), *still, "-i", str(ring)]


def _bubble_filters(background: str, first_input: int, duration: float, keep_timing: bool = False) -> list[str]:
    """Overlay the presenter as a round bubble, with its ring, on the `background` stream.

    Inputs `first_input`, `first_input + 1` and `first_input + 2` are the presenter, the mask and the ring.
    `keep_timing`: the clip's own audio is used too (a camera recording), so its video keeps its start
    time instead of being moved to zero (a video that starts after its audio would lose the lip sync).
    """
    avatar, mask, ring = first_input, first_input + 1, first_input + 2
    timing = f"fps={FPS}:start_time=0" if keep_timing else f"setpts=PTS-STARTPTS,fps={FPS}"
    return [
        f"[{avatar}:v]{timing},crop='min(iw,ih)':'min(iw,ih)'[avsquare]",
        *_circle_overlay(background, "avsquare", mask, ring, BUBBLE, _bubble_xy(), duration, "withbubble"),
        "[withbubble]format=yuv420p[vout]",
    ]


def _bubble_xy() -> tuple[int, int]:
    return WIDTH - BUBBLE - BUBBLE_MARGIN, HEIGHT - BUBBLE - BUBBLE_MARGIN


def _circle_overlay(
    background: str, square: str, mask: int, ring: int, size: int, xy: tuple[int, int], duration: float, out: str,
    enable: str | None = None,
) -> list[str]:
    """The `square` presenter stream as a ringed circle of `size` at `xy` over `background` (only while `enable`)."""
    x, y = xy
    when = f":enable='{enable}'" if enable else ""
    tag = f"{out}_"
    return [
        # Scaled from a centered square: never stretched. The last frame holds if the clip is short.
        f"[{square}]scale={size}:{size},tpad=stop_mode=clone:stop_duration={PRESENTER_HOLD_SECONDS},"
        f"trim=duration={duration:.3f},format=rgba[{tag}av]",
        f"[{mask}:v]fps={FPS},format=gray[{tag}mask]",
        f"[{tag}av][{tag}mask]alphamerge[{tag}circle]",
        f"[{background}][{tag}circle]overlay=x={x}:y={y}:eof_action=pass{when}[{tag}with]",
        f"[{ring}:v]fps={FPS},format=rgba[{tag}ring]",
        f"[{tag}with][{tag}ring]overlay=x={x}:y={y}:eof_action=pass{when}[{out}]",
    ]


def _encode_options(duration: float) -> list[str]:
    """H.264/AAC output options, cutting the file at exactly `duration`."""
    return [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p", "-r", str(FPS),
        "-c:a", "aac", "-b:a", "160k", "-ar", str(SAMPLE_RATE),
        "-t", f"{duration:.3f}", "-movflags", "+faststart", "-threads", "2",
    ]


def compose_video(
    scenes: list[motion.Scene | Path], narration: Narration, avatar: Path | None, output: Path, work: Path,
    hero: list[bool] | None = None, on_frame=None,
) -> float:
    """1920x1080 H.264/AAC MP4 (+faststart). Returns its duration in seconds.

    `scenes`: each scene's images by beat (a plain image is a scene without beats). `hero`: the scenes where the
    presenter is shown large instead of in the bubble (their slides keep that side free).
    """
    if len(scenes) != len(narration.starts):
        raise ComposeError("Cada escena necesita su diapositiva y su narración")
    total = narration.total
    timed = [
        scene if isinstance(scene, motion.Scene) else motion.Scene([scene], start, [])
        for scene, start in zip(scenes, narration.starts)
    ]
    frames = work / "frames"
    frames.mkdir(exist_ok=True)
    listing = motion.write_concat(motion.timeline(timed, total, frames, on_frame), work / "slides.ffconcat")

    cmd = [*FFMPEG, "-f", "concat", "-safe", "0", "-i", str(listing), "-i", str(narration.audio)]
    filters = [f"[0:v]scale={WIDTH}:{HEIGHT},setsar=1,fps={FPS},format=yuv420p[slides]"]
    if avatar is None:
        filters.append("[slides]null[vout]")
    else:
        big = hero_intervals(timed, hero or [False] * len(timed), total)
        mask, ring = _bubble_assets(work)
        still = ["-loop", "1", "-t", f"{total:.3f}"]
        cmd += _bubble_inputs(avatar, mask, ring, total)  # inputs 2, 3, 4
        filters.append(f"[2:v]setpts=PTS-STARTPTS,fps={FPS},crop='min(iw,ih)':'min(iw,ih)',split=2[sq_small][sq_big]")
        if big:
            hero_mask, hero_ring = _bubble_assets(work, HERO_SIZE, RING + 2)
            cmd += [*still, "-i", str(hero_mask), *still, "-i", str(hero_ring)]  # inputs 5, 6
            filters += _circle_overlay("slides", "sq_big", 5, 6, HERO_SIZE, HERO_BOX[:2], total, "withhero", _enable(big))
            filters += _circle_overlay(
                "withhero", "sq_small", 3, 4, BUBBLE, _bubble_xy(), total, "withbubble", f"not({_enable(big)})"
            )
        else:
            filters.append("[sq_big]nullsink")
            filters += _circle_overlay("slides", "sq_small", 3, 4, BUBBLE, _bubble_xy(), total, "withbubble")
        filters.append("[withbubble]format=yuv420p[vout]")

    cmd += ["-filter_complex", ";".join(filters), "-map", "[vout]", "-map", "1:a"]
    cmd += [*_encode_options(total), str(output)]
    run(cmd)
    return total


def poster(slide: Path, dest: Path) -> tuple[int, int]:
    """The first slide as the video's poster (JPEG, 1280 wide). Returns its size."""
    with Image.open(slide) as image:
        image = image.convert("RGB")
        image.thumbnail((1280, 720))
        image.save(dest, "JPEG", quality=85, optimize=True)
        return image.size


# ── Recording with slides ─────────────────────────────────────────────

SLIDE_BACKGROUND = (12, 12, 12)
SLIDE_MARGIN = 48


def letterbox(page: Path, dest: Path) -> Path:
    """A deck page fitted inside 1920x1080 on the dark background, never stretched."""
    with Image.open(page) as image:
        image = image.convert("RGB")
        image.thumbnail((WIDTH - 2 * SLIDE_MARGIN, HEIGHT - 2 * SLIDE_MARGIN), Image.LANCZOS)
        canvas = Image.new("RGB", (WIDTH, HEIGHT), SLIDE_BACKGROUND)
        canvas.paste(image, ((WIDTH - image.width) // 2, (HEIGHT - image.height) // 2))
        canvas.save(dest)
    return dest


def slide_segments(timeline: list[tuple[float, int]], page_count: int, duration: float) -> list[tuple[int, int]]:
    """(page index, frames on screen) for each slide change, on the video's frame grid.

    Changes keep the order they were recorded in (the last one within a frame wins), the first slide
    is on screen from the start, empty segments are dropped and the frames add up to the recording.
    """
    total = max(1, round(duration * FPS))

    def clamp(page: int) -> int:
        return max(0, min(page, page_count - 1))

    ordered = sorted(timeline, key=lambda point: point[0])  # stable: equal times keep their recorded order
    starts = {0: clamp(ordered[0][1]) if ordered else 0}
    for at, page in ordered:
        starts[min(max(round(at * FPS), 0), total)] = clamp(page)
    frames = sorted(frame for frame in starts if frame < total)
    segments: list[tuple[int, int]] = []
    for start, end in zip(frames, [*frames[1:], total]):
        page = starts[start]
        if segments and segments[-1][0] == page:  # the same page again: it just stays on screen
            segments[-1] = (page, segments[-1][1] + end - start)
        else:
            segments.append((page, end - start))
    return segments


def compose_recording(
    pages: list[Path], timeline: list[tuple[float, int]], camera: Path, duration: float, output: Path, work: Path,
    camera_has_audio: bool = True,
) -> Path:
    """Deck pages full screen, switched at the recorded times, with the camera in the bubble.

    Only the pages the timeline shows are read. Returns the first slide as shown (for the poster).
    """
    if not pages:
        raise ComposeError("La presentación no tiene páginas")
    segments = slide_segments(timeline, len(pages), duration)
    shown: dict[int, Path] = {}  # a page shown several times is fitted once
    for page, _ in segments:
        if page not in shown:
            shown[page] = letterbox(pages[page], work / f"page_{page:03d}.png")
    # One FFmpeg input for the whole deck, however many changes: the concat demuxer switches the images.
    lines = ["ffconcat version 1.0"]
    rate = f"option framerate {FPS}"  # images default to 25 fps: every change would drift up to a frame
    for page, frames in segments:
        lines += [f"file '{shown[page].name}'", rate, f"duration {frames / FPS:.6f}"]
    lines += [f"file '{shown[segments[-1][0]].name}'", rate]  # the demuxer ignores the last duration otherwise
    listing = work / "slides.ffconcat"
    listing.write_text("\n".join(lines) + "\n", encoding="utf-8")

    mask, ring = _bubble_assets(work)
    cmd = [*FFMPEG, "-f", "concat", "-safe", "0", "-i", str(listing), *_bubble_inputs(camera, mask, ring, duration)]
    filters = [f"[0:v]scale={WIDTH}:{HEIGHT},setsar=1,fps={FPS},format=yuv420p[slides]"]
    filters += _bubble_filters("slides", 1, duration, keep_timing=True)
    if camera_has_audio:
        filters.append(f"[1:a]{LOUDNORM},aresample={SAMPLE_RATE}[aout]")
    else:  # e.g. the microphone was denied: a silent track, so every player handles the file the same way
        cmd += ["-f", "lavfi", "-t", f"{duration:.3f}", "-i", f"anullsrc=r={SAMPLE_RATE}:cl=mono"]
        filters.append("[4:a]anull[aout]")  # inputs: 0 slides, 1 camera, 2 mask, 3 ring, 4 silence
    cmd += ["-filter_complex", ";".join(filters), "-map", "[vout]", "-map", "[aout]"]
    cmd += [*_encode_options(duration), str(output)]
    run(cmd, timeout=max(FFMPEG_TIMEOUT_SECONDS, int(duration * 3)))  # a long class takes a while to encode
    return shown[segments[0][0]]
