"""
Revisions of an interactive activity (what every target shares is in `revisions`). An activity revision's content is
`{"title", "intro", "spec"}`: what is played (a change to it is a new version of the activity; its type never
changes).

The model writes the whole activity back: the server validates it as a save in the editor does (`parse_spec`) and
compares it with the stored one part by part. A part is each element of a list of its own (a conversation's
situations and their choices, an inspection's decisions and items, a simulator's controls) and every other field of
the spec whole; the admin also sees how many spoken lines would be voiced again.
"""

import json
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import Activity, ActivityAttempt, ModuleRevision
from app.services import activities as engine
from app.services import revisions
from app.services.activity_spec import SpecError, parse_spec
from app.services.ai.designer import ActivityPatchDraft

MAX_TITLE_CHARS, MAX_INTRO_CHARS = 200, 600  # as the editor's (schemas.activities)
UNREADABLE = "La IA devolvió la actividad en un formato que no se puede leer. Intenta de nuevo."
_MISSING = object()

# The fields of an activity, as the admin knows them (article, name).
FIELD_NAMES = {
    "title": ("el", "título"),
    "intro": ("la", "introducción"),
    "prompt": ("la", "instrucción"),
    "scene": ("el", "escenario"),
    "lives": ("las", "vidas"),
    "formula": ("la", "fórmula"),
    "output": ("el", "resultado"),
    "target": ("el", "objetivo"),
    "success_text": ("el", "texto del reto logrado"),
    "fail_text": ("el", "texto del reto no logrado"),
    "hint": ("la", "pista"),
    "mode": ("el", "modo"),
    "items": ("los", "pasos"),  # a quick challenge's (an inspection's items are parts of their own)
    "pairs": ("las", "parejas"),
    "seconds": ("el", "tiempo"),
}
# The elements of the lists that are parts of their own, by the list's name.
ELEMENT_NAMES = {
    "nodes": ("la", "situación"),
    "choices": ("la", "opción"),
    "verdicts": ("la", "decisión"),
    "items": ("el", "elemento"),
    "variables": ("el", "control"),
}


# ── Content ───────────────────────────────────────────────────────────


def content(activity: Activity) -> dict:
    """What an activity revision changes: what is played."""
    return {"title": activity.title, "intro": activity.intro or "", "spec": activity.spec}


def validated(activity_type: str, wanted: dict) -> dict:
    """`wanted` as it is saved, its spec validated as a save in the editor does. Raises SpecError saying why it
    can't be saved."""
    return {**wanted, "spec": parse_spec(activity_type, wanted["spec"]).model_dump()}


def write(activity: Activity, wanted: dict) -> bool:
    """Make `wanted` (validated) what the activity plays, as a save in the editor does: a change is a new version (open
    plays must start again), and the admin's (a new AI proposal never replaces it). Whether anything changed; the
    caller commits."""
    now = content(activity)
    changed = [name for name in ("title", "intro", "spec") if wanted[name] != now[name]]
    for name in changed:
        setattr(activity, name, wanted[name])
    if changed:
        activity.version += 1
        activity.source = "admin"
    return bool(changed)


def learners_mid_play(db: Session, activity: Activity) -> int:
    """The learners with a play of the activity open that can go on: a change makes them start it again."""
    now = engine.utcnow()
    plays = db.query(ActivityAttempt).filter(
        ActivityAttempt.activity_id == activity.id, ActivityAttempt.enrollment_id.isnot(None),
        ActivityAttempt.finished_at.is_(None),
    )
    return len({play.enrollment_id for play in plays if engine.resumable(activity, play, now)})


# ── Its parts ─────────────────────────────────────────────────────────


def _ident(element: Any) -> str | None:
    """The id of an element of a list of its own (a simulator's controls are named by their key)."""
    if isinstance(element, dict):
        for name in ("id", "key"):
            if isinstance(element.get(name), str):
                return element[name]
    return None


def _listed(value: Any) -> bool:
    """A list whose elements each have their own id: each of them is a part of its own."""
    return isinstance(value, list) and bool(value) and all(_ident(element) is not None for element in value)


