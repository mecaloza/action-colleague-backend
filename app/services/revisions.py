"""
Revisions of a module's script: the changes the AI proposes from an admin's observation, previewed as a diff and
applied (or undone) on purpose, and the changes made otherwise (saved by hand, rewritten by the AI, undone), kept the
same way so any of them can be undone.

The model proposes, the server decides. The model names the scenes and the fields it changes; only those are taken
from its answer, through the same conversions as a first draft (`designer.to_slide` / `to_visual`), and every other
scene is copied as stored, byte for byte. A proposal is made from the script as the admin saw it: applying it after
other changes rebases it scene by scene, and is refused when the same scenes changed meanwhile.

A script revision's content is `{"scenes": [...], "reading": "..."}`: the module's storyboard scenes and its reading.
"""

import difflib
import hashlib
import json
import logging
import re
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.db.models import Job, Module, ModuleRevision
from app.schemas.studio import Storyboard, StoryboardScene
from app.services.ai import designer
from app.services.ai.designer import MAX_NARRATION_CHARS, MAX_STORYBOARD_SCENES, SceneChangeDraft, StoryboardPatchDraft
from app.services.slides.spec import SceneVisual
from app.services.video.avatar import get_avatar_provider
from app.services.video.visuals import scene_request

logger = logging.getLogger(__name__)

PENDING = ("proposing", "proposed")  # a proposal not applied nor discarded yet
MAX_READING_CHARS = 100_000  # the module's reading (as the editor's limit)
GENERATED_VISUALS = ("image", "infographic", "clip")  # made (and paid for) from their description
NOTHING_PROPOSED = "La IA no propuso ningún cambio. Describe lo que quieres cambiar con más detalle."

# The fields of a scene a change names (designer.SceneField), with the slide's keys each one replaces...
SLIDE_FIELDS = {
    "layout": ("layout",),
    "title": ("title",),
    "subtitle": ("subtitle",),
    "points": ("points",),
    "icons": ("icons",),
    "icon": ("icon",),
    "stat": ("stat_value", "stat_label"),
    "quote": ("quote_author",),
    "comparison": ("left", "right"),
    "chart": ("chart_labels", "chart_values", "chart_unit"),
}
# ...and the attributes of the model's scene (designer.SceneDraft) they come from ("layout" brings the whole slide).
DRAFT_FIELDS = {
    "title": ("title",),
    "subtitle": ("subtitle",),
    "points": ("points",),
    "icons": ("icons",),
    "icon": ("icon",),
    "stat": ("stat_value", "stat_label"),
    "quote": ("quote_author",),
    "comparison": ("left_heading", "left_points", "right_heading", "right_points"),
    "chart": ("chart_labels", "chart_values", "chart_unit"),
}
WITH_ICONS = ("bullets", "closing")  # each of their points has its icon: new points bring theirs
# What the admin sees change in a scene: those fields, the eyebrow (only the editor writes it), narration and visual.
DIFF_FIELDS = (*SLIDE_FIELDS, "eyebrow", "narration", "visual")


class ProposalError(Exception):
    """The model's answer leaves nothing that can be applied (the message is for the admin)."""


class RevisionError(Exception):
    """A change can't be applied or undone as it is (the message is for the admin)."""


class Conflict(Exception):
    """What changed since in the places a change touches: those scenes (ids) and whether the reading."""

    def __init__(self, scene_ids: list[str], reading: bool):
        super().__init__(", ".join(scene_ids) or "reading")
        self.scene_ids, self.reading = scene_ids, reading


# ── Content ───────────────────────────────────────────────────────────


def empty_scope() -> dict:
    return {"scene_ids": [], "question_ids": [], "qa_issue_id": None}


def script_content(module: Module) -> dict:
    """What a script revision changes: the module's scenes (as stored) and its reading."""
    return {"scenes": list((module.storyboard or {}).get("scenes") or []), "reading": module.content_text or ""}


def write_script(module: Module, content: dict) -> None:
    """Make `content` the module's script, keeping the rest of its storyboard (the outline, the video's record)."""
    module.storyboard = {**(module.storyboard or {}), "scenes": content["scenes"]}
    module.content_text = content["reading"]


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def content_hash(content: dict) -> str:
    """The content exactly as stored: whether anything at all changed since a proposal was made from it."""
    return hashlib.sha256(_canonical(content)).hexdigest()


