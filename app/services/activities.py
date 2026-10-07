"""
The game engine of the interactive activities: what a learner sees, how each move is graded, when a play is over.

Everything is decided here, on the server, from the activity's `spec` and the attempt's `state`. The learner only
receives what they have already earned: the current situation of a conversation (not where each choice leads), the
zones they found (not the others), the pairs they matched. Choices and items travel as opaque tokens, different for
every attempt, so a token copied from someone else's play means nothing.

`start` builds a fresh state; `view` shows it; `move` grades one play; `finish` closes the attempt. Narration lines
are named by keys (`line_keys`) that the voice job and the learner's player share.
"""

import hashlib
import hmac
import json
import random
from dataclasses import dataclass
from functools import lru_cache
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.db.models import Activity, ActivityAttempt
from app.services.activity_spec import (
    DecisionSpec,
    InspectionSpec,
    QuickSpec,
    SimulatorSpec,
    Spec,
    SpecError,
    parse_spec,
)
from app.services import formula
from app.services.media import MediaResolver

PROBLEM_POINTS = 10  # inspection: a problem found
DISTRACTOR_PENALTY = 3  # inspection: a sound zone taken for a problem
MISS_PENALTY = 1  # inspection: a tap on nothing
SIMULATOR_POINTS = 100  # simulator: the target met at the first try
SIMULATOR_TRY_COST = 10  # simulator: each extra try
SIMULATOR_MIN_POINTS = 50
SIMULATOR_MAX_TRIES = 6  # then the play ends (met or not)
QUICK_POINTS = 10  # quick: a right match or step
QUICK_STREAK_BONUS = 2  # quick: per right answer in a row after the first, up to QUICK_MAX_STREAK_BONUS
QUICK_MAX_STREAK_BONUS = 10
QUICK_WRONG_PENALTY = 2
QUICK_GRACE_SECONDS = 3  # network delay forgiven after the deadline


class GameError(Exception):
    """A move the game can't take. `status`: 409 for a play that is over or out of date, 422 for a bad move."""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


@dataclass
class Move:
    """What one move answered: `result` for the player, `done` when the play is over."""

    result: dict[str, Any]
    done: bool


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def activity_secret(app_secret: str) -> str:
    """Key for the activity tokens, derived so it is never the JWT signing key itself."""
    return hmac.new(app_secret.encode(), b"activity-tokens", hashlib.sha256).hexdigest()


def _token(secret: str, attempt_id: int, kind: str, key: str) -> str:
    message = f"activity-attempt:{attempt_id}:{kind}:{key}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()[:12]


@lru_cache(maxsize=256)
def _parsed(activity_type: str, spec_json: str) -> Spec:
    return parse_spec(activity_type, json.loads(spec_json))


def spec_of(activity: Activity) -> Spec:
    """The activity's validated spec (cached by content: a simulator's check runs once, not on every move)."""
    try:
        return _parsed(activity.type, json.dumps(activity.spec, sort_keys=True))
    except SpecError as exc:  # an activity is validated when saved: this one was edited by hand
        raise GameError(f"La actividad no se puede jugar: {exc}", status=409) from exc


