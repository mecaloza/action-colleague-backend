"""
What a video's review looks at (pure functions over local files: the render and the review job handle storage and the
database; `app.services.video_reviews` turns it into findings).

Frames (`sample`): one per scene where its slide is settled (every part on screen, just before the next scene comes),
one at random inside each scene and a few at random over the whole video, seeded: the same seed takes the same frames.
None is ever taken while a scene arrives (the slides cross, the backdrops fade into each other) or while a part of a
slide slides in: those moments show two things on top of each other on purpose, never a defect.

Checks (`video_checks`): what FFmpeg tells on its own, in one pass over the video: a black screen, a long silence and,
while the presenter is in its bubble, the bubble standing still (a presenter clip shorter than the narration freezes,
then leaves an empty ring).
"""

import logging
import random
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from app.services.slides.render import BUBBLE_BOX, beat_count, is_hero
from app.services.slides.spec import CASE_PARTS, MAX_COLUMN_POINTS, MAX_POINTS, Slide, chart_bars, visible_points
from app.services.video import compose, motion

logger = logging.getLogger(__name__)

FRAME_WIDTH, FRAME_HEIGHT = 960, 540
RANDOM_FRAMES = 6  # over the whole video, besides the two of each scene
UNTIMED_FRAMES = 12  # a video whose scenes' times are unknown: random ones only
MAX_FRAMES = 40  # what the model looks at in one go: past it, the random ones go first
FRAME_SECONDS = 1 / motion.FPS
SETTLE_SECONDS = 2 / motion.FPS  # a part that slid in is still this long after its entrance, for sure
ARRIVED_SECONDS = 0.1  # this long after a scene's narration starts, its arrival is over
LAST_SCENE_END = 0.3  # nothing is taken closer to the end of the video
MIN_GAP_SECONDS = 0.5  # a frame at random this close to another one adds nothing
JPEG_QUALITY = 4  # FFmpeg's -q:v (2 best .. 31 worst): about 50 KB a frame
FRAME_TIMEOUT_SECONDS = 120
FRAMES_AT_ONCE = 3  # FFmpeg processes taking frames side by side (each decodes on one thread)
CHECK_TIMEOUT_SECONDS = 1800  # one pass over the whole video, decoding it
MIN_FREEZE_SECONDS = 3.0
MIN_BLACK_SECONDS = 1.0  # the first scene fades in from black for half a second: that is no black screen
MIN_SILENCE_SECONDS = 2.5  # the narration's own pauses between scenes are 0.7 s, its tail 1 s
FREEZE_NOISE = 0.001  # freezedetect's: smaller differences between frames are compression noise
BLACK_PIXEL = 0.03  # blackdetect's: a pixel this close to black is black (the dark theme's background is not)
SILENCE_NOISE = "-50dB"
FACE_CROP = 200  # px of the presenter's bubble the freeze check looks at: inside the circle, clear of its ring
ERROR_TAIL_BYTES = 800  # of FFmpeg's log kept in the log line


class ReviewError(RuntimeError):
    """FFmpeg could not take a frame or read the video (the message is the admin's)."""


@dataclass(frozen=True)
class Timeline:
    """When each scene of a video is on screen, as its render timed it (seconds in the video)."""

    starts: list[float]  # when each scene's narration starts: its slide finished arriving just then
    lengths: list[float]
    total: float
    beats: list[list[float]]  # when each later part of its slide appears

    def arrivals(self) -> list[float]:
        """When each scene starts arriving (the first one fades in from black at once)."""
        return [0.0, *(max(0.0, start - motion.TRANSITION_SECONDS) for start in self.starts[1:])]

    def ends(self) -> list[float]:
        """When each scene starts leaving: the next one arrives (the last one stays to the end)."""
        return [*self.arrivals()[1:], self.total]

    def scene_at(self, at: float) -> int:
        """The scene on screen at `at` (the arriving one, during an arrival)."""
        return max(index for index, arrival in enumerate(self.arrivals()) if arrival <= max(at, 0.0))

    def busy(self) -> list[tuple[float, float]]:
        """When two things are on screen at once on purpose: a scene arriving, a part sliding in."""
        windows = [(start - motion.TRANSITION_SECONDS, start + ARRIVED_SECONDS) for start in self.starts]
        windows += [
            (at - FRAME_SECONDS, at + motion.ENTRANCE_SECONDS + FRAME_SECONDS) for beats in self.beats for at in beats
        ]
        return windows

    def settled(self, index: int) -> float | None:
        """When the scene's slide is whole and still: right after its last part slid in, or just before the next
        scene comes (None for a scene too short to show settled)."""
        start = self.starts[index]
        last = max([start, *self.beats[index]])
        latest = self.total - LAST_SCENE_END if index == len(self.starts) - 1 else self.ends()[index] - SETTLE_SECONDS
        earliest = start + ARRIVED_SECONDS + FRAME_SECONDS
        if latest < earliest:
            return None
        return max(earliest, min(latest, last + motion.ENTRANCE_SECONDS + SETTLE_SECONDS))

    def hero_spans(self, hero: list[bool]) -> list[tuple[float, float]]:
        """When the presenter is shown large (see `compose.hero_intervals`)."""
        scenes = [motion.Scene([], start, []) for start in self.starts]
        return compose.hero_intervals(scenes, hero, self.total)