def _normalized(scene: dict) -> dict:
    """A scene as the editor saves it, every default written down: two scenes that only differ in defaults one of
    them leaves out (stored before the field existed) are the same scene."""
    try:
        return StoryboardScene.model_validate(scene).model_dump(mode="json")
    except ValidationError:
        return scene


def _same(scene: dict, other: dict) -> bool:
    return scene == other or _normalized(scene) == _normalized(other)


def scene_hash(scene: dict) -> str:
    return hashlib.sha256(_canonical(_normalized(scene))).hexdigest()[:16]


def _moved(before: list, after: list) -> set:
    """The scenes in both lists whose place among the others changed: those out of the longest runs kept in order."""
    common = set(before) & set(after)
    old, new = [item for item in before if item in common], [item for item in after if item in common]
    kept = set()
    for block in difflib.SequenceMatcher(None, old, new, autojunk=False).get_matching_blocks():
        kept.update(old[block.a: block.a + block.size])
    return common - kept


# ── The video's scenes ────────────────────────────────────────────────


def video_scenes(scenes: list[dict]) -> list[dict]:
    """What a video shows, scene by scene and in order (kept as `storyboard.render.scene_hashes`)."""
    return [{"id": str(scene.get("id", "")), "hash": scene_hash(scene)} for scene in scenes]


def stale_scene_ids(storyboard: dict | None) -> list[str]:
    """The scenes the module's video doesn't show as the script has them now: changed, added or moved since, and
    then those it shows that the script no longer has. Empty when unknown (a video made before this was recorded)."""
    storyboard = storyboard or {}
    shown = (storyboard.get("render") or {}).get("scene_hashes")
    if not shown:
        return []
    then = {entry["id"]: entry["hash"] for entry in shown}
    now = [(str(scene.get("id", "")), scene_hash(scene)) for scene in storyboard.get("scenes") or []]
    moved = _moved([entry["id"] for entry in shown], [scene_id for scene_id, _ in now])
    stale = [scene_id for scene_id, digest in now if then.get(scene_id) != digest or scene_id in moved]
    present = {scene_id for scene_id, _ in now}
    return stale + [entry["id"] for entry in shown if entry["id"] not in present]


def _render_of(module: Module) -> dict:
    """What the module's storyboard says about its video (empty without an AI video)."""
    return (module.storyboard or {}).get("render") or {}


def backfill_video_scenes(db: Session) -> int:
    """
    The scenes of the videos made before they were recorded (the maintenance pass of every deploy): a video shows
    what its render froze when it started, its job's `state.input.scenes`. Idempotent: videos with their scenes
    recorded are left alone, and so are those whose job no longer says.
    """
    query = db.query(Module).filter(Module.source == "ai", Module.video_asset_id.isnot(None))
    waiting = [
        module.id for module in query if _render_of(module).get("job_id") and "scene_hashes" not in _render_of(module)
    ]
    if not waiting:
        db.commit()
        return 0
    # Locked (NO KEY UPDATE: learners' progress still references them) and read again: a script saved or a video
    # published meanwhile is never written over.
    modules = db.query(Module).filter(Module.id.in_(waiting)).populate_existing().with_for_update(key_share=True)
    by_job = {_render_of(module).get("job_id"): module for module in modules}
    filled = 0
    for job_id, state in db.query(Job.id, Job.state).filter(Job.id.in_([job_id for job_id in by_job if job_id])):
        module = by_job[job_id]
        scenes = ((state or {}).get("input") or {}).get("scenes")
        if scenes and "scene_hashes" not in _render_of(module):
            render = {**_render_of(module), "scene_hashes": video_scenes(scenes)}
            module.storyboard = {**module.storyboard, "render": render}
            filled += 1
    db.commit()
    if filled:
        logger.info("video_scenes_backfilled", extra={"modules": filled})
    return filled


# ── What changed ──────────────────────────────────────────────────────


