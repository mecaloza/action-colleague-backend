"""
Video composition with FFmpeg: slide timeline + narration + optional presenter bubble.

Timing model (all from decoded sample counts, never from metadata):
  narration i starts at s_i = LEAD_IN + sum(a_j + GAP for j < i)
  slide i is fully visible from s_i and cross-fades in during [s_i - FADE, s_i]
  xfade shortens the chain by FADE per transition, so every segment except the first is FADE
  seconds longer than its visible time; the offsets are simply s_i - FADE.
"""

import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw

from app.services.slides.render import BUBBLE_MARGIN, HEIGHT, WIDTH
from app.services.slides.render import BUBBLE_SIZE as BUBBLE  # the bottom-right corner the slides leave free

FPS = 30
SAMPLE_RATE = 48000
CHANNELS, SAMPLE_WIDTH = 1, 2  # the narration is mono, 16-bit (2 bytes per sample)
LEAD_IN, GAP, TAIL, FADE = 0.4, 0.7, 1.0, 0.4
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


def _circle(dest: Path, mode: str, background: int | tuple[int, ...], **style) -> Path:
    """A BUBBLE x BUBBLE image filled by a circle, drawn SUPERSAMPLE times larger and shrunk (anti-aliased)."""
    size = BUBBLE * SUPERSAMPLE
    image = Image.new(mode, (size, size), background)
    ImageDraw.Draw(image).ellipse([0, 0, size - 1, size - 1], **style)
    image.resize((BUBBLE, BUBBLE), Image.LANCZOS).save(dest)
    return dest


def _bubble_assets(work: Path) -> tuple[Path, Path]:
    """The presenter bubble's circular alpha mask and its orange ring."""
    mask = _circle(work / "bubble_mask.png", "L", 0, fill=255)
    ring = _circle(work / "bubble_ring.png", "RGBA", (0, 0, 0, 0), outline=ACCENT + (255,), width=RING * SUPERSAMPLE)
    return mask, ring


def _slide_durations(narration: Narration) -> list[float]:
    """Length of each slide's clip: up to the next slide's start (the last one, to the end), plus its FADE."""
    starts = narration.starts
    ends = [*starts[1:], narration.total]
    return [ends[0], *(end - start + FADE for start, end in zip(starts[1:], ends[1:]))]


def _slide_filters(starts: list[float]) -> tuple[list[str], str]:
    """Every slide scaled to the frame and chained with cross-fades. Returns the filters and the chain's last label."""
    filters = [
        f"[{i}:v]scale={WIDTH}:{HEIGHT},setsar=1,fps={FPS},format=yuv420p,settb=AVTB[v{i}]" for i in range(len(starts))
    ]
    last = "v0"
    for i in range(1, len(starts)):
        filters.append(f"[{last}][v{i}]xfade=transition=fade:duration={FADE}:offset={starts[i] - FADE:.3f}[x{i}]")
        last = f"x{i}"
    return filters, last


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
    x, y = WIDTH - BUBBLE - BUBBLE_MARGIN, HEIGHT - BUBBLE - BUBBLE_MARGIN
    timing = f"fps={FPS}:start_time=0" if keep_timing else f"setpts=PTS-STARTPTS,fps={FPS}"
    return [
        # Square crop from the centre, then scaled: never stretched. The last frame holds if short.
        f"[{avatar}:v]{timing},crop='min(iw,ih)':'min(iw,ih)',"
        f"scale={BUBBLE}:{BUBBLE},tpad=stop_mode=clone:stop_duration={PRESENTER_HOLD_SECONDS},"
        f"trim=duration={duration:.3f},format=rgba[av]",
        f"[{mask}:v]fps={FPS},format=gray[mask]",
        "[av][mask]alphamerge[bubble]",
        f"[{background}][bubble]overlay=x={x}:y={y}:eof_action=pass[withbubble]",
        f"[{ring}:v]fps={FPS},format=rgba[ring]",
        f"[withbubble][ring]overlay=x={x}:y={y}:eof_action=pass,format=yuv420p[vout]",
    ]


def _encode_options(duration: float) -> list[str]:
    """H.264/AAC output options, cutting the file at exactly `duration`."""
    return [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p", "-r", str(FPS),
        "-c:a", "aac", "-b:a", "160k", "-ar", str(SAMPLE_RATE),
        "-t", f"{duration:.3f}", "-movflags", "+faststart", "-threads", "2",
    ]


def compose_video(slides: list[Path], narration: Narration, avatar: Path | None, output: Path, work: Path) -> float:
    """1920x1080 H.264/AAC MP4 (+faststart). Returns its duration in seconds."""
    if len(slides) != len(narration.starts):
        raise ComposeError("Cada escena necesita su diapositiva y su narración")
    total = narration.total
    cmd = [*FFMPEG]
    for slide, duration in zip(slides, _slide_durations(narration)):
        cmd += ["-loop", "1", "-framerate", str(FPS), "-t", f"{duration:.3f}", "-i", str(slide)]
    audio_index = len(slides)
    cmd += ["-i", str(narration.audio)]

    filters, last = _slide_filters(narration.starts)
    if avatar is None:
        filters.append(f"[{last}]null[vout]")
    else:
        mask, ring = _bubble_assets(work)
        cmd += _bubble_inputs(avatar, mask, ring, total)
        filters += _bubble_filters(last, audio_index + 1, total)

    cmd += ["-filter_complex", ";".join(filters), "-map", "[vout]", "-map", f"{audio_index}:a"]
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
