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
    return {word for word in ('"points"', '"next"', '"answer"', '"feedback"', '"explanation"') if word in text}


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


# ── Inspección ────────────────────────────────────────────────────────


def test_an_inspection_reveals_each_answer_only_after_deciding():
    activity, attempt = play("inspection", INSPECTION)
    start = engine.view(activity, attempt, SECRET)
    assert [item["id"] for item in start["items"]] == ["l1", "l2", "l3", "l4"] and start["lives"] == 3
    assert start["items"][1]["facts"][0] == {"label": "Profundidad", "value": "4 mm"}
    assert secrets_in(start) == set() and all(item["outcome"] is None for item in start["items"])
    assert attempt.max_score == 10 + 12 + 14 + 16  # every answer right, in a row

    right = engine.move(activity, attempt, {"item": "l1", "verdict": "rodar"}, SECRET)
    assert right.result["correct"] and right.result["points"] == 10 and right.result["item"]["outcome"]["line"] == "item.l1"
    wrong = engine.move(activity, attempt, {"item": "l2", "verdict": "rodar"}, SECRET)
    assert not wrong.result["correct"] and wrong.result["lives"] == 2 and wrong.result["streak"] == 0
    assert wrong.result["item"]["outcome"]["answer_label"] == "Reencauchar"
    assert wrong.result["item"]["chosen"] == "rodar" and wrong.result["score"] == 10
    with pytest.raises(GameError, match="Ya decidiste") as already:
        engine.move(activity, attempt, {"item": "l1", "verdict": "retirar"}, SECRET)
    assert already.value.status == 409
    engine.move(activity, attempt, {"item": "l3", "verdict": "retirar"}, SECRET)
    last = engine.move(activity, attempt, {"item": "l4", "verdict": "rodar"}, SECRET)
    assert last.done and last.result["score"] == 10 + 10 + 12 and last.result["streak"] == 2
    assert [item["outcome"]["correct"] for item in last.result["items"]] == [True, False, True, True]


def test_an_inspection_ends_when_the_lives_run_out_showing_every_answer():
    activity, attempt = play("inspection", {**INSPECTION, "lives": 1})
    out = engine.move(activity, attempt, {"item": "l3", "verdict": "rodar"}, SECRET)
    assert out.done and out.result["lives"] == 0
    assert {item["id"]: item["outcome"]["answer"] for item in out.result["items"]} == {
        "l1": "rodar", "l2": "reencauchar", "l3": "retirar", "l4": "rodar"}
    assert engine.view(activity, attempt, SECRET)["items"][0]["outcome"]["answer"] == "rodar"  # also after a reload
    with pytest.raises(GameError, match="ya terminó"):
        engine.move(activity, attempt, {"item": "l1", "verdict": "rodar"}, SECRET)


@pytest.mark.parametrize("payload", [{}, {"item": "l9", "verdict": "rodar"}, {"item": "l1", "verdict": "vender"},
                                     {"item": ["l1"], "verdict": "rodar"}])
def test_an_inspection_move_names_an_item_and_a_verdict_of_its_own(payload):
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
    assert attempt.score == 4 * 10 + 12 + 17 - 2  # the wrong match costs its 2 points, even made first


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
    assert set(engine.line_keys(play("inspection", INSPECTION)[0])) == {"intro", "item.l1", "item.l2", "item.l3",
                                                                         "item.l4"}
    assert set(engine.line_keys(play("simulator", SIMULATOR)[0])) == {"intro", "success", "fail"}



def test_a_simulator_value_the_formula_can_not_take_is_a_bad_move_not_a_crash(monkeypatch):
    from app.services import formula
    from app.services.activity_spec import SimulatorSpec

    activity, attempt = play("simulator", SIMULATOR)

    def divides_by_zero(self, values):
        raise formula.FormulaError("La fórmula divide entre cero con algunos valores de los controles.")

    monkeypatch.setattr(SimulatorSpec, "result", divides_by_zero)
    with pytest.raises(GameError, match="divide entre cero") as error:
        engine.move(activity, attempt, {"values": {"precio": 6000, "km": 100000}}, SECRET)
    assert error.value.status == 422