def _field_values(scene: dict) -> dict[str, Any]:
    """A scene as the admin sees it, field by field (DIFF_FIELDS)."""
    normalized = _normalized(scene)
    slide = normalized.get("slide") or {}
    values: dict[str, Any] = {
        name: slide.get(keys[0]) if len(keys) == 1 else {key: slide.get(key) for key in keys}
        for name, keys in SLIDE_FIELDS.items()
    }
    values["eyebrow"] = slide.get("eyebrow", "")
    values["narration"] = normalized.get("narration", "")
    values["visual"] = normalized.get("visual") or {}
    return values


def _is_empty(name: str, value: Any) -> bool:
    if name == "visual":
        return (value or {}).get("kind", "none") == "none"
    if isinstance(value, dict):
        return all(_is_empty("", item) for item in value.values())
    return value in (None, "", [])


_WORDS = re.compile(r"\s*\S+\s*")


def narration_diff(before: str, after: str) -> list[dict]:
    """The narration's change word by word, as [{op: "equal" | "insert" | "delete", text}]: the equal and inserted
    parts, in order, make the new narration (the equal and deleted ones, the old one)."""
    old, new = _WORDS.findall(before or ""), _WORDS.findall(after or "")
    matcher = difflib.SequenceMatcher(None, [w.strip() for w in old], [w.strip() for w in new], autojunk=False)
    parts: list[dict] = []

    def add(op: str, text: str) -> None:
        if not text:
            return
        if parts and parts[-1]["op"] == op:
            parts[-1]["text"] += text
        else:
            parts.append({"op": op, "text": text})

    for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        if tag == "equal":
            add("equal", "".join(new[new_start:new_end]))
        else:
            add("delete", "".join(old[old_start:old_end]))
            add("insert", "".join(new[new_start:new_end]))
    return parts


def _scene_change(
    scene_id: str, action: str, positions: tuple[int | None, int | None], was: dict | None, now: dict | None,
    reasons: dict[str, str],
) -> dict:
    old = _field_values(was) if was is not None else {}
    new = _field_values(now) if now is not None else {}
    fields = {}
    for name in DIFF_FIELDS:
        before, after = old.get(name), new.get(name)
        if was is None and _is_empty(name, after) or now is None and _is_empty(name, before):
            continue  # a scene added or removed: only what it shows
        if was is not None and now is not None and before == after:
            continue
        fields[name] = {"before": before, "after": after}
    return {
        "scene_id": scene_id,
        "action": action,
        "position": {"before": positions[0], "after": positions[1]},
        "fields": fields,
        "reason": reasons.get(scene_id, ""),
        "narration_diff": narration_diff(old.get("narration", ""), new.get("narration", ""))
        if "narration" in fields else [],
        "before": was,
        "after": now,
    }


def scene_changes(before: list[dict], after: list[dict], reasons: dict[str, str] | None = None) -> list[dict]:
    """Every scene that changed from `before` to `after`, in the script's order: updated (and maybe moved), only
    moved, added or removed, each with its fields before and after and its narration word by word."""
    reasons = reasons or {}
    old = {scene.get("id"): (position, scene) for position, scene in enumerate(before, start=1)}
    new = {scene.get("id"): (position, scene) for position, scene in enumerate(after, start=1)}
    moved = _moved(list(old), list(new))
    changes = []
    for scene_id, (position, scene) in new.items():
        if scene_id not in old:
            changes.append(_scene_change(scene_id, "insert", (None, position), None, scene, reasons))
            continue
        was_at, was = old[scene_id]
        same = _same(was, scene)
        if not same or scene_id in moved:
            action = "move" if same else "update"
            changes.append(_scene_change(scene_id, action, (was_at, position), was, scene, reasons))
    for scene_id, (position, scene) in old.items():
        if scene_id not in new:
            changes.append(_scene_change(scene_id, "delete", (position, None), scene, None, reasons))
    return sorted(changes, key=lambda change: (
        change["position"]["after"] or change["position"]["before"], change["action"] != "delete"
    ))


@dataclass(frozen=True)
class Production:
    """How the module's next video is made, as far as what a change costs."""

    presenters: int  # 0 (none), 1, or 2 taking turns scene by scene (see render.scene_speakers)
    has_video: bool

    @classmethod
    def of(cls, module: Module) -> "Production":
        settings = module.course.settings or {}
        presenters = 0
        if settings.get("presenter", True) and settings.get("avatar_id") and get_avatar_provider() is not None:
            presenters = 2 if settings.get("co_avatar_id") else 1
        return cls(presenters, bool(module.video_asset_id or module.video_url))


