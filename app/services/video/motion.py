"""
Motion for AI videos: when each part of a slide appears, and the animated frames that make it move.

A slide is revealed beat by beat (see `render.beat_count`): the title first, then each point as the narration
reaches it. Beats are timed from the speech's word timings; a point the narration never names outright is
spread over the rest of the scene. Between scenes the slide pushes the previous one out (or fades, at the ends).

`timeline` turns this into a list of (image, frames) segments on the video's frame grid: still images held
for their time plus short animations (a beat sliding in, a scene transition), each written as its own frames.
"""

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageChops

from app.services.slides.render import HEIGHT, WIDTH
from app.services.slides.spec import MAX_COLUMN_POINTS, MAX_POINTS, Slide, visible_points
from app.services.video.captions import TimedWord

FPS = 30
TRANSITION_SECONDS = 0.5  # a scene change; it ends when the scene's narration starts
ENTRANCE_SECONDS = 0.45  # a beat sliding in
ENTRANCE_RISE = 28  # pixels a beat rises while it fades in
LEAD_SECONDS = 0.25  # a point appears this long before the narration says it
MIN_BEAT_GAP = 0.7  # seconds between two beats, so each one is seen arriving
FIRST_BEAT_DELAY = 0.15  # the first beat of a scene enters right after the transition
MIN_KEYWORD_CHARS = 4
STEM_CHARS = 5  # "casco" matches "cascos": words are compared by their first letters
STOPWORDS = {
    "para", "como", "cuando", "donde", "pero", "porque", "sobre", "entre", "desde", "hasta", "este", "esta",
    "estos", "estas", "tiene", "tienen", "hacer", "debe", "deben", "siempre", "nunca", "todo", "todos", "cada",
    "that", "with", "this", "from", "your", "have", "com", "uma", "mais", "sempre",
}


# ── When each beat appears ────────────────────────────────────────────


def _normalized(word: str) -> str:
    plain = unicodedata.normalize("NFKD", word).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", plain.lower())


def _keywords(text: str) -> set[str]:
    words = (_normalized(word) for word in text.split())
    return {word[:STEM_CHARS] for word in words if len(word) >= MIN_KEYWORD_CHARS and word not in STOPWORDS}


def _says(spoken: str, keywords: set[str]) -> bool:
    """Whether a spoken word is one of the keywords, or the same word with a longer ending ("idea", "ideas")."""
    return any(spoken.startswith(keyword) for keyword in keywords)


def beat_texts(slide: Slide) -> list[str]:
    """What each beat after the first shows (the text the narration is matched against)."""
    if slide.layout in ("bullets", "steps", "closing"):
        return visible_points(slide.points, MAX_POINTS)
    if slide.layout == "comparison":
        return visible_points(slide.left.points, MAX_COLUMN_POINTS) + visible_points(
            slide.right.points, MAX_COLUMN_POINTS
        )
    if slide.layout == "cover":
        return [slide.title] + ([slide.subtitle] if slide.subtitle.strip() else [])
    if slide.layout == "statement":
        return [slide.title] + ([slide.quote_author] if slide.quote_author.strip() else [])
    return [slide.stat_value, slide.stat_label or slide.title]  # stat


def beat_times(slide: Slide, words: list[TimedWord], start: float, length: float) -> list[float]:
    """When beats 1.. appear (seconds in the video); beat 0, the slide's frame, is there from `start`.

    A beat appears just before the narration first says one of its words (after the previous beat); one the
    narration doesn't name is spread evenly. Beats never come closer than MIN_BEAT_GAP, and all of them are
    on screen before the scene ends.
    """
    texts = beat_texts(slide)
    if not texts:
        return []
    end = start + max(length, 0.0)
    latest = max(start + FIRST_BEAT_DELAY, end - 0.4)
    headline = slide.layout in ("cover", "statement", "stat")  # its first beat is the slide itself: at once
    spoken = [(_normalized(word.text), word.start) for word in words if start - 0.01 <= word.start <= end]
    # The beats share the time they have: apart by MIN_BEAT_GAP, or less when the scene is short.
    gap = min(MIN_BEAT_GAP, (latest - start - FIRST_BEAT_DELAY) / max(1, len(texts) - 1))
    # A word every point shares ("idea" in "primera idea", "segunda idea") says nothing about which one it is.
    all_keywords = [_keywords(text) for text in texts]
    times: list[float] = []
    cursor = 0  # words before it belong to earlier beats
    for index, text in enumerate(texts):
        even = start + FIRST_BEAT_DELAY + (length - FIRST_BEAT_DELAY) * index / len(texts)
        at = None
        if not (headline and index == 0):
            others = set().union(*(words for number, words in enumerate(all_keywords) if number != index))
            keywords = (all_keywords[index] - others) or all_keywords[index]
            for position in range(cursor, len(spoken)):
                if _says(spoken[position][0], keywords):
                    at, cursor = spoken[position][1] - LEAD_SECONDS, position + 1
                    break
        if at is None:
            at = start + FIRST_BEAT_DELAY if index == 0 else even
        earliest = times[-1] + gap if times else start + FIRST_BEAT_DELAY
        room_left = latest - gap * (len(texts) - 1 - index)  # the beats still to come must fit after it
        times.append(min(max(at, earliest), room_left))
    return times


# ── Frames ────────────────────────────────────────────────────────────