def _even_beats(scene: dict, start: float, length: float) -> list[float]:
    """A part's time as the render spread it without word timings (a render from before beats were kept)."""
    try:
        return motion.beat_times(Slide.model_validate(scene.get("slide") or {}), [], start, length)
    except ValidationError:
        return []


def timeline_of(state: dict, scenes: list[dict]) -> Timeline | None:
    """The scenes' times a render kept in its state (None when it kept none, or for other scenes)."""
    starts, lengths, total = state.get("starts"), state.get("lengths"), state.get("total")
    if not starts or not lengths or not total or len(starts) != len(scenes) or len(lengths) != len(scenes):
        return None
    beats = state.get("beats")
    if not beats or len(beats) != len(scenes):
        beats = [_even_beats(scene, start, length) for scene, start, length in zip(scenes, starts, lengths)]
    return Timeline([float(at) for at in starts], [float(at) for at in lengths], float(total),
                    [[float(at) for at in times] for times in beats])


# ── Frames ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Frame:
    index: int  # its place among the review's frames, in the video's order
    at: float  # seconds in the video
    scene: int | None  # the scene on screen (its place in the video); None when unknown
    kind: str  # "settled", "scene" (at random inside it) or "random" (anywhere)


def _free(spans: list[tuple[float, float]], avoid: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """The parts of `spans` outside every window of `avoid`."""
    free = []
    for span in spans:
        pieces = [span]
        for low, high in avoid:
            pieces = [
                part for start, end in pieces for part in ((start, min(end, low)), (max(start, high), end))
                if part[1] - part[0] > 1e-6
            ]
        free += pieces
    return free


def _pick(rng: random.Random, spans: list[tuple[float, float]], avoid: list[tuple[float, float]]) -> float | None:
    """A moment at random inside `spans` and outside `avoid` (None when there is no such moment)."""
    free = _free(spans, avoid)
    room = sum(end - start for start, end in free)
    if room <= 0:
        return None
    left = rng.uniform(0, room)
    for start, end in free:
        if left <= end - start:
            return start + left
        left -= end - start
    return free[-1][1]


def _near(picks: list[tuple[float, int | None, str]]) -> list[tuple[float, float]]:
    """Right beside the moments already picked: another frame there would show the same."""
    return [(at - MIN_GAP_SECONDS, at + MIN_GAP_SECONDS) for at, _, _ in picks]


def sample(timeline: Timeline | None, duration: float, seed: str) -> list[Frame]:
    """The moments a review looks at, in the video's order (see the module's docstring)."""
    rng = random.Random(seed)
    last = max(0.0, duration - LAST_SCENE_END)
    picks: list[tuple[float, int | None, str]] = []
    if timeline is None:  # only the first scene's arrival is known: the fade in from black
        avoid, extra = [(0.0, motion.TRANSITION_SECONDS + ARRIVED_SECONDS)], UNTIMED_FRAMES
    else:
        avoid, extra = timeline.busy(), RANDOM_FRAMES
        for index in range(len(timeline.starts)):
            settled = timeline.settled(index)
            if settled is not None and settled <= last:
                picks.append((settled, index, "settled"))
        for index, (start, length) in enumerate(zip(timeline.starts, timeline.lengths)):
            at = _pick(rng, [(start, min(start + length, last))], avoid + _near(picks))
            if at is not None:
                picks.append((at, index, "scene"))
    for _ in range(extra):
        at = _pick(rng, [(0.0, last)], avoid + _near(picks))
        if at is not None:
            picks.append((at, timeline.scene_at(at) if timeline else None, "random"))
    for kind in ("random", "scene"):  # too many for one look: the random ones go first, then the scenes'
        while len(picks) > MAX_FRAMES and any(pick[2] == kind for pick in picks):
            picks.remove(next(pick for pick in reversed(picks) if pick[2] == kind))
    picks.sort(key=lambda pick: pick[0])
    return [Frame(index, round(at, 3), scene, kind) for index, (at, scene, kind) in enumerate(picks)]


def frame_name(index: int) -> str:
    """A frame's file name, by its index."""
    return f"{index:02d}.jpg"


def _extract(video: Path, frame: Frame, folder: Path) -> Path:
    path = folder / frame_name(frame.index)
    failed = f"No pudimos tomar el fotograma del segundo {frame.at:.1f} del video."
    try:  # seeking on the input: only the stretch before the frame is decoded
        compose.run([*compose.FFMPEG, "-ss", f"{frame.at:.3f}", "-i", str(video), "-frames:v", "1",
                     "-vf", f"scale={FRAME_WIDTH}:-2", "-q:v", str(JPEG_QUALITY), "-threads", "1", str(path)],
                    timeout=FRAME_TIMEOUT_SECONDS)
    except compose.ComposeError as exc:
        raise ReviewError(failed) from exc
    if not path.exists() or not path.stat().st_size:
        raise ReviewError(failed)
    return path


def extract_frames(video: Path, frames: list[Frame], folder: Path) -> list[Path]:
    """Each frame as a JPEG 960 px wide in `folder`, a few at a time. Raises ReviewError when FFmpeg can't take one."""
    folder.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=FRAMES_AT_ONCE) as pool:
        return list(pool.map(lambda frame: _extract(video, frame, folder), frames))