def _takes(scenes: list[dict], production: Production) -> list[tuple[str, ...]]:
    """Each presenter's take as it is recorded: the narration of the scenes it presents, in order."""
    if not production.presenters:
        return []
    speakers = [index % 2 if production.presenters == 2 else 0 for index in range(len(scenes))]
    return [
        tuple(scene.get("narration") or "" for scene, speaker in zip(scenes, speakers) if speaker == take)
        for take in sorted(set(speakers))
    ]


def _reassigns(before: list[dict], after: list[dict], production: Production) -> bool:
    """Whether a scene goes to the other presenter (they take turns by position: a scene added or removed before
    it swaps them)."""
    if production.presenters != 2:
        return False
    then = {scene.get("id"): index % 2 for index, scene in enumerate(before)}
    return any(scene.get("id") in then and then[scene.get("id")] != index % 2 for index, scene in enumerate(after))


def _made_visuals(scenes: list[dict]) -> set[str]:
    """The pictures and clips the scenes' visuals are made from (each paid for once: they are kept by request)."""
    return {request.key() for request in map(scene_request, scenes) if request.kind in GENERATED_VISUALS}


def _reordered_or_changed(before: list[dict], after: list[dict]) -> bool:
    return len(before) != len(after) or any(
        one.get("id") != other.get("id") or not _same(one, other) for one, other in zip(before, after)
    )


def impact(before: dict, after: dict, production: Production) -> dict:
    """What producing the video again takes after the change: the scenes narrated again (and their characters), the
    presenter's takes recorded again, whether scenes go to the other presenter, the pictures to make, and whether
    the video must be produced again at all (it has one, and its scenes changed)."""
    old, new = before["scenes"], after["scenes"]
    narrations = {scene.get("id"): scene.get("narration") or "" for scene in old}
    narrated = [scene for scene in new if narrations.get(scene.get("id")) != (scene.get("narration") or "")]
    old_takes, new_takes = _takes(old, production), _takes(new, production)
    return {
        "narration_scenes": [scene.get("id") for scene in narrated],
        "narration_chars": sum(len(scene.get("narration") or "") for scene in narrated),
        "presenter_takes": sum(
            1 for index, take in enumerate(new_takes) if index >= len(old_takes) or old_takes[index] != take
        ),
        "reassigns_presenters": _reassigns(old, new, production),
        "visuals_to_make": len(_made_visuals(new) - _made_visuals(old)),
        "needs_render": production.has_video and _reordered_or_changed(old, new),
    }


def describe(before: dict, after: dict, production: Production, reasons: dict[str, str] | None = None) -> dict:
    """The change from `before` to `after` as a revision keeps it (`changes`; see schemas.revisions)."""
    reading = None
    if before["reading"] != after["reading"]:
        reading = {"before": before["reading"], "after": after["reading"]}
    return {
        "scenes": scene_changes(before["scenes"], after["scenes"], reasons),
        "reading": reading,
        "impact": impact(before, after, production),
        "ignored": [],
        "affected_question_ids": [],
        "rebased": False,
    }


def is_empty(changes: dict) -> bool:
    return not changes["scenes"] and changes["reading"] is None


# ── Proposals ─────────────────────────────────────────────────────────


@dataclass
class Proposal:
    after: dict
    changes: dict


def _clip(text: str, limit: int) -> str:
    return text.strip()[:limit].strip()