@dataclass
class Parts:
    """A spec part by part, by path ("prompt", "nodes/inicio", "nodes/inicio/choices/precio", "variables/km")."""

    values: dict[str, Any] = field(default_factory=dict)  # what each part is compared by (without its own parts)
    whole: dict[str, Any] = field(default_factory=dict)  # each part whole (an element with the parts inside it)
    lists: dict[str, list[str]] = field(default_factory=dict)  # a list's path -> its elements' paths, in order

    @classmethod
    def of(cls, spec: dict | None) -> "Parts":
        parts = cls()

        def walk(value: dict, prefix: str) -> None:
            for key, item in value.items():
                path = f"{prefix}{key}"
                if not _listed(item):
                    parts.values[path] = parts.whole[path] = item
                    continue
                parts.lists[path] = []
                for element in item:
                    element_path = f"{path}/{_ident(element)}"
                    parts.lists[path].append(element_path)
                    parts.values[element_path] = {k: v for k, v in element.items() if not _listed(v)}
                    parts.whole[element_path] = element
                    walk({k: v for k, v in element.items() if _listed(v)}, f"{element_path}/")

        walk(spec or {}, "")
        return parts

    def name(self, path: str) -> tuple[str, str]:
        """(article, name) of a part as the admin knows it: ("la", "opción 1 de la situación 2"), ("el", "control
        «Precio»"), ("la", "fórmula")."""
        segments = path.split("/")
        if len(segments) == 1:
            return FIELD_NAMES.get(path, ("", path))
        names = []
        for depth in range(1, len(segments), 2):
            list_path, element_path = "/".join(segments[:depth]), "/".join(segments[:depth + 1])
            article, noun = ELEMENT_NAMES.get(segments[depth - 1], ("el", "elemento"))
            label = (self.whole.get(element_path) or {}).get("label")
            siblings = self.lists.get(list_path, [])
            if isinstance(label, str) and label:
                which = f"«{label}»"
            else:
                which = str(siblings.index(element_path) + 1) if element_path in siblings else segments[depth]
            names.append((article, f"{noun} {which}"))
        (article, innermost), *outer = reversed(names)
        return article, " de ".join([innermost, *(f"{a} {noun}" for a, noun in outer)])

    def label(self, path: str) -> str:
        name = self.name(path)[1]
        return name[:1].upper() + name[1:]


def _inside(path: str, elements: set[str]) -> bool:
    return any(path.startswith(f"{element}/") for element in elements)


def entity_changes(before: dict, after: dict) -> list[dict]:
    """The parts of the spec that changed from `before` to `after`, in the activity's order (those removed last):
    updated, moved among the others of their list, added or removed (whole, with the parts inside them), each with
    its name for the admin."""
    old, new = Parts.of(before), Parts.of(after)
    moved = set()
    for path, order in new.lists.items():
        moved |= revisions.moved_items(old.lists.get(path, []), order)
    changes, added, removed = [], set(), set()
    for path, value in new.values.items():
        if _inside(path, added):
            continue  # it comes with the element it is in
        if path not in old.values:
            added.add(path)
            changes.append({"path": path, "action": "insert", "label": new.label(path), "before": None,
                            "after": new.whole[path]})
        elif old.values[path] != value or path in moved:
            action = "update" if old.values[path] != value else "move"
            changes.append({"path": path, "action": action, "label": new.label(path), "before": old.values[path],
                            "after": value})
    for path in old.values:
        if path not in new.values and not _inside(path, removed):
            removed.add(path)
            changes.append({"path": path, "action": "delete", "label": old.label(path), "before": old.whole[path],
                            "after": None})
    return changes


def _lines(activity_type: str, wanted: dict) -> dict[str, str]:
    """What the narrator says in it, by line (`engine.line_keys`); nothing when it can't be played."""
    try:
        return engine.line_keys(Activity(type=activity_type, intro=wanted["intro"], spec=wanted["spec"]))
    except engine.GameError:
        return {}


def lines_changed(activity_type: str, before: dict, after: dict) -> int:
    """The spoken lines `after` has that `before` didn't say: voiced again once it is published (every other line
    keeps its clip)."""
    said = {text.strip() for text in _lines(activity_type, before).values()}
    return sum(1 for text in _lines(activity_type, after).values() if text.strip() not in said)


def describe(activity_type: str, before: dict, after: dict) -> dict:
    """The change from `before` to `after` as a revision keeps it (`changes`; see schemas.revisions)."""

    def text(name: str) -> dict | None:
        return {"before": before[name], "after": after[name]} if before[name] != after[name] else None

    return {
        "activity": {"title": text("title"), "intro": text("intro"),
                     "entities": entity_changes(before["spec"], after["spec"]),
                     "lines_changed": lines_changed(activity_type, before, after)},
        "ignored": [], "rebased": False,
    }