def max_score(activity: Activity, untimed: bool = False) -> int:
    """The most a play can score (a quick challenge without its clock has no time bonus to win)."""
    spec = spec_of(activity)
    match spec:
        case DecisionSpec():
            return spec.max_score()
        case InspectionSpec():
            return PROBLEM_POINTS * len(spec.problems)
        case SimulatorSpec():
            return SIMULATOR_POINTS
        case QuickSpec():
            return spec.size * (QUICK_POINTS + QUICK_MAX_STREAK_BONUS) + (0 if untimed else spec.seconds // 2)


def line_keys(activity: Activity) -> dict[str, str]:
    """Every line the narrator says in this activity, by key: the intro, then each type's explanations."""
    spec = spec_of(activity)
    lines = {"intro": activity.intro} if activity.intro.strip() else {}
    match spec:
        case DecisionSpec():
            for node in spec.nodes:
                lines[f"{node.id}.situation"] = node.situation
                for choice in node.choices:
                    lines[f"{node.id}.{choice.id}"] = choice.feedback
        case InspectionSpec():
            for zone in spec.zones:
                lines[f"zone.{zone.id}"] = zone.explanation
        case SimulatorSpec():
            lines.update(success=spec.success_text, fail=spec.fail_text)
    return lines


# ── Starting and showing a play ───────────────────────────────────────


def start(activity: Activity, now: datetime | None = None, untimed: bool = False, seed: int | None = None) -> dict:
    """A fresh state for a new attempt. `untimed`: a quick challenge without its clock (and its time bonus)."""
    spec = spec_of(activity)
    state: dict[str, Any] = {"score": 0, "done": False, "seed": seed if seed is not None else random.randrange(1 << 30)}
    match spec:
        case DecisionSpec():
            state.update(node=spec.nodes[0].id, path=[])
        case InspectionSpec():
            state.update(revealed=[], found=0, misses=0)
        case SimulatorSpec():
            state.update(tries=0, met=False, last=None)
        case QuickSpec():
            deadline = None if untimed else (now or utcnow()) + timedelta(seconds=spec.seconds)
            state.update(solved=[], streak=0, best_streak=0, mistakes=0,
                         deadline=deadline.isoformat() if deadline else None)
    return state


def view(activity: Activity, attempt: ActivityAttempt, secret: str, now: datetime | None = None) -> dict:
    """What the learner sees of the play right now (also to resume it after a reload)."""
    spec, state = spec_of(activity), attempt.state
    base = {"type": activity.type, "title": activity.title, "intro": activity.intro, "score": _shown(state, attempt),
            "max_score": attempt.max_score, "done": state["done"]}
    match spec:
        case DecisionSpec():
            return {**base, "step": len(state["path"]) + 1, "path": state["path"],
                    "node": None if state["done"] else _node_view(spec, state["node"], attempt.id, secret)}
        case InspectionSpec():
            return {**base, "prompt": spec.prompt, "problems": len(spec.problems), "found": state["found"],
                    "misses": state["misses"], "max_misses": spec.max_misses,
                    "revealed": [_zone_view(zone) for zone in spec.zones if zone.id in state["revealed"]]}
        case SimulatorSpec():
            return {**base, "prompt": spec.prompt, "hint": spec.hint, "formula": spec.formula,
                    "variables": [variable.model_dump() for variable in spec.variables],
                    "output": spec.output.model_dump(), "target": spec.target.model_dump(),
                    "tries": state["tries"], "max_tries": SIMULATOR_MAX_TRIES, "met": state["met"],
                    "last": state["last"]}
        case QuickSpec():
            return {**base, **_quick_view(spec, state, attempt.id, secret, now or utcnow())}


def _node_view(spec: DecisionSpec, node_id: str, attempt_id: int, secret: str) -> dict:
    node = next(node for node in spec.nodes if node.id == node_id)
    rng = random.Random(_token(secret, attempt_id, "shuffle", node_id))  # a seed the learner can't rebuild
    choices = [{"token": _token(secret, attempt_id, f"choice:{node.id}", choice.id), "text": choice.text}
               for choice in node.choices]
    rng.shuffle(choices)  # the best answer is not always the same button
    return {"line": f"{node.id}.situation", "speaker": node.speaker, "situation": node.situation, "choices": choices}


def _zone_view(zone) -> dict:
    return {"id": zone.id, "label": zone.label, "x": zone.x, "y": zone.y, "w": zone.w, "h": zone.h,
            "is_problem": zone.is_problem, "explanation": zone.explanation, "line": f"zone.{zone.id}"}


def _quick_entries(spec: QuickSpec) -> tuple[list[str], list[str]]:
    """Order mode: the steps (in order). Match mode: the left and the right texts (pair by pair)."""
    if spec.mode == "order":
        return spec.items, []
    return [pair.left for pair in spec.pairs], [pair.right for pair in spec.pairs]


def _quick_view(spec: QuickSpec, state: dict, attempt_id: int, secret: str, now: datetime) -> dict:
    lefts, rights = _quick_entries(spec)
    rng = random.Random(state["seed"])
    cards = [{"token": _token(secret, attempt_id, "left", str(i)), "text": text, "solved": i in state["solved"]}
             for i, text in enumerate(lefts)]
    rng.shuffle(cards)
    answers = [{"token": _token(secret, attempt_id, "right", str(i)), "text": text, "solved": i in state["solved"]}
               for i, text in enumerate(rights)]
    rng.shuffle(answers)
    deadline = state["deadline"]
    remaining = None if deadline is None else max(0.0, (datetime.fromisoformat(deadline) - now).total_seconds())
    return {"mode": spec.mode, "prompt": spec.prompt, "seconds": spec.seconds, "remaining": remaining,
            "cards": cards, "answers": answers, "solved": len(state["solved"]), "total": spec.size,
            "streak": state["streak"], "mistakes": state["mistakes"],
            # order mode: the steps already placed, in order
            "placed": [lefts[i] for i in state["solved"]] if spec.mode == "order" else []}


# ── Moves ─────────────────────────────────────────────────────────────


def move(activity: Activity, attempt: ActivityAttempt, payload: dict, secret: str, now: datetime | None = None) -> Move:
    """Grade one move. The attempt's state and score are updated in place; the caller saves them."""
    _playable(activity, attempt)
    spec, state = spec_of(activity), attempt.state
    if state["done"]:
        raise GameError("Esta partida ya terminó.", status=409)
    match spec:
        case DecisionSpec():
            outcome = _decide(spec, state, payload, attempt.id, secret)
        case InspectionSpec():
            outcome = _inspect(spec, state, payload)
        case SimulatorSpec():
            outcome = _simulate(spec, state, payload)
        case QuickSpec():
            outcome = _quick(spec, state, payload, attempt.id, secret, now or utcnow())
    state["done"] = outcome.done
    attempt.score = _shown(state, attempt)
    flag_modified(attempt, "state")  # nested lists changed in place: JSON columns only notice reassignments
    return Move({**outcome.result, "score": attempt.score, "done": outcome.done}, outcome.done)


def _shown(state: dict, attempt: ActivityAttempt) -> int:
    """The score as shown: the running total keeps every penalty (an early mistake is not free), but what the
    learner sees and keeps never goes below zero nor above the maximum."""
    return max(0, min(state["score"], attempt.max_score))


def _playable(activity: Activity, attempt: ActivityAttempt) -> None:
    if attempt.finished_at is not None:
        raise GameError("Esta partida ya terminó.", status=409)
    if attempt.version != activity.version:
        raise GameError("La actividad se actualizó: vuelve a empezar.", status=409)


def _decide(spec: DecisionSpec, state: dict, payload: dict, attempt_id: int, secret: str) -> Move:
    node = next(node for node in spec.nodes if node.id == state["node"])
    token = str(payload.get("choice", ""))
    choice = next((c for c in node.choices if hmac.compare_digest(
        _token(secret, attempt_id, f"choice:{node.id}", c.id), token)), None)
    if choice is None:
        raise GameError("Esa opción no es de esta situación.")
    state["score"] += choice.points
    state["path"].append({"situation": node.situation, "choice": choice.text, "feedback": choice.feedback,
                          "points": choice.points, "line": f"{node.id}.{choice.id}"})
    if choice.next:
        state["node"] = choice.next
    result = {"choice": choice.text, "feedback": choice.feedback, "points": choice.points,
              "line": f"{node.id}.{choice.id}",
              "next": _node_view(spec, choice.next, attempt_id, secret) if choice.next else None}
    return Move(result, done=not choice.next)


def _inspect(spec: InspectionSpec, state: dict, payload: dict) -> Move:
    try:
        x, y = float(payload["x"]), float(payload["y"])
    except (KeyError, TypeError, ValueError) as exc:
        raise GameError("Indica dónde tocaste la imagen.") from exc
    if not (0 <= x <= 1 and 0 <= y <= 1):
        raise GameError("El toque cae fuera de la imagen.")
    touched = sorted((z for z in spec.zones if z.x <= x <= z.x + z.w and z.y <= y <= z.y + z.h),
                     key=lambda zone: zone.w * zone.h)  # the most precise zone under the finger
    zone = next((z for z in touched if z.id not in state["revealed"]), None)
    if zone is None and touched:
        return Move({"hit": "already", "zone": _zone_view(touched[0])}, done=False)
    if zone is None:
        state["misses"] += 1
        state["score"] -= MISS_PENALTY
        hit, shown = "none", None
    else:
        state["revealed"].append(zone.id)
        if zone.is_problem:
            state["found"] += 1
            state["score"] += PROBLEM_POINTS
            hit = "problem"
        else:
            state["misses"] += 1
            state["score"] -= DISTRACTOR_PENALTY
            hit = "distractor"
        shown = _zone_view(zone)
    done = state["found"] == len(spec.problems) or state["misses"] >= spec.max_misses
    result = {"hit": hit, "zone": shown, "found": state["found"], "misses": state["misses"]}
    if done and state["found"] < len(spec.problems):  # out of tries: show what was left, to learn from it
        result["missed"] = [_zone_view(z) for z in spec.problems if z.id not in state["revealed"]]
    return Move(result, done=done)


def _simulate(spec: SimulatorSpec, state: dict, payload: dict) -> Move:
    raw = payload.get("values")
    if not isinstance(raw, dict):
        raise GameError("Faltan los valores de los controles.")
    try:
        values = spec.clamp({key: float(value) for key, value in raw.items()})
    except (TypeError, ValueError) as exc:
        raise GameError("Los valores de los controles deben ser números.") from exc
    try:
        result = spec.result(values)
    except formula.FormulaError as exc:  # e.g. a division by zero on a value the save-time check didn't sample
        raise GameError(str(exc)) from exc
    state["tries"] += 1
    met = spec.target.met(result)
    if met:
        state["met"] = True
        state["score"] = max(SIMULATOR_MIN_POINTS, SIMULATOR_POINTS - SIMULATOR_TRY_COST * (state["tries"] - 1))
    state["last"] = {"values": values, "result": result, "met": met}
    done = met or state["tries"] >= SIMULATOR_MAX_TRIES
    return Move({"values": values, "result": result, "met": met, "tries": state["tries"],
                 "text": spec.success_text if met else spec.fail_text, "line": "success" if met else "fail"},
                done=done)


def _quick(spec: QuickSpec, state: dict, payload: dict, attempt_id: int, secret: str, now: datetime) -> Move:
    lefts, _ = _quick_entries(spec)
    if state["deadline"] and now > datetime.fromisoformat(state["deadline"]) + timedelta(seconds=QUICK_GRACE_SECONDS):
        return Move({"correct": False, "timeout": True, "solved": len(state["solved"])}, done=True)

    def index_of(kind: str, token: object) -> int | None:
        return next((i for i in range(len(lefts)) if hmac.compare_digest(
            _token(secret, attempt_id, kind, str(i)), str(token))), None)

    card = index_of("left", payload.get("card"))
    if card is None:
        raise GameError("Esa tarjeta no es de este reto.")
    if card in state["solved"]:
        return Move({"correct": True, "repeated": True, "solved": len(state["solved"])}, done=False)
    if spec.mode == "order":
        correct = card == len(state["solved"])  # the next step, in order
    else:
        answer = index_of("right", payload.get("answer"))
        if answer is None:
            raise GameError("Esa respuesta no es de este reto.")
        correct = answer == card
    if correct:
        state["solved"].append(card)
        state["streak"] += 1
        state["best_streak"] = max(state["best_streak"], state["streak"])
        bonus = min(QUICK_MAX_STREAK_BONUS, QUICK_STREAK_BONUS * (state["streak"] - 1))
        state["score"] += QUICK_POINTS + bonus
    else:
        state["streak"] = 0
        state["mistakes"] += 1
        state["score"] -= QUICK_WRONG_PENALTY
        bonus = 0
    done = len(state["solved"]) == spec.size
    result = {"correct": correct, "bonus": bonus, "streak": state["streak"], "solved": len(state["solved"])}
    if done and state["deadline"]:  # the time left, as points
        left = max(0.0, (datetime.fromisoformat(state["deadline"]) - now).total_seconds())
        state["score"] += int(left) // 2
        result["time_bonus"] = int(left) // 2
    return Move(result, done=done)


# ── Finishing ─────────────────────────────────────────────────────────


def finish(activity: Activity, attempt: ActivityAttempt, now: datetime | None = None) -> dict:
    """Close a play that is over (a quick challenge also when its time ran out). Returns the final score.
    A play already over is kept even if the activity was edited since: it was graded on the version it played."""
    state = attempt.state
    if attempt.finished_at is not None:
        raise GameError("Esta partida ya terminó.", status=409)
    if not state["done"]:
        _playable(activity, attempt)
    timed_out = (activity.type == "quick" and state.get("deadline")
                 and (now or utcnow()) > datetime.fromisoformat(state["deadline"]))
    if not state["done"] and not timed_out:
        raise GameError("La partida todavía no termina.", status=409)
    state["done"] = True
    attempt.finished_at = now or utcnow()
    attempt.score = _shown(state, attempt)
    flag_modified(attempt, "state")
    return {"score": attempt.score, "max_score": attempt.max_score}


# ── Narration ─────────────────────────────────────────────────────────


def revealed_lines(data: object) -> list[str]:
    """The narration keys a response reveals: every "line" in it (the situation, a consequence, a zone...)."""
    if isinstance(data, dict):
        return [line for key, value in data.items() for line in ([value] if key == "line" else revealed_lines(value))
                if isinstance(line, str)]
    if isinstance(data, list):
        return [line for item in data for line in revealed_lines(item)]
    return []


def text_hash(text: str) -> str:
    """Which text a clip says: a clip of an older wording is never played over the new one."""
    return hashlib.sha256(text.strip().encode()).hexdigest()[:16]


def narration_urls(db: Session, activity: Activity, keys: list[str]) -> dict[str, str]:
    """Signed audio URLs for these lines (those with a clip that says their current text)."""
    lines, narration = line_keys(activity), activity.narration or {}
    clips = {key: clip.get("asset_id") for key in dict.fromkeys(keys)
             if isinstance(clip := narration.get(key), dict) and key in lines
             and clip.get("text_hash") == text_hash(lines[key])}
    media = MediaResolver(db).prepare(extra_asset_ids=clips.values())
    return {key: url for key, asset_id in clips.items() if (url := media.url(asset_id))}


def image_url(db: Session, activity: Activity) -> str | None:
    """The signed URL of the picture an inspection is played on."""
    if not activity.image_asset_id:
        return None
    return MediaResolver(db).prepare(extra_asset_ids=[activity.image_asset_id]).url(activity.image_asset_id)