class _Merge:
    """The model's changes, taken one by one over the script as stored (see `propose`)."""

    def __init__(self, scenes: list[dict], scope: list[str] | None, max_clips: int):
        self.scenes = scenes
        self.by_id = {scene.get("id"): scene for scene in scenes}
        self.scope = set(scope) if scope else None
        self.max_clips = max_clips
        self.updated: dict[str, dict] = {}
        self.deleted: set[str] = set()
        self.inserted: dict[str, list[dict]] = defaultdict(list)  # the scene they follow ("" = first) -> new scenes
        self.seen: set[str] = set()  # scenes a change was already taken for
        self.reasons: dict[str, str] = {}
        self.ignored: list[dict] = []

    def _ignore(self, change: SceneChangeDraft, reason: str) -> None:
        self.ignored.append({"scene_id": change.scene_id, "action": change.action, "reason": reason})

    def _in_scope(self, scene_id: str) -> bool:
        return self.scope is None or scene_id in self.scope

    def take(self, change: SceneChangeDraft) -> None:
        scene_id = change.scene_id.strip()
        if change.action == "insert_after":
            self._insert(change, scene_id)
            return
        if scene_id not in self.by_id:
            self._ignore(change, "Esa escena no existe en el guion.")
        elif not self._in_scope(scene_id):
            self._ignore(change, "La escena no está entre las que elegiste.")
        elif scene_id in self.seen:
            self._ignore(change, "Ya había otro cambio para esta escena.")
        elif change.action == "delete":
            self.seen.add(scene_id)
            self.deleted.add(scene_id)
            self.reasons[scene_id] = change.reason.strip()
        else:
            self.seen.add(scene_id)
            scene = self._updated(self.by_id[scene_id], change)
            if scene is None:
                self._ignore(change, "El cambio propuesto no cambiaba nada en esta escena.")
            else:
                self.updated[scene_id] = scene
                self.reasons[scene_id] = change.reason.strip()

    def _clips_left(self, besides: str | None = None) -> int:
        """Animated clips the module may still have: its limit minus those of its other scenes."""
        scenes = [self.updated.get(scene.get("id"), scene) for scene in self.scenes
                  if scene.get("id") not in self.deleted and scene.get("id") != besides]
        scenes += [scene for added in self.inserted.values() for scene in added]
        return self.max_clips - sum((scene.get("visual") or {}).get("kind") == "clip" for scene in scenes)

    def _insert(self, change: SceneChangeDraft, after_id: str) -> None:
        if after_id and after_id not in self.by_id:
            self._ignore(change, "La escena después de la que iba la nueva no existe.")
        elif not self._in_scope(after_id or self.scenes[0].get("id")):
            self._ignore(change, "La escena nueva iba fuera de las escenas que elegiste.")
        elif not change.scene.narration.strip():
            self._ignore(change, "La escena nueva no tenía narración.")
        else:
            draft = change.scene
            scene = {
                "id": f"s{uuid.uuid4().hex[:8]}", "slide": designer.to_slide(draft).model_dump(),
                "narration": _clip(draft.narration, MAX_NARRATION_CHARS),
                "visual": designer.to_visual(draft, self._clips_left()).model_dump(),
            }
            self.inserted[after_id].append(scene)
            self.reasons[scene["id"]] = change.reason.strip()

    def _updated(self, old: dict, change: SceneChangeDraft) -> dict | None:
        """The scene with the fields the change names, or None when that changes nothing."""
        fields = set(change.fields)
        try:
            base = designer.draft_of(old)
        except ValueError:  # stored by hand into something the slides don't accept: left as it is
            return None
        slide = dict(old.get("slide") or {})
        layout = slide.get("layout")
        if "layout" in fields and change.scene.layout != layout:  # another design: the model's whole slide
            slide = {**designer.to_slide(change.scene).model_dump(), "eyebrow": slide.get("eyebrow", "")}
        else:
            # The changed fields over the scene as it is, converted together: e.g. icons alone follow its points.
            named = [name for name in DRAFT_FIELDS if name in fields]
            with_icons = ("icons",) if layout in WITH_ICONS else ()
            values = {
                attribute: getattr(change.scene, attribute)
                for name in named for attribute in DRAFT_FIELDS[name] + (with_icons if name == "points" else ())
            }
            converted = designer.to_slide(base.model_copy(update=values)).model_dump()
            for name in named:
                keys = SLIDE_FIELDS[name] + (with_icons if name == "points" else ())
                slide.update({key: converted[key] for key in keys})
        narration = old.get("narration", "")
        if "narration" in fields and change.scene.narration.strip():
            narration = _clip(change.scene.narration, MAX_NARRATION_CHARS)
        visual = old.get("visual") or SceneVisual().model_dump()
        if "visual" in fields:
            visual = self._visual(visual, change, slide, old.get("id"))
        if slide["layout"] != layout:
            visual = self._layout_visual(visual, change, slide["layout"], old.get("id"))
        scene = {**old, "slide": slide, "narration": narration, "visual": visual}
        return None if _same(scene, old) else scene

    def _visual(self, visual: dict, change: SceneChangeDraft, slide: dict, scene_id: str) -> dict:
        if change.visual_action == "new_version":
            if (visual.get("kind") or "none") == "none":
                return visual  # no picture to make again
            return {**visual, "variant": (int(visual.get("variant") or 0) + 1) % 100}
        if change.visual_action == "new_prompt":  # described by the model, over the scene's slide (e.g. its title)
            draft = change.scene.model_copy(update={"layout": slide["layout"], "title": slide.get("title", "")})
            return designer.to_visual(draft, self._clips_left(besides=scene_id)).model_dump()
        return visual

    def _layout_visual(self, visual: dict, change: SceneChangeDraft, layout: str, scene_id: str) -> dict:
        """The visual a new layout takes: an infographic is the "visual" layout's content, and only its."""
        kind = visual.get("kind") or "none"
        if layout == "visual" and kind != "infographic":
            draft = change.scene.model_copy(update={"layout": "visual"})
            return designer.to_visual(draft, self._clips_left(besides=scene_id)).model_dump()
        if layout != "visual" and kind == "infographic":
            return SceneVisual().model_dump()
        return visual

    def result(self) -> list[dict]:
        scenes = list(self.inserted.get("", []))
        for scene in self.scenes:
            if scene.get("id") not in self.deleted:
                scenes.append(self.updated.get(scene.get("id"), scene))
            scenes.extend(self.inserted.get(scene.get("id"), []))
        added = [scene["id"] for group in self.inserted.values() for scene in group]
        while len(scenes) > MAX_STORYBOARD_SCENES and added:  # the last new scenes past the editor's limit
            dropped = added.pop()
            scenes = [scene for scene in scenes if scene.get("id") != dropped]
            self.reasons.pop(dropped, None)
            self.ignored.append({"scene_id": "", "action": "insert_after",
                                 "reason": f"El guion ya tiene el máximo de {MAX_STORYBOARD_SCENES} escenas."})
        if not scenes:
            raise ProposalError("La propuesta quitaba todas las escenas del guion. Pide el cambio de otra forma.")
        try:
            Storyboard.model_validate({"scenes": scenes})
        except ValidationError as exc:  # the conversions keep every field within its limits: not expected
            logger.warning("revision_unusable_script", extra={"error": str(exc)[:300]})
            raise ProposalError("La IA propuso un guion que no se puede guardar. Intenta de nuevo.") from exc
        return scenes


