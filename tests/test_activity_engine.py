"""The game engine: what a learner sees, how each move is graded and when a play is over."""

import copy
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.db.models import Activity, ActivityAttempt
from app.services import activities as engine
from app.services.activities import GameError
from tests.test_activity_spec import DECISION, INSPECTION, QUICK_MATCH, QUICK_ORDER, SIMULATOR

SECRET = engine.activity_secret("test-secret")
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)


def play(kind: str, spec: dict, attempt_id: int = 1, untimed: bool = False, title: str = "Actividad"):
    activity = Activity(id=1, module_id=1, type=kind, title=title, intro="Escucha y decide.",
                        spec=copy.deepcopy(spec), version=1, status="ready")
    attempt = ActivityAttempt(id=attempt_id, activity_id=1, user_id=1, version=1,
                              state=engine.start(activity, now=NOW, untimed=untimed, seed=7),
                              max_score=engine.max_score(activity), score=0)
    return activity, attempt


def secrets_in(view: dict) -> set[str]:
    """Answer keys that must never reach the learner before they act."""
    text = json.dumps(view, ensure_ascii=False)
    return {word for word in ('"points"', '"next"', '"is_problem"', '"feedback"', '"explanation"') if word in text}


# ── Decisión con cliente ──────────────────────────────────────────────


def test_a_conversation_shows_only_the_current_situation_and_scores_each_choice():
    activity, attempt = play("decision", DECISION)
    first = engine.view(activity, attempt, SECRET)
    assert first["node"]["situation"].startswith("Solo quiero") and len(first["node"]["choices"]) == 2
    assert secrets_in(first) == set()  # neither the points, nor where each choice leads, nor its consequence

    ask = next(c["token"] for c in first["node"]["choices"] if c["text"].startswith("¿Qué ruta"))
    step = engine.move(activity, attempt, {"choice": ask}, SECRET)
    assert step.result["points"] == 6 and step.result["feedback"].startswith("Entiendes")
    assert step.result["next"]["situation"].startswith("Carretera") and not step.done

    cpk = next(c["token"] for c in step.result["next"]["choices"] if "costo por kilómetro" in c["text"])
    last = engine.move(activity, attempt, {"choice": cpk}, SECRET)
    assert last.done and last.result["score"] == 10 == attempt.max_score
    assert engine.finish(activity, attempt, now=NOW) == {"score": 10, "max_score": 10}


def test_a_choice_from_another_play_or_situation_is_refused():
    activity, attempt = play("decision", DECISION, attempt_id=1)
    _, other = play("decision", DECISION, attempt_id=2)
    foreign = engine.view(activity, other, SECRET)["node"]["choices"][0]["token"]
    with pytest.raises(GameError, match="no es de esta situación"):
        engine.move(activity, attempt, {"choice": foreign}, SECRET)


# ── Inspección visual ─────────────────────────────────────────────────


def test_an_inspection_reveals_only_the_zones_tapped():
    activity, attempt = play("inspection", INSPECTION)
    start = engine.view(activity, attempt, SECRET)
    assert start["revealed"] == [] and start["problems"] == 2 and secrets_in(start) == set()

    nothing = engine.move(activity, attempt, {"x": 0.5, "y": 0.9}, SECRET)
    assert nothing.result["hit"] == "none" and nothing.result["score"] == 0  # never below zero
    sound = engine.move(activity, attempt, {"x": 0.8, "y": 0.2}, SECRET)
    assert sound.result["hit"] == "distractor" and sound.result["zone"]["explanation"].startswith("Esta llanta")
    shoulders = engine.move(activity, attempt, {"x": 0.1, "y": 0.2}, SECRET)
    assert shoulders.result["hit"] == "problem" and shoulders.result["score"] == 10
    again = engine.move(activity, attempt, {"x": 0.1, "y": 0.2}, SECRET)
    assert again.result["hit"] == "already" and again.result["score"] == 10
    center = engine.move(activity, attempt, {"x": 0.5, "y": 0.2}, SECRET)
    assert center.done and center.result["score"] == 20
    assert [z["id"] for z in engine.view(activity, attempt, SECRET)["revealed"]] == ["hombros", "centro", "parejo"]


def test_an_inspection_ends_after_too_many_misses_showing_what_was_left():
    spec = {**INSPECTION, "max_misses": 2}
    activity, attempt = play("inspection", spec)
    engine.move(activity, attempt, {"x": 0.5, "y": 0.9}, SECRET)
    last = engine.move(activity, attempt, {"x": 0.55, "y": 0.95}, SECRET)
    assert last.done and {z["id"] for z in last.result["missed"]} == {"hombros", "centro"}
    with pytest.raises(GameError, match="ya terminó"):
        engine.move(activity, attempt, {"x": 0.1, "y": 0.2}, SECRET)


@pytest.mark.parametrize("payload", [{}, {"x": "a", "y": 0}, {"x": 1.5, "y": 0.5}])
def test_an_inspection_needs_a_tap_on_the_picture(payload):
    activity, attempt = play("inspection", INSPECTION)
    with pytest.raises(GameError):
        engine.move(activity, attempt, payload, SECRET)


# ── Simulador con reto ────────────────────────────────────────────────