# ── What FFmpeg tells on its own ──────────────────────────────────────


def _span(start: float, end: float) -> dict:
    return {"start": round(start, 2), "end": round(end, 2), "seconds": round(end - start, 2)}


def _spans(lines: list[str], start: str, end: str, duration: float) -> list[tuple[float, float]]:
    """The stretches a detect filter logged: each from a line matching `start` to the next matching `end` (to the
    end of the video when the filter stopped before closing it)."""
    spans, opened = [], None
    for line in lines:
        if match := re.search(start, line):
            opened = max(0.0, float(match.group(1)))
        if (match := re.search(end, line)) and opened is not None:
            spans.append((opened, min(duration, float(match.group(1)))))
            opened = None
    if opened is not None:
        spans.append((opened, duration))
    return spans


def _within(spans: list[tuple[float, float]], windows: list[tuple[float, float]], least: float) -> list[dict]:
    """The parts of `spans` inside `windows` that last at least `least` seconds."""
    parts = [(max(start, low), min(end, high)) for start, end in spans for low, high in windows]
    return [_span(start, end) for start, end in parts if end - start >= least]


def bubble_spans(timeline: Timeline, hero: list[bool]) -> list[tuple[float, float]]:
    """When the presenter is in its bubble: the whole video but where it is large."""
    spans, cursor = [], 0.0
    for start, end in timeline.hero_spans(hero):
        if start > cursor:
            spans.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < timeline.total:
        spans.append((cursor, timeline.total))
    return spans


def video_checks(video: Path, duration: float, bubble: list[tuple[float, float]] | None) -> dict:
    """Black screens, long silences and, while the presenter is in its bubble (`bubble`: those stretches; None when
    the video has no presenter or it is unknown when), the bubble standing still: each as [{start, end, seconds}]
    (`frozen` None when not looked at). Raises ReviewError when FFmpeg can't read the video."""
    black = f"blackdetect=d={MIN_BLACK_SECONDS}:pix_th={BLACK_PIXEL}[black]"
    silence = f"[0:a]silencedetect=noise={SILENCE_NOISE}:d={MIN_SILENCE_SECONDS}[silent]"
    if bubble is None:
        graph, outputs = [f"[0:v]scale=320:-2,{black}", silence], ["[black]", "[silent]"]
    else:
        left = (BUBBLE_BOX[0] + BUBBLE_BOX[2] - FACE_CROP) // 2
        top = (BUBBLE_BOX[1] + BUBBLE_BOX[3] - FACE_CROP) // 2
        freeze = f"freezedetect=n={FREEZE_NOISE}:d={MIN_FREEZE_SECONDS}[frozen]"
        graph = ["[0:v]split=2[face][whole]", f"[face]crop={FACE_CROP}:{FACE_CROP}:{left}:{top},{freeze}",
                 f"[whole]scale=320:-2,{black}", silence]
        outputs = ["[frozen]", "[black]", "[silent]"]
    # Its detect filters log what they find at the info level: read from its log, never shown to anyone.
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-loglevel", "info", "-i", str(video),
           "-filter_complex", ";".join(graph), *(arg for output in outputs for arg in ("-map", output)),
           "-f", "null", "-"]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=CHECK_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise ReviewError("El video tardó demasiado en revisarse.") from exc
    log = result.stderr.decode(errors="replace")
    if result.returncode != 0:
        logger.warning("review_checks_failed", extra={"video": video.name, "error": log[-ERROR_TAIL_BYTES:]})
        raise ReviewError("No pudimos leer el video para revisarlo.")
    lines = log.splitlines()
    number = r"(-?\d+(?:\.\d+)?)"
    frozen = None
    if bubble is not None:
        frozen = _within(_spans(lines, rf"freeze_start: {number}", rf"freeze_end: {number}", duration), bubble,
                         MIN_FREEZE_SECONDS)
    return {
        "black": [_span(*span) for span in _spans(lines, rf"black_start:{number}", rf"black_end:{number}", duration)],
        "silence": [
            _span(*span) for span in _spans(lines, rf"silence_start: {number}", rf"silence_end: {number}", duration)
            if span[1] - span[0] >= MIN_SILENCE_SECONDS
        ],
        "frozen": frozen,
    }