def propose(
    before: dict, patch: StoryboardPatchDraft, *, scene_ids: list[str] | None, production: Production,
    question_ids: list[str], max_clips: int,
) -> Proposal:
    """The script with the model's patch: the scenes (in scope) and fields it names changed, every other scene as
    stored. What it asked that can't be taken (another scene, one that doesn't exist, a second change to the same
    scene...) is left out and listed in `changes.ignored`. Raises ProposalError when nothing is left to apply."""
    merge = _Merge(before["scenes"], scene_ids, max_clips)
    for change in patch.changes:
        merge.take(change)
    scenes = merge.result()
    reading = before["reading"]
    if patch.reading_changed and patch.reading_summary.strip():
        reading = designer.plain_markdown(patch.reading_summary)[:MAX_READING_CHARS]
    after = {"scenes": scenes, "reading": reading}
    changes = describe(before, after, production, merge.reasons)
    if is_empty(changes):
        unresolved = patch.unresolved.strip()
        raise ProposalError(f"La IA no propuso cambios: {unresolved}" if unresolved else NOTHING_PROPOSED)
    known = set(question_ids)
    changes["ignored"] = merge.ignored
    changes["affected_question_ids"] = [qid for qid in dict.fromkeys(patch.affected_question_ids) if qid in known]
    return Proposal(after, changes)


# ── Applying and undoing ──────────────────────────────────────────────