def test_a_simulator_checks_the_values_on_the_server_and_rewards_few_tries():
    activity, attempt = play("simulator", SIMULATOR)
    start = engine.view(activity, attempt, SECRET)
    assert start["formula"] == "precio / km" and start["target"] == {"op": "lte", "value": 0.05, "value2": 0}
    miss = engine.move(activity, attempt, {"values": {"precio": 9000, "km": 100000}}, SECRET)
    assert not miss.result["met"] and miss.result["line"] == "fail" and not miss.done
    # a client sending values off the slider gets them clamped: 1 MXN is not on the range
    hit = engine.move(activity, attempt, {"values": {"precio": 1, "km": 200000}}, SECRET)
    assert hit.result["values"] == {"precio": 4000, "km": 200000} and hit.result["met"] and hit.done
    assert hit.result["score"] == 90  # second try


def test_a_simulator_ends_after_its_tries_even_if_the_target_is_not_met():
    activity, attempt = play("simulator", SIMULATOR)
    for _ in range(engine.SIMULATOR_MAX_TRIES):
        last = engine.move(activity, attempt, {"values": {"precio": 12000, "km": 60000}}, SECRET)
    assert last.done and last.result["score"] == 0
    assert engine.finish(activity, attempt, now=NOW)["score"] == 0


# ── Reto rápido con tiempo ────────────────────────────────────────────


def _pairs(view: dict) -> dict[str, str]:
    answers = {a["text"]: a["token"] for a in view["answers"]}
    right_for = {pair["left"]: pair["right"] for pair in QUICK_MATCH["pairs"]}
    return {card["token"]: answers[right_for[card["text"]]] for card in view["cards"]}


def test_matching_scores_streaks_and_the_time_left():
    activity, attempt = play("quick", QUICK_MATCH)
    view = engine.view(activity, attempt, SECRET, now=NOW)
    assert view["remaining"] == 45 and secrets_in(view) == set()
    pairs = _pairs(view)
    cards = list(pairs)
    wrong = engine.move(activity, attempt, {"card": cards[0], "answer": pairs[cards[1]]}, SECRET, now=NOW)
    assert not wrong.result["correct"] and wrong.result["streak"] == 0
    results = [engine.move(activity, attempt, {"card": c, "answer": pairs[c]}, SECRET,
                           now=NOW + timedelta(seconds=10)).result for c in cards]
    assert [r["bonus"] for r in results] == [0, 2, 4, 6]
    assert results[-1]["time_bonus"] == 17  # 35 s left
    assert attempt.score == 4 * 10 + 12 + 17  # the wrong match cost 2 points before the score had any


def test_ordering_takes_the_steps_one_by_one():
    activity, attempt = play("quick", QUICK_ORDER, untimed=True)
    view = engine.view(activity, attempt, SECRET, now=NOW)
    assert view["remaining"] is None and view["answers"] == []
    token = {card["text"]: card["token"] for card in view["cards"]}
    early = engine.move(activity, attempt, {"card": token["Leer el DOT"]}, SECRET, now=NOW)
    assert not early.result["correct"]
    for step in QUICK_ORDER["items"]:
        last = engine.move(activity, attempt, {"card": token[step]}, SECRET, now=NOW + timedelta(minutes=10))
    assert last.done and "time_bonus" not in last.result  # untimed: no clock, no bonus
    assert engine.view(activity, attempt, SECRET)["placed"] == QUICK_ORDER["items"]


def test_a_quick_challenge_after_its_deadline_is_over():
    activity, attempt = play("quick", QUICK_MATCH)
    with pytest.raises(GameError, match="todavía no termina"):
        engine.finish(activity, attempt, now=NOW + timedelta(seconds=10))
    card = engine.view(activity, attempt, SECRET, now=NOW)["cards"][0]["token"]
    late = engine.move(activity, attempt, {"card": card, "answer": "x"}, SECRET, now=NOW + timedelta(seconds=60))
    assert late.done and late.result["timeout"]
    assert engine.finish(activity, attempt, now=NOW + timedelta(seconds=60))["score"] == 0


# ── Any activity ──────────────────────────────────────────────────────


def test_a_play_on_an_edited_activity_must_start_again():
    activity, attempt = play("decision", DECISION)
    activity.version = 2
    choice = engine.view(activity, attempt, SECRET)["node"]["choices"][0]["token"]
    with pytest.raises(GameError, match="se actualizó") as error:
        engine.move(activity, attempt, {"choice": choice}, SECRET)
    assert error.value.status == 409


def test_every_spoken_line_has_a_key():
    activity, _ = play("decision", DECISION)
    keys = engine.line_keys(activity)
    assert keys["intro"] == "Escucha y decide." and keys["inicio.situation"].startswith("Solo quiero")
    assert keys["inicio.pregunta"].startswith("Entiendes") and len(keys) == 1 + 2 + 4
    assert set(engine.line_keys(play("inspection", INSPECTION)[0])) == {"intro", "zone.hombros", "zone.centro",
                                                                         "zone.parejo"}
    assert set(engine.line_keys(play("simulator", SIMULATOR)[0])) == {"intro", "success", "fail"}
