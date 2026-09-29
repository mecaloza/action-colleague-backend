"""Question validation, legacy normalization, learner view and grading (the only grading code)."""

import json
import random

import pytest

from app.services import quiz

SECRET, SCOPE = "test-secret", "evaluation:1"


def _token(qid: str, kind: str, index: int) -> str:
    return quiz._token(SECRET, SCOPE, qid, kind, index)


def test_invalid_questions_are_rejected():
    with pytest.raises(ValueError):
        quiz.validate_questions([{"type": "single_choice", "prompt": "?", "options": ["a", "b"], "correct_index": 5}])
    with pytest.raises(ValueError):
        quiz.validate_questions([{"type": "matching", "prompt": "?", "pairs": [{"left": "a", "right": "x"}, {"left": "a", "right": "y"}]}])
    with pytest.raises(ValueError):
        quiz.validate_questions([{"id": "q", "type": "true_false", "prompt": "a", "correct": True}] * 2)


def test_legacy_true_false_saved_as_numbers_is_normalized():
    # The old editor stored "true" as 1 and "false" as 0, and the old grader compared booleans.
    questions = quiz.normalize_legacy_questions(
        [{"type": "true_false", "statement": "A", "correct": 1}, {"type": "true_false", "statement": "B", "correct": 0}]
    )
    assert [q.correct for q in questions] == [True, False]


def test_legacy_ordering_is_stored_in_the_correct_order():
    [question] = quiz.normalize_legacy_questions(
        [{"type": "ordering", "question": "Ordena", "items": ["c", "a", "b"], "correct_order": [1, 2, 0]}]
    )
    assert question.items == ["a", "b", "c"]


def test_legacy_multiple_choice_accepts_letters_and_text():
    raw = [
        {"question": "1", "options": ["x", "y", "z"], "correct": "b"},
        {"question": "2", "options": ["x", "y", "z"], "correct": "z"},
        {"question": "3", "options": ["x", "y"], "correct": 1},
    ]
    assert [q.correct_index for q in quiz.normalize_legacy_questions(raw)] == [1, 2, 1]



def test_legacy_numeric_options_and_long_texts_are_kept():
    raw = [
        {"question": "¿Días?", "options": ["15", "30", "45"], "correct": "30"},  # the option's text, not an index
        {"question": "¿Año?", "options": ["1990", "2000"], "correct": 2000},
        {"type": "scenario", "question": "¿Qué harías?", "options": ["x" * 400, "Nada"], "correct": 0},
        {"type": "ordering", "question": "Ordena", "items": ["y" * 400, "Fin"], "correct_order": [0, 1]},
    ]
    questions = quiz.normalize_legacy_questions(raw)
    assert [q.prompt for q in questions] == ["¿Días?", "¿Año?", "¿Qué harías?", "Ordena"]
    assert [q.correct_index for q in questions[:3]] == [1, 1, 0]
    assert len(questions[2].options[0]) == 300 and len(questions[3].items[0]) == 300

def test_duplicate_legacy_ids_get_deterministic_ids():
    raw = [{"id": "q1", "type": "true_false", "statement": "A", "correct": True}] * 2
    first = [q.id for q in quiz.normalize_legacy_questions(raw)]
    second = [q.id for q in quiz.normalize_legacy_questions(raw)]
    assert first == second and len(set(first)) == 2


def test_unusable_legacy_questions_are_dropped_not_fatal():
    questions = quiz.normalize_legacy_questions([{"question": "sin opciones", "options": []}, "basura", None])
    assert questions == []


def test_learner_view_hides_answer_keys_and_shuffles():
    questions = quiz.validate_questions(
        [
            {"id": "a", "type": "single_choice", "prompt": "?", "options": ["x", "y", "z"], "correct_index": 2, "explanation": "porque"},
            {"id": "b", "type": "ordering", "prompt": "?", "items": ["1", "2", "3", "4"]},
            {"id": "c", "type": "matching", "prompt": "?", "pairs": [{"left": "l1", "right": "r1"}, {"left": "l2", "right": "r2"}]},
            {"id": "d", "type": "fill_blank", "prompt": "?", "answers": ["secreto"], "hint": "s..."},
            {"id": "e", "type": "true_false", "prompt": "?", "correct": True},
        ]
    )
    view = quiz.learner_view(questions, SECRET, SCOPE, random.Random(7))
    text = json.dumps(view)
    for leaked in ("correct", "answers", "secreto", "porque", "pairs", "correct_index"):
        assert leaked not in text
    assert [item["text"] for item in view[1]["items"]] != ["1", "2", "3", "4"]
    # Tokens don't reveal positions: the correct option's token differs from its index.
    assert all(len(option["id"]) == 12 for option in view[0]["options"])


def test_grading_every_question_type():
    questions = quiz.validate_questions(
        [
            {"id": "a", "type": "single_choice", "prompt": "?", "options": ["x", "y"], "correct_index": 1},
            {"id": "b", "type": "true_false", "prompt": "?", "correct": False},
            {"id": "c", "type": "ordering", "prompt": "?", "items": ["1", "2", "3"]},
            {"id": "d", "type": "matching", "prompt": "?", "pairs": [{"left": "l1", "right": "r1"}, {"left": "l2", "right": "r2"}]},
            {"id": "e", "type": "fill_blank", "prompt": "?", "answers": ["Prevención"]},
        ]
    )
    answers = [
        {"question_id": "a", "response": {"option": _token("a", "option", 1)}},
        {"question_id": "b", "response": {"value": False}},
        {"question_id": "c", "response": {"order": [_token("c", "item", i) for i in range(3)]}},
        # Matching is graded as a mapping: the order in which pairs were made doesn't matter.
        {"question_id": "d", "response": {"matches": {_token("d", "left", 1): _token("d", "right", 1), _token("d", "left", 0): _token("d", "right", 0)}}},
        {"question_id": "e", "response": {"text": "  PREVENCION. "}},
    ]
    graded = quiz.grade(questions, answers, SECRET, SCOPE)
    assert graded["score"] == 100.0 and graded["correct"] == 5


def test_wrong_or_malformed_answers_count_as_incorrect():
    questions = quiz.validate_questions(
        [
            {"id": "a", "type": "true_false", "prompt": "?", "correct": True},
            {"id": "b", "type": "ordering", "prompt": "?", "items": ["1", "2"]},
        ]
    )
    graded = quiz.grade(
        questions,
        [{"question_id": "a", "response": {"value": 1}}, {"question_id": "b", "response": "nope"}],
        SECRET,
        SCOPE,
    )
    assert graded["correct"] == 0 and graded["score"] == 0.0


def test_evaluation_questions_prefers_the_canonical_spec():
    class Evaluation:
        spec = [{"id": "s", "type": "true_false", "prompt": "nuevo", "correct": True}, {"type": "roto"}]
        questions = [{"type": "true_false", "statement": "viejo", "correct": 0}]

    assert [q.prompt for q in quiz.evaluation_questions(Evaluation())] == ["nuevo"]
    Evaluation.spec = None
    assert [q.prompt for q in quiz.evaluation_questions(Evaluation())] == ["viejo"]