def _ease(progress: float) -> float:
    """Ease-out cubic: fast start, soft landing."""
    return 1 - (1 - progress) ** 3


def _frames(seconds: float) -> int:
    return max(1, round(seconds * FPS))


def entrance(before: Image.Image, after: Image.Image, count: int) -> list[Image.Image]:
    """The part of `after` that `before` lacks rising into place while it fades in (`count` frames)."""
    box = ImageChops.difference(before, after).getbbox()
    if box is None:
        return [after] * count
    left, top, right, bottom = box
    top, bottom = max(0, top - 4), min(HEIGHT, bottom + 4)
    piece = after.crop((left, top, right, bottom))
    frames = []
    for number in range(1, count + 1):
        progress = _ease(number / count)
        rise = round((1 - progress) * ENTRANCE_RISE)
        frame = before.copy()
        target = (left, min(HEIGHT - (bottom - top), top + rise))
        under = frame.crop((*target, target[0] + piece.width, target[1] + piece.height))
        frame.paste(Image.blend(under, piece, progress), target)
        frames.append(frame)
    return frames


def push(before: Image.Image, after: Image.Image, count: int) -> list[Image.Image]:
    """`after` slides in from the right, pushing `before` out to the left."""
    frames = []
    for number in range(1, count + 1):
        shift = round(_ease(number / count) * WIDTH)
        frame = Image.new("RGB", (WIDTH, HEIGHT))
        frame.paste(before, (-shift, 0))
        frame.paste(after, (WIDTH - shift, 0))
        frames.append(frame)
    return frames


def fade(before: Image.Image, after: Image.Image, count: int) -> list[Image.Image]:
    return [Image.blend(before, after, _ease(number / count)) for number in range(1, count + 1)]


@dataclass
class Scene:
    """A scene of the video: its slide image at each beat, and when its narration and beats start."""

    beats: list[Path]  # beats[k] shows beats 0..k
    start: float
    beat_times: list[float]  # when beats 1.. appear; one per image after the first
    push: bool = True  # enters by pushing the previous scene (else it fades in)


Segment = tuple[Path, int]  # an image and the frames it stays on screen


def timeline(scenes: list[Scene], total: float, work: Path, on_frame: Callable[[], None] | None = None) -> list[Segment]:
    """The whole video as images held on the frame grid, with the animation frames written to `work`.

    The first scene fades in from black; each later one arrives during the TRANSITION_SECONDS before its
    narration. A beat slides in at its time (shortened if the next event comes sooner).
    """
    total_frames = max(1, round(total * FPS))
    events: list[tuple[int, str, int, int]] = []  # (frame, kind, scene, beat)
    for index, scene in enumerate(scenes):
        arrive = 0 if index == 0 else max(0, round((scene.start - TRANSITION_SECONDS) * FPS))
        events.append((arrive, "scene", index, 0))
        for beat, at in enumerate(scene.beat_times, start=1):
            events.append((max(arrive + 1, round(at * FPS)), "beat", index, beat))
    events.sort(key=lambda event: (event[0], event[2], event[3]))

    cache: dict[Path, Image.Image] = {}

    def image(path: Path) -> Image.Image:
        if path not in cache:
            cache.clear()  # scenes go in order: only the current one's images are needed
            with Image.open(path) as opened:
                cache[path] = opened.convert("RGB")
        return cache[path]

    written = 0

    def save(frames: list[Image.Image]) -> list[Segment]:
        nonlocal written
        segments = []
        for frame in frames:
            path = work / f"motion_{written:05d}.png"
            frame.save(path, compress_level=1)
            written += 1
            segments.append((path, 1))
            if on_frame:
                on_frame()
        return segments

    segments: list[Segment] = []
    current: Path | None = None  # the image on screen after the last event
    position = 0  # frames placed so far
    for number, (frame, kind, scene_index, beat) in enumerate(events):
        frame = min(max(frame, position), total_frames)
        if frame >= total_frames:
            break
        if current is not None and frame > position:
            segments.append((current, frame - position))  # hold until this event
            position = frame
        next_frame = events[number + 1][0] if number + 1 < len(events) else total_frames
        room = max(1, min(next_frame, total_frames) - position)
        scene = scenes[scene_index]
        target = scene.beats[min(beat, len(scene.beats) - 1)]
        if kind == "scene":
            count = min(_frames(TRANSITION_SECONDS), room)
            if current is None:
                black = Image.new("RGB", (WIDTH, HEIGHT))
                frames = fade(black, image(target), count)
            else:
                before = image(current).copy()
                effect = push if scene.push else fade
                frames = effect(before, image(target), count)
        else:
            count = min(_frames(ENTRANCE_SECONDS), room)
            before = image(current).copy() if current else image(target)
            frames = entrance(before, image(target), count)
        frames = frames[: total_frames - position]
        segments += save(frames)
        position += len(frames)
        current = target
    if current is not None and position < total_frames:
        segments.append((current, total_frames - position))
    return segments


def write_concat(segments: list[Segment], dest: Path) -> Path:
    """The segments as one FFmpeg concat input (each image for its frames at FPS)."""
    rate = f"option framerate {FPS}"  # images default to 25 fps: durations would drift
    lines = ["ffconcat version 1.0"]
    for path, frames in segments:
        lines += [f"file '{path}'", rate, f"duration {frames / FPS:.6f}"]
    lines += [f"file '{segments[-1][0]}'", rate]  # the demuxer ignores the last duration otherwise
    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return dest