def three_way(current: dict, base: dict, target: dict) -> dict:
    """
    The change from `base` to `target` made over `current` (a proposal applied after other edits: base = what it was
    made from; an undo: base = what the change left, target = what was there before it). The scenes it changes,
    adds, removes or moves and the reading, if it changes it; everything else stays as `current` has it.

    Raises Conflict when the scenes (or the reading) it changes are not what it found any more, and RevisionError
    when the script would end up empty or too long.
    """
    if content_hash(current) == content_hash(base):
        return target
    now = {scene.get("id"): scene for scene in current["scenes"]}
    old = {scene.get("id"): scene for scene in base["scenes"]}
    new = {scene.get("id"): scene for scene in target["scenes"]}
    changed = {scene_id for scene_id in old.keys() & new.keys() if not _same(old[scene_id], new[scene_id])}
    removed, added = old.keys() - new.keys(), new.keys() - old.keys()
    moved = _moved(list(old), list(new))
    # A scene is in the way when it is neither as the change found it nor as the change leaves it.
    conflicts = {scene_id for scene_id in changed if scene_id not in now
                 or not _same(now[scene_id], old[scene_id]) and not _same(now[scene_id], new[scene_id])}
    conflicts |= {scene_id for scene_id in removed if scene_id in now and not _same(now[scene_id], old[scene_id])}
    conflicts |= {scene_id for scene_id in added if scene_id in now and not _same(now[scene_id], new[scene_id])}
    reading_changes = base["reading"] != target["reading"]
    reading_conflict = reading_changes and current["reading"] not in (base["reading"], target["reading"])
    if conflicts or reading_conflict:
        order = [scene.get("id") for scene in (*target["scenes"], *base["scenes"])]
        raise Conflict(sorted(conflicts, key=order.index), reading_conflict)

    placed = moved | added  # put where the target has them, after the scene they follow there
    scenes = []
    for scene in current["scenes"]:
        scene_id = scene.get("id")
        if scene_id not in removed and scene_id not in placed:
            scenes.append(new[scene_id] if scene_id in changed else scene)
    ids = [scene.get("id") for scene in scenes]
    order = [scene.get("id") for scene in target["scenes"]]
    for index, scene_id in enumerate(order):
        if scene_id not in placed:
            continue
        scene = new[scene_id] if scene_id in added or scene_id in changed else now.get(scene_id)
        if scene is None:
            continue  # moved by the change, but removed since
        at = next((ids.index(previous) + 1 for previous in reversed(order[:index]) if previous in ids), 0)
        scenes.insert(at, scene)
        ids.insert(at, scene_id)
    if not scenes:
        raise RevisionError("El guion quedaría sin escenas.")
    if len(scenes) > MAX_STORYBOARD_SCENES:
        raise RevisionError(f"El guion quedaría con más de {MAX_STORYBOARD_SCENES} escenas.")
    return {"scenes": scenes, "reading": target["reading"] if reading_changes else current["reading"]}


def _what(changes: dict) -> str:
    count = len(changes["scenes"])
    scenes = f"{count} escena{'s' if count != 1 else ''}"
    if changes["reading"] is None:
        return scenes
    return f"{scenes} y la lectura" if count else "la lectura"


def record(
    db: Session, module: Module, *, source: str, before: dict, after: dict, user_id: int | None,
    observation: str = "", summary: str = "",
) -> ModuleRevision | None:
    """Keep a change already made to the module's script (saved by hand, rewritten, undone) as an applied revision,
    so it can be undone too. Nothing is kept when nothing changed; the caller commits."""
    changes = describe(before, after, Production.of(module))
    if is_empty(changes):
        return None
    if not summary:
        what = _what(changes)
        summary = f"Guion reescrito con IA ({what})." if source == "regenerate" else f"Cambios a mano en {what}."
    revision = ModuleRevision(
        module_id=module.id, target="script", source=source, status="applied", observation=observation,
        scope=empty_scope(), summary=summary, base_hash=content_hash(before), before=before, after=after,
        changes=changes, created_by=user_id, applied_by=user_id, applied_at=datetime.now(timezone.utc),
    )
    db.add(revision)
    return revision


def pending_modules(db: Session, module_ids: list[int]) -> set[int]:
    """The modules with a change proposed (or being proposed) that is not applied nor discarded yet."""
    if not module_ids:
        return set()
    rows = (
        db.query(ModuleRevision.module_id)
        .filter(ModuleRevision.module_id.in_(module_ids), ModuleRevision.status.in_(PENDING))
        .distinct()
    )
    return {module_id for (module_id,) in rows}
