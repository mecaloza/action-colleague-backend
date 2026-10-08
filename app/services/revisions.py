"""
Revisions of a module's content: the changes the AI proposes from an admin's observation, previewed as a diff and
applied (or undone) on purpose, and the changes made otherwise (saved by hand, rewritten by the AI, undone), kept the
same way so any of them can be undone. This module holds what every target shares and the script's own rules; the
quiz's and the activities' are in `quiz_revisions` and `activity_revisions`.

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
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError
from sqlalchemy import delete, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import Job, Module, ModuleRevision
from app.schemas.studio import Storyboard, StoryboardScene
from app.services.ai import designer
from app.services.ai.designer import MAX_NARRATION_CHARS, MAX_STORYBOARD_SCENES, SceneChangeDraft, StoryboardPatchDraft
from app.services.slides.spec import SceneVisual
from app.services.video import cache as video_cache
from app.services.video.avatar import get_avatar_provider
from app.services.video.visuals import scene_request

logger = logging.getLogger(__name__)

PENDING = ("proposing", "proposed")  # a proposal not applied nor discarded yet
MAX_READING_CHARS = 100_000  # the module's reading (as the editor's limit)
GENERATED_VISUALS = ("image", "infographic", "clip")  # made (and paid for) from their description
NOTHING_PROPOSED = "La IA no propuso ningún cambio. Describe lo que quieres cambiar con más detalle."
NOTHING_APPLICABLE = "La IA solo propuso cambios que no se pueden aplicar."
UNCHANGED = "El cambio propuesto no cambiaba nada en esta escena."  # left out, but nothing to apply in the first place

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
    """What changed since in the places a change touches: those items (a script's scenes, a quiz's questions, an
    activity's parts) and, in a script, whether the reading."""

    def __init__(self, ids: list[str], reading: bool = False):
        super().__init__(", ".join(ids) or "reading")
        self.ids, self.reading = ids, reading


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
    """A scene as the editor saves it, every default written down: what the admin sees in each of its fields."""
    try:
        return StoryboardScene.model_validate(scene).model_dump(mode="json")
    except ValidationError:
        return scene


def _essential(scene: dict) -> dict:
    """A scene without the defaults it holds: what two versions of it are compared by, and its hash taken on. Scenes
    that only differ in defaults one of them leaves out (stored before the field existed) are the same scene, and a
    field added to the slides later, with its default, changes none of the hashes the videos keep."""
    try:
        return StoryboardScene.model_validate(scene).model_dump(mode="json", exclude_defaults=True)
    except ValidationError:
        return scene


def _same(scene: dict, other: dict) -> bool:
    return scene == other or _essential(scene) == _essential(other)


def scene_hash(scene: dict) -> str:
    return hashlib.sha256(_canonical(_essential(scene))).hexdigest()[:16]


def moved_items(before: list, after: list) -> set:
    """The items in both lists (ids) whose place among the others changed: those out of the longest runs kept in
    order."""
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
    moved = moved_items([entry["id"] for entry in shown], [scene_id for scene_id, _ in now])
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


def _equal(item: dict, other: dict) -> bool:
    return item == other


def item_changes(
    before: list[dict], after: list[dict], change: Callable[..., dict], same: Callable[[dict, dict], bool] = _equal,
    renamed: dict[str, str] | None = None,
) -> list[dict]:
    """Every item (a script's scenes, a quiz's questions: each with its "id") that changed from `before` to `after`,
    in their order: updated (and maybe moved), only moved, added or removed, each as `change(item_id, action,
    (position before, position after), before, after)` describes it. An item `renamed` ({old id: new id}: a question
    given a new id) is an update of the old one, under its old id."""
    renamed = renamed or {}
    old = {item.get("id"): (position, item) for position, item in enumerate(before, start=1)}
    new = {item.get("id"): (position, item) for position, item in enumerate(after, start=1)}
    previous = {new_id: old_id for old_id, new_id in renamed.items()
                if old_id in old and old_id not in new and new_id in new and new_id not in old}
    successors = {old_id: new_id for new_id, old_id in previous.items()}
    moved = moved_items([successors.get(item_id, item_id) for item_id in old], list(new))
    changes = []
    for item_id, (position, item) in new.items():
        was_id = previous.get(item_id, item_id)
        if was_id not in old:
            changes.append(change(item_id, "insert", (None, position), None, item))
            continue
        was_at, was = old[was_id]
        unchanged = same(was, item)
        if not unchanged or item_id in moved:
            changes.append(change(was_id, "move" if unchanged else "update", (was_at, position), was, item))
    for item_id, (position, item) in old.items():
        if item_id not in new and item_id not in successors:
            changes.append(change(item_id, "delete", (position, None), item, None))
    return sorted(changes, key=lambda each: (
        each["position"]["after"] or each["position"]["before"], each["action"] != "delete"
    ))


def scene_changes(before: list[dict], after: list[dict], reasons: dict[str, str] | None = None) -> list[dict]:
    """Every scene that changed from `before` to `after`, in the script's order: updated (and maybe moved), only
    moved, added or removed, each with its fields before and after and its narration word by word."""
    reasons = reasons or {}
    return item_changes(before, after, lambda *change: _scene_change(*change, reasons), _same)


@dataclass(frozen=True)
class Production:
    """How the module's next video is made, as far as what a change costs: who says and who presents each scene (as
    the render assigns them, from the course's choices) and what the module's earlier videos left for the next one."""

    presenters: int  # 0 (none), 1, or 2 taking turns scene by scene (see render.scene_speakers)
    has_video: bool
    voices: tuple[str, ...] = ("",)  # the voice of each who speaks: one, or two taking turns scene by scene
    avatars: tuple[str, ...] = ()  # each presenter's avatar
    engine: str = ""  # HeyGen's
    kept: video_cache.Kept = field(default_factory=video_cache.Kept)  # nothing, unless a video left it

    @classmethod
    def of(cls, module: Module) -> "Production":
        settings = module.course.settings or {}  # what its next render is asked with (studio._render_choices)
        presenter, avatar = settings.get("presenter", True), settings.get("avatar_id") or ""
        co_avatar = settings.get("co_avatar_id") or ""
        voice = settings.get("voice_id") or get_settings().elevenlabs_voice_id
        # Two presenters chosen take turns in the voices even when none can be recorded, as the render does.
        voices = (voice, settings.get("co_voice_id") or voice) if presenter and avatar and co_avatar else (voice,)
        presenters = 0
        if presenter and avatar and get_avatar_provider() is not None:
            presenters = 2 if co_avatar else 1
        return cls(
            presenters, bool(module.video_asset_id or module.video_url), voices, (avatar, co_avatar)[:presenters],
            settings.get("avatar_engine") or get_settings().heygen_engine,
            video_cache.Kept(_render_of(module).get("cache")),
        )

    def narration_keys(self, scenes: list[dict]) -> list[str]:
        """Each scene's speech as it is kept (video.cache.narration_key), in the voice that says it."""
        speakers = video_cache.scene_speakers(len(scenes), len(self.voices) == 2)
        return [
            video_cache.narration_key(self.voices[speaker], scene.get("narration") or "")
            for scene, speaker in zip(scenes, speakers)
        ]

    def take_keys(self, scenes: list[dict], digests: list[str | None]) -> list[str | None]:
        """Each presenter's take of a video of `scenes` (video.cache.take_key), from the speech clip of each scene (its
        digest); None for a take with a scene narrated anew (no clip yet: a new take, whatever it holds)."""
        speakers = video_cache.scene_speakers(len(scenes), self.presenters == 2)
        roles = video_cache.scene_roles(len(scenes))
        keys: list[str | None] = []
        for presenter in sorted(set(speakers)) if self.presenters else []:
            indexes = [index for index, speaker in enumerate(speakers) if speaker == presenter]
            clips = [digests[index] for index in indexes]
            if not all(clips):
                keys.append(None)
                continue
            avatar = self.avatars[presenter] if presenter < len(self.avatars) else ""
            keys.append(video_cache.take_key(avatar, self.engine, zip(clips, (roles[index] for index in indexes))))
        return keys


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
    """What producing the video again takes after the change, and only what will be paid: the scenes narrated anew
    (and their characters) and the presenter's takes recorded anew, those the module's earlier videos didn't leave
    (video.cache: a scene that says the same in the same voice keeps its speech, a take whose scenes all keep theirs
    is kept whole); then whether scenes go to the other presenter, the pictures to make, and whether the video must be
    produced again at all (it has one, and its scenes changed)."""
    old, new = before["scenes"], after["scenes"]
    digests = [production.kept.clip(key) for key in production.narration_keys(new)]
    narrated = [scene for scene, digest in zip(new, digests) if digest is None]
    takes = production.take_keys(new, digests)
    return {
        "narration_scenes": [scene.get("id") for scene in narrated],
        "narration_chars": sum(len(scene.get("narration") or "") for scene in narrated),
        "presenter_takes": sum(1 for key in takes if key is None or key not in production.kept.takes),
        "reassigns_presenters": _reassigns(old, new, production),
        "visuals_to_make": len(_made_visuals(new) - _made_visuals(old)),
        "needs_render": production.has_video and _reordered_or_changed(old, new),
    }


def describe(before: dict, after: dict, production: Production, reasons: dict[str, str] | None = None) -> dict:
    """The change from `before` to `after` as a revision keeps it (`changes`; see schemas.revisions)."""
    return {
        "scenes": scene_changes(before["scenes"], after["scenes"], reasons),
        "reading": {"before": before["reading"], "after": after["reading"]}
        if before["reading"] != after["reading"] else None,
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


@dataclass(frozen=True)
class MergeTexts:
    """What a merge tells the admin about the changes it leaves out, in the words of its items."""

    missing: str  # the item doesn't exist
    out_of_scope: str  # it isn't among the chosen ones
    repeated: str  # a second change to the same item
    unchanged: str  # the change changes nothing in it (no reason to tell when nothing is left to apply)
    insert_missing: str  # the item a new one was to follow doesn't exist
    insert_out_of_scope: str  # the new one goes among items that weren't chosen
    full: str  # past the limit of items
    emptied: str  # every item removed (the proposal fails)


class LeftOut(Exception):
    """A change the merge can't make, and why (for the admin)."""


class ItemMerge:
    """
    A patch's changes, taken one by one over the items as stored (a script's scenes, a quiz's questions: each with
    its "id"). A change names an item by its id (update, delete) or the one a new item follows (insert_after; "" =
    first); one about an item that doesn't exist, isn't among the chosen ones or already had a change is left out and
    listed in `ignored`, as is one the subclass can't make (`_changed` / `_added` raise LeftOut). Untouched items stay
    as stored.
    """

    id_field: str  # the change's field naming its item
    limit: int  # items at most: the last new ones past it are left out
    texts: MergeTexts

    def __init__(self, items: list[dict], scope: list[str] | None):
        self.items = items
        self.by_id = {item.get("id"): item for item in items}
        self.scope = set(scope) if scope else None
        self.updated: dict[str, dict] = {}
        self.deleted: set[str] = set()
        self.inserted: dict[str, list[dict]] = defaultdict(list)  # the item they follow ("" = first) -> new items
        self.seen: set[str] = set()  # items a change was already taken for
        self.reasons: dict[str, str] = {}
        self.ignored: list[dict] = []

    def _changed(self, old: dict, change) -> dict | None:
        """The item with the change made, or None when that changes nothing. Raises LeftOut when it can't be made."""
        raise NotImplementedError

    def _added(self, change) -> dict:
        """The new item the change adds (with its own new id). Raises LeftOut when it can't be made."""
        raise NotImplementedError

    def _ignore(self, change, reason: str) -> None:
        self.ignored.append({self.id_field: getattr(change, self.id_field), "action": change.action, "reason": reason})

    def _in_scope(self, item_id: str) -> bool:
        return self.scope is None or item_id in self.scope

    def take(self, change) -> None:
        item_id = getattr(change, self.id_field).strip()
        if change.action == "insert_after":
            self._insert(change, item_id)
            return
        if item_id not in self.by_id:
            self._ignore(change, self.texts.missing)
        elif not self._in_scope(item_id):
            self._ignore(change, self.texts.out_of_scope)
        elif item_id in self.seen:
            self._ignore(change, self.texts.repeated)
        elif change.action == "delete":
            self.seen.add(item_id)
            self.deleted.add(item_id)
            self.reasons[item_id] = change.reason.strip()
        else:
            self.seen.add(item_id)
            try:
                item = self._changed(self.by_id[item_id], change)
            except LeftOut as exc:
                self._ignore(change, str(exc))
                return
            if item is None:
                self._ignore(change, self.texts.unchanged)
            else:
                self.updated[item_id] = item
                self.reasons[item_id] = change.reason.strip()

    def _insert(self, change, after_id: str) -> None:
        if after_id and after_id not in self.by_id:
            self._ignore(change, self.texts.insert_missing)
        elif not self._in_scope(after_id or self.items[0].get("id")):
            self._ignore(change, self.texts.insert_out_of_scope)
        else:
            try:
                item = self._added(change)
            except LeftOut as exc:
                self._ignore(change, str(exc))
                return
            self.inserted[after_id].append(item)
            self.reasons[item["id"]] = change.reason.strip()

    def result(self) -> list[dict]:
        items = list(self.inserted.get("", []))
        for item in self.items:
            if item.get("id") not in self.deleted:
                items.append(self.updated.get(item.get("id"), item))
            items.extend(self.inserted.get(item.get("id"), []))
        added = [item["id"] for group in self.inserted.values() for item in group]
        while len(items) > self.limit and added:  # the last new items past the limit
            dropped = added.pop()
            items = [item for item in items if item.get("id") != dropped]
            self.reasons.pop(dropped, None)
            self.ignored.append({self.id_field: "", "action": "insert_after", "reason": self.texts.full})
        if not items:
            raise ProposalError(self.texts.emptied)
        return items


class _Merge(ItemMerge):
    """The model's changes, taken one by one over the script as stored (see `propose`)."""

    id_field, limit = "scene_id", MAX_STORYBOARD_SCENES  # the editor's limit
    texts = MergeTexts(
        missing="Esa escena no existe en el guion.",
        out_of_scope="La escena no está entre las que elegiste.",
        repeated="Ya había otro cambio para esta escena.",
        unchanged=UNCHANGED,
        insert_missing="La escena después de la que iba la nueva no existe.",
        insert_out_of_scope="La escena nueva iba fuera de las escenas que elegiste.",
        full=f"El guion ya tiene el máximo de {MAX_STORYBOARD_SCENES} escenas.",
        emptied="La propuesta quitaba todas las escenas del guion. Pide el cambio de otra forma.",
    )

    def __init__(self, scenes: list[dict], scope: list[str] | None, max_clips: int):
        super().__init__(scenes, scope)
        self.max_clips = max_clips

    def _clips_left(self, besides: str | None = None) -> int:
        """Animated clips the module may still have: its limit minus those of its other scenes."""
        scenes = [self.updated.get(scene.get("id"), scene) for scene in self.items
                  if scene.get("id") not in self.deleted and scene.get("id") != besides]
        scenes += [scene for added in self.inserted.values() for scene in added]
        return self.max_clips - sum((scene.get("visual") or {}).get("kind") == "clip" for scene in scenes)

    def _added(self, change: SceneChangeDraft) -> dict:
        draft = change.scene
        if not draft.narration.strip():
            raise LeftOut("La escena nueva no tenía narración.")
        return {
            "id": f"s{uuid.uuid4().hex[:8]}", "slide": designer.to_slide(draft).model_dump(),
            "narration": _clip(draft.narration, MAX_NARRATION_CHARS),
            "visual": designer.to_visual(draft, self._clips_left()).model_dump(),
        }

    def _changed(self, old: dict, change: SceneChangeDraft) -> dict | None:
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
        scenes = super().result()
        try:
            Storyboard.model_validate({"scenes": scenes})
        except ValidationError as exc:  # the conversions keep every field within its limits: not expected
            logger.warning("revision_unusable_script", extra={"error": str(exc)[:300]})
            raise ProposalError("La IA propuso un guion que no se puede guardar. Intenta de nuevo.") from exc
        return scenes


def nothing_to_apply(unresolved: str, reasons: list[str]) -> str:
    """Why a proposal is left with nothing to apply, for the admin: why its changes were left out (e.g. scenes other
    than the chosen ones: the scope to widen; changes that changed nothing are no reason), then what the model says
    it could not do."""
    unresolved = unresolved.strip()
    reasons = list(dict.fromkeys(reasons))
    if reasons:
        return " ".join([NOTHING_APPLICABLE, *reasons, *([unresolved] if unresolved else [])])
    return f"La IA no propuso cambios: {unresolved}" if unresolved else NOTHING_PROPOSED


def propose(
    before: dict, patch: StoryboardPatchDraft, *, scene_ids: list[str] | None, production: Production,
    question_ids: list[str], max_clips: int,
) -> Proposal:
    """The script with the model's patch: the scenes (in scope) and fields it names changed, every other scene as
    stored. What it asked that can't be taken (another scene, one that doesn't exist, a second change to the same
    scene...) is left out and listed in `changes.ignored`. Raises ProposalError, saying why, when nothing is left to
    apply."""
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
        reasons = [item["reason"] for item in merge.ignored if item["reason"] != UNCHANGED]
        raise ProposalError(nothing_to_apply(patch.unresolved, reasons))
    known = set(question_ids)
    changes["ignored"] = merge.ignored
    changes["affected_question_ids"] = [qid for qid in dict.fromkeys(patch.affected_question_ids) if qid in known]
    return Proposal(after, changes)


# ── Applying and undoing ──────────────────────────────────────────────


def merge_items(current: list[dict], base: list[dict], target: list[dict], same=_equal) -> list[dict]:
    """
    The change from `base` to `target` made over `current`, item by item (a script's scenes, a quiz's questions: each
    with its "id"). The items it changes, adds, removes or moves; every other item stays as `current` has it, and
    where. `same` says whether two versions of an item are the same.

    Raises Conflict with the items it changes that are neither as it found them nor as it leaves them.
    """
    now = {item.get("id"): item for item in current}
    old = {item.get("id"): item for item in base}
    new = {item.get("id"): item for item in target}
    changed = {item_id for item_id in old.keys() & new.keys() if not same(old[item_id], new[item_id])}
    removed, added = old.keys() - new.keys(), new.keys() - old.keys()
    moved = moved_items(list(old), list(new))
    conflicts = {item_id for item_id in changed if item_id not in now
                 or not same(now[item_id], old[item_id]) and not same(now[item_id], new[item_id])}
    conflicts |= {item_id for item_id in removed if item_id in now and not same(now[item_id], old[item_id])}
    conflicts |= {item_id for item_id in added if item_id in now and not same(now[item_id], new[item_id])}
    if conflicts:
        order = [item.get("id") for item in (*target, *base)]
        raise Conflict(sorted(conflicts, key=order.index))

    placed = moved | added  # put where the target has them, after the item they follow there
    items = []
    for item in current:
        item_id = item.get("id")
        if item_id not in removed and item_id not in placed:
            items.append(new[item_id] if item_id in changed else item)
    ids = [item.get("id") for item in items]
    order = [item.get("id") for item in target]
    for index, item_id in enumerate(order):
        if item_id not in placed:
            continue
        item = new[item_id] if item_id in added or item_id in changed else now.get(item_id)
        if item is None:
            continue  # moved by the change, but removed since
        at = next((ids.index(previous) + 1 for previous in reversed(order[:index]) if previous in ids), 0)
        items.insert(at, item)
        ids.insert(at, item_id)
    return items


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
    reading_changes = base["reading"] != target["reading"]
    reading_conflict = reading_changes and current["reading"] not in (base["reading"], target["reading"])
    try:
        scenes = merge_items(current["scenes"], base["scenes"], target["scenes"], _same)
    except Conflict as conflict:
        raise Conflict(conflict.ids, reading_conflict) from None
    if reading_conflict:
        raise Conflict([], True)
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


def applied(
    db: Session, module_id: int, *, target: str, source: str, before: dict, after: dict, changes: dict,
    user_id: int | None, summary: str, observation: str = "", activity_id: int | None = None,
) -> ModuleRevision:
    """A change already made to the module's content (a target's), kept as an applied revision so it can be undone;
    the caller commits."""
    revision = ModuleRevision(
        module_id=module_id, target=target, activity_id=activity_id, source=source, status="applied",
        observation=observation, scope=empty_scope(), summary=summary, base_hash=content_hash(before), before=before,
        after=after, changes=changes, created_by=user_id, applied_by=user_id, applied_at=datetime.now(timezone.utc),
    )
    db.add(revision)
    return revision


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
    return applied(db, module.id, target="script", source=source, before=before, after=after, changes=changes,
                   user_id=user_id, summary=summary, observation=observation)


def supersede(
    db: Session, module_id: int, statuses: tuple[str, ...] = PENDING, besides: int | None = None, *,
    target: str = "script", activity_id: int | None = None,
) -> None:
    """The module's proposals for `target` (an activity's: for that one) in `statuses` (by default, those not applied
    nor discarded yet), all but `besides`, give way to a newer change. A job still proposing one of them then leaves
    it as it is. The other targets' proposals stay."""
    conditions = [ModuleRevision.module_id == module_id, ModuleRevision.target == target,
                  ModuleRevision.status.in_(statuses)]
    if activity_id is not None:
        conditions.append(ModuleRevision.activity_id == activity_id)
    if besides is not None:
        conditions.append(ModuleRevision.id != besides)
    db.execute(update(ModuleRevision).where(*conditions).values(status="superseded"),
               execution_options={"synchronize_session": False})


def forget_activities(db: Session, activity_ids: list[int]) -> None:
    """The changes of activities about to be deleted go with them (the foreign key's cascade does it too; SQLite
    doesn't enforce it)."""
    if activity_ids:
        db.execute(delete(ModuleRevision).where(ModuleRevision.activity_id.in_(activity_ids)),
                   execution_options={"synchronize_session": False})


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
