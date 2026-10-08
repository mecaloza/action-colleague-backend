"""
What producing a module's video keeps for the next one, so producing it again pays only for what changed.

  narration — each scene's speech (`courses/{id}/narration/{key}.mp3`), kept by what it says and in which voice, with
              its timings beside it (`{key}.json`: the characters' alignment and the words they make up, which the
              captions and the slides' beats are timed with). A scene that says the same in the same voice is never
              narrated again, whatever else changed around it.
  presenter — each presenter's take (`courses/{id}/presenter/{key}.mp4`), kept by the avatar, HeyGen's engine and the
              speech it lip-syncs, scene by scene. A change to the slides or the visuals only keeps every take.

Both are kept by content, like the visuals (`visuals.cache_path`), and stay until the course is deleted. Each video
records in its module what it used and left (`storyboard.render.cache`, see `record`): what a change will cost is told
from there, without asking the storage (`revisions.impact`). The render itself always asks the storage.
"""

import hashlib
import json
from collections.abc import Iterable

from app.core.config import get_settings
from app.services.video import compose, motion
from app.services.video.avatar import PRESENTER_BACKGROUND
from app.services.video.voice import AUDIO_FORMAT, VOICE_SETTINGS

NARRATION, PRESENTER, VISUALS = "narration", "presenter", "visuals"  # folders of a course's storage
TAKE_FORMAT = 1  # raise it when takes are asked for differently: those kept before are made again
MAX_RECORDED_CLIPS = 300  # a module's record of the speech its videos used (the oldest forgotten past it)
MAX_RECORDED_TAKES = 60


def _key(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:24]


def narration_key(voice_id: str, text: str) -> str:
    """A scene's speech by what it says and how (the voice, the model and how ElevenLabs is asked for it). The
    neighbouring scenes' text, sent along as a hint for the intonation, is left out: editing a scene never has its
    neighbours narrated again."""
    asked = [voice_id, get_settings().elevenlabs_model, AUDIO_FORMAT, VOICE_SETTINGS, text.strip()]
    return _key(json.dumps(asked, sort_keys=True, ensure_ascii=False, separators=(",", ":")))


def narration_paths(course_id: int, key: str) -> tuple[str, str]:
    """Where a scene's speech is kept, and its timings."""
    base = f"courses/{course_id}/{NARRATION}/{key}"
    return f"{base}.mp3", f"{base}.json"


def digest(audio: bytes) -> str:
    """A speech clip's identity: the same text narrated again is another clip (it lasts and sounds a little different),
    and the presenter's takes are kept by it."""
    return hashlib.sha256(audio).hexdigest()[:32]


def scene_speakers(count: int, two: bool) -> list[int]:
    """Who presents each scene: 0, or with a second presenter 0 and 1 taking turns (the first opens)."""
    return [index % 2 if two else 0 for index in range(count)]


def scene_roles(count: int) -> list[str]:
    """What sets each scene's stretch of the narration apart besides its speech: the first has the lead-in before it,
    the others a pause (see compose.build_narration). The silence after the last scene is left out: a take kept from
    when another scene followed it only runs short there, by less than a second of silence, and holds its last frame
    (compose.presenter_track). Adding or removing a scene at the end never has the scene before it recorded again."""
    return ["first", *["next"] * (count - 1)][:count]


def take_key(avatar_id: str, engine: str, clips: Iterable[tuple[str, str]]) -> str:
    """A presenter's take by what HeyGen makes it from: the avatar, the engine, the look and the narration it lip-syncs,
    which is, scene by scene, the speech clip (`digest`) in its place in the video (`scene_roles`) with the pauses
    around it."""
    timing = [compose.LEAD_IN, compose.GAP, compose.TAIL, motion.TRANSITION_SECONDS]
    asked = [TAKE_FORMAT, avatar_id, engine, PRESENTER_BACKGROUND, timing, [list(clip) for clip in clips]]
    return _key(json.dumps(asked, separators=(",", ":")))


def take_path(course_id: int, key: str) -> str:
    return f"courses/{course_id}/{PRESENTER}/{key}.mp4"


def course_folders(course_id: int) -> list[str]:
    """Everything a course's videos keep for the next ones (the visuals too): made from its content, it goes with it."""
    return [f"courses/{course_id}/{name}" for name in (VISUALS, NARRATION, PRESENTER)]


def is_kept(path: str) -> bool:
    """Whether a file is kept for the next videos (never one of a render's own leftovers)."""
    parts = path.split("/")
    return len(parts) == 4 and parts[0] == "courses" and parts[2] in (VISUALS, NARRATION, PRESENTER)


class Kept:
    """What a module's videos left for the next one, as its storyboard records it (`record`): the speech clips, by
    narration key with their digest, and the presenter's takes."""

    def __init__(self, record: dict | None = None):
        record = record or {}
        self.clips: dict[str, str] = dict(record.get("clips") or {})
        self.takes: set[str] = set(record.get("takes") or [])

    def clip(self, key: str) -> str | None:
        """The digest of the speech kept for a narration key; None when it would be narrated anew."""
        return self.clips.get(key)


def record(previous: dict | None, clips: dict[str, str], takes: list[str], dropped: Iterable[str] = ()) -> dict:
    """The record of a module's new video (`storyboard.render.cache`): the speech clips ({narration key: digest}) and
    the takes it used and left, after those its earlier videos did (they are kept too: undoing a change costs
    nothing), but the takes `dropped` from storage. The oldest are forgotten past a limit, the record living in the
    storyboard."""
    earlier, dropped = previous or {}, set(dropped)
    merged = {**{key: value for key, value in (earlier.get("clips") or {}).items() if key not in clips}, **clips}
    new = [take for take in dict.fromkeys(takes) if take not in dropped]
    kept = [take for take in earlier.get("takes") or [] if take not in new and take not in dropped] + new
    return {"clips": dict(list(merged.items())[-MAX_RECORDED_CLIPS:]), "takes": kept[-MAX_RECORDED_TAKES:]}