def is_empty(changes: dict) -> bool:
    activity = changes["activity"]
    return activity["title"] is None and activity["intro"] is None and not activity["entities"]


def places(paths: list[str], *contents: dict) -> str:
    """Where an activity changed, for the admin ("el título y la opción 1 de la situación 2"): each part named as the
    first of `contents` that has it says."""
    parts = [Parts.of(wanted["spec"]) for wanted in contents]
    names = []
    for path in paths:
        holder = next((each for each in parts if path in each.whole), parts[0])  # the title and intro: in none
        names.append(" ".join(filter(None, holder.name(path))))
    if len(names) <= 1:
        return "".join(names) or "la actividad"
    return f"{', '.join(names[:-1])} y {names[-1]}"


# ── Proposals ─────────────────────────────────────────────────────────


def propose(activity_type: str, before: dict, patch: ActivityPatchDraft) -> revisions.Proposal:
    """The activity as the model wrote it back, validated as a save in the editor would be. Raises ProposalError,
    saying why, when it can't be saved or it changes nothing."""
    try:
        spec = json.loads(patch.spec_json)
    except ValueError as exc:
        raise revisions.ProposalError(UNREADABLE) from exc
    if not isinstance(spec, dict):
        raise revisions.ProposalError(UNREADABLE)
    wanted = {"title": patch.title.strip()[:MAX_TITLE_CHARS] or before["title"],
              "intro": patch.intro.strip()[:MAX_INTRO_CHARS], "spec": spec}
    try:
        after = validated(activity_type, wanted)
    except SpecError as exc:
        raise revisions.ProposalError(f"La actividad que propuso la IA no se puede guardar: {exc}") from exc
    changes = describe(activity_type, before, after)
    if is_empty(changes):
        raise revisions.ProposalError(revisions.nothing_to_apply(patch.unresolved, []))
    return revisions.Proposal(after, changes)


# ── Applying and undoing ──────────────────────────────────────────────


def _fields(wanted: dict) -> dict[str, Any]:
    """An activity field by field: its title, its intro and each field of its spec."""
    return {"title": wanted["title"], "intro": wanted["intro"],
            **{f"spec/{key}": value for key, value in wanted["spec"].items()}}


def three_way(current: dict, base: dict, target: dict) -> dict:
    """
    The change from `base` to `target` made over `current`, field by field: the title, the intro and each field of
    the spec (all of a conversation's situations go together, all of an inspection's items...). Every other field
    stays as `current` has it. The result is validated by whoever writes it.

    Raises Conflict with the parts (paths) that changed since in the fields it changes.
    """
    if revisions.content_hash(current) == revisions.content_hash(base):
        return target
    now, old, new = _fields(current), _fields(base), _fields(target)
    merged, conflicts = dict(now), []
    for key in dict.fromkeys([*old, *new]):
        was, will = old.get(key, _MISSING), new.get(key, _MISSING)
        if was == will:
            continue  # the change leaves it as it was
        if now.get(key, _MISSING) not in (was, will):
            conflicts.append(key)
        elif will is _MISSING:
            merged.pop(key, None)
        else:
            merged[key] = will
    if conflicts:
        raise revisions.Conflict(_changed_since(base, current, conflicts))
    spec = {key.removeprefix("spec/"): value for key, value in merged.items() if key.startswith("spec/")}
    return {"title": merged["title"], "intro": merged["intro"], "spec": spec}


def _changed_since(base: dict, current: dict, fields: list[str]) -> list[str]:
    """The parts of these fields that changed from `base` to `current` (the field itself when it has no parts)."""
    changed = [change["path"] for change in entity_changes(base["spec"], current["spec"])]
    paths = []
    for name in fields:
        key = name.removeprefix("spec/")
        inside = [path for path in changed if path == key or path.startswith(f"{key}/")] if name != key else []
        paths += inside or [key]
    return paths


def record(
    db: Session, activity: Activity, *, source: str, before: dict, after: dict, user_id: int | None,
    summary: str = "",
) -> ModuleRevision | None:
    """Keep a change already made to the activity (saved by hand, undone) as an applied revision, so it can be undone
    too. Nothing is kept when nothing changed; the caller commits."""
    changes = describe(activity.type, before, after)
    if is_empty(changes):
        return None
    return revisions.applied(
        db, activity.module_id, target="activity", activity_id=activity.id, source=source, before=before, after=after,
        changes=changes, user_id=user_id, summary=summary or f"Cambios a mano en «{after['title']}».",
    )