# ── What each frame should show ───────────────────────────────────────


def slide_texts(slide: Slide) -> list[str]:
    """The texts a slide shows, as the script has them: what its frames are read against."""
    texts = [slide.eyebrow, slide.title, slide.subtitle]
    if slide.layout in ("bullets", "steps", "closing", "calculation"):
        texts += visible_points(slide.points, MAX_POINTS)
    elif slide.layout == "case":
        texts += [f"{heading}: {point}" for heading, point in zip(CASE_PARTS, slide.points)]
    elif slide.layout == "comparison":
        for column in (slide.left, slide.right):
            texts += [column.heading, *visible_points(column.points, MAX_COLUMN_POINTS)]
    elif slide.layout == "chart":
        unit = f" {slide.chart_unit.strip()}" if slide.chart_unit.strip() else ""
        texts += [f"{label}: {value:g}{unit}" for label, value in chart_bars(slide)]
    if slide.layout in ("stat", "calculation"):
        texts += [slide.stat_value, slide.stat_label]
    elif slide.layout == "statement":
        texts.append(slide.quote_author)
    return [text.strip() for text in texts if text.strip()]


def _slide(scene: dict) -> Slide | None:
    try:
        return Slide.model_validate(scene.get("slide") or {})
    except ValidationError:
        return None


@dataclass(frozen=True)
class Video:
    """What a video shows, scene by scene, as its render made it."""

    scenes: list[dict]  # the script it was made from ({id, slide, narration, visual}), frozen by its render
    timeline: Timeline | None  # None: when each scene shows is unknown
    duration: float
    presenter: bool  # it shows the presenter: in its bubble, and large on the cover and the closing
    pictures: frozenset[int] = frozenset()  # the scenes shown over their visual (an infographic, a photo, a clip)

    def scene_id(self, index: int | None) -> str | None:
        if index is None or not 0 <= index < len(self.scenes):
            return None
        return str(self.scenes[index].get("id", "")) or None

    def places(self) -> dict[str | None, int]:
        """Each scene's place in the video, by its id."""
        return {self.scene_id(index): index for index in range(len(self.scenes))}

    def hero(self) -> list[bool]:
        """The scenes where the presenter is large."""
        total = len(self.scenes)
        return [bool(slide and is_hero(slide, index + 1, total, self.presenter))
                for index, slide in enumerate(map(_slide, self.scenes))]

    def bubble(self) -> list[tuple[float, float]] | None:
        """When the presenter is in its bubble (None without a presenter, or when the scenes' times are unknown)."""
        if not self.presenter or self.timeline is None:
            return None
        return bubble_spans(self.timeline, self.hero())

    def context(self, frame: Frame) -> dict:
        """What the frame should show, for the model that looks at it."""
        index = frame.scene
        entry: dict = {
            "frame_index": frame.index, "at_seconds": round(frame.at, 2), "scene_id": self.scene_id(index) or "",
        }
        slide = _slide(self.scenes[index]) if index is not None and 0 <= index < len(self.scenes) else None
        if slide is None:
            return {**entry, "texts": []}
        picture = index in self.pictures
        visual = self.scenes[index].get("visual") or {}
        infographic = picture and visual.get("kind") == "infographic"
        entry.update(
            scene_number=index + 1, layout=slide.layout,
            presenter="large" if self.hero()[index] else "bubble" if self.presenter else "none",
            texts=[] if infographic and slide.layout == "visual" else slide_texts(slide),
        )
        parts = beat_count(slide) - 1  # those that appear one after the other, after the slide's frame
        if parts > 0 and self.timeline is not None:
            shown = sum(1 for at in self.timeline.beats[index] if at + motion.ENTRANCE_SECONDS <= frame.at)
            entry["revealed"] = f"{min(shown, parts)} de {parts}"
        if infographic:
            entry["infographic"] = visual.get("prompt") or slide.title
        elif picture:
            entry["backdrop"] = True
        return entry
