"""
Evaluation questions: canonical format, legacy normalization, learner view and grading.

Canonical (v2) questions store the answer key in a shape that cannot drift:
- single_choice: options + correct_index (covers the old "scenario" and "multiple_choice")
- true_false: correct as a real boolean
- ordering: items stored in the CORRECT order (shuffled only when presented)
- matching: pairs (left -> right); graded as a mapping, never by position
- fill_blank: accepted answers, compared without case, accents or punctuation

Learners never receive answer keys. Options/items are identified by opaque tokens derived with
HMAC from the server secret, so neither ids nor order leak the solution; grading maps them back.
"""

import hashlib
import hmac
import random
import re
import unicodedata
import uuid
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field, TypeAdapter, field_validator, model_validator

from app.services.metrics import percentage


def _new_id() -> str:
    return uuid.uuid4().hex[:10]


class _QuestionBase(BaseModel):
    id: str = Field(default_factory=_new_id, min_length=1, max_length=40)
    prompt: str = Field(min_length=1, max_length=1000)
    explanation: str = Field(default="", max_length=1500)

    @field_validator("prompt", "explanation")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()


class SingleChoiceQuestion(_QuestionBase):
    type: Literal["single_choice"] = "single_choice"
    scenario: str = Field(default="", max_length=1500)
    options: list[str] = Field(min_length=2, max_length=6)
    correct_index: int

    @model_validator(mode="after")
    def _check(self) -> "SingleChoiceQuestion":
        self.options = [option.strip() for option in self.options]
        if any(not option for option in self.options):
            raise ValueError("options cannot be empty")
        if not 0 <= self.correct_index < len(self.options):
            raise ValueError("correct_index is out of range")
        return self


class TrueFalseQuestion(_QuestionBase):
    type: Literal["true_false"] = "true_false"
    correct: bool


class OrderingQuestion(_QuestionBase):
    type: Literal["ordering"] = "ordering"
    items: list[str] = Field(min_length=2, max_length=8)  # in the correct order

    @field_validator("items")
    @classmethod
    def _check_items(cls, items: list[str]) -> list[str]:
        items = [item.strip() for item in items]
        if any(not item for item in items):
            raise ValueError("items cannot be empty")
        return items


class MatchPair(BaseModel):
    left: str = Field(min_length=1, max_length=300)
    right: str = Field(min_length=1, max_length=300)


class MatchingQuestion(_QuestionBase):
    type: Literal["matching"] = "matching"
    pairs: list[MatchPair] = Field(min_length=2, max_length=8)

    @model_validator(mode="after")
    def _check(self) -> "MatchingQuestion":
        lefts = [pair.left.strip() for pair in self.pairs]
        rights = [pair.right.strip() for pair in self.pairs]
        if len(set(lefts)) != len(lefts) or len(set(rights)) != len(rights):
            raise ValueError("matching sides must not repeat")
        return self


class FillBlankQuestion(_QuestionBase):
    type: Literal["fill_blank"] = "fill_blank"
    answers: list[str] = Field(min_length=1, max_length=5)
    hint: str = Field(default="", max_length=300)

    @field_validator("answers")
    @classmethod
    def _check_answers(cls, answers: list[str]) -> list[str]:
        answers = [answer.strip() for answer in answers if answer.strip()]
        if not answers:
            raise ValueError("at least one accepted answer is required")
        return answers


Question = Annotated[
    Union[SingleChoiceQuestion, TrueFalseQuestion, OrderingQuestion, MatchingQuestion, FillBlankQuestion],
    Field(discriminator="type"),
]
_questions_adapter = TypeAdapter(list[Question])


def validate_questions(raw: list[dict]) -> list[Question]:
    questions = _questions_adapter.validate_python(raw)
    ids = [question.id for question in questions]
    if len(set(ids)) != len(ids):
        raise ValueError("question ids must be unique")
    return questions


def dump_questions(questions: list[Question]) -> list[dict]:
    return _questions_adapter.dump_python(questions, mode="json")


# ── Legacy normalization ──────────────────────────────────────────────


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value == 1  # the old editor stored true as 1 and false as 0
    return str(value).strip().lower() in {"true", "1", "verdadero", "v", "si", "sí", "yes"}


def _legacy_choice_index(question: dict, options: list[str]) -> int:
    correct = question.get("correct", question.get("answer", 0))
    if isinstance(correct, bool):
        return 0
    if isinstance(correct, int):
        return correct if 0 <= correct < len(options) else 0
    text = str(correct).strip()
    if len(text) == 1 and text.lower() in "abcdef":
        index = ord(text.lower()) - ord("a")
        return index if index < len(options) else 0
    if text.isdecimal() and int(text) < len(options):
        return int(text)
    lowered = [option.strip().lower() for option in options]
    return lowered.index(text.lower()) if text.lower() in lowered else 0


def normalize_legacy_question(question: dict, index: int) -> dict:
    """Convert one question stored by the old app into the canonical shape (not validated yet)."""
    qtype = question.get("type") or "multiple_choice"
    qid = str(question.get("id") or f"q{index + 1}")
    explanation = str(question.get("explanation") or "")
    prompt = str(question.get("question") or question.get("text") or question.get("statement") or "").strip()

    if qtype == "true_false":
        statement = str(question.get("statement") or prompt).strip()
        return {"id": qid, "type": "true_false", "prompt": statement, "correct": _to_bool(question.get("correct")),
                "explanation": explanation}

    if qtype == "ordering":
        items = [str(item) for item in question.get("items") or []]
        order = question.get("correct_order") or list(range(len(items)))
        if sorted(order) != list(range(len(items))):
            order = list(range(len(items)))
        return {"id": qid, "type": "ordering", "prompt": prompt or "Ordena los pasos",
                "items": [items[i] for i in order], "explanation": explanation}

    if qtype == "matching":
        pairs = [{"left": str(p.get("left", "")), "right": str(p.get("right", ""))}
                 for p in question.get("pairs") or [] if isinstance(p, dict)]
        return {"id": qid, "type": "matching", "prompt": prompt or "Empareja los conceptos", "pairs": pairs,
                "explanation": explanation}

    if qtype == "fill_blank":
        return {"id": qid, "type": "fill_blank", "prompt": prompt, "answers": [str(question.get("answer", ""))],
                "hint": str(question.get("hint") or ""), "explanation": explanation}

    options = [str(option) for option in question.get("options") or []]
    return {"id": qid, "type": "single_choice", "prompt": prompt, "scenario": str(question.get("scenario") or ""),
            "options": options, "correct_index": _legacy_choice_index(question, options), "explanation": explanation}


def _valid_question(raw: Any) -> "Question | None":
    """`raw` as a canonical question, or None when it cannot be made valid."""
    try:
        return _questions_adapter.validate_python([raw])[0]
    except ValueError:
        return None


def normalize_legacy_questions(raw: list) -> list[Question]:
    """Best effort: questions that cannot be made valid are dropped rather than breaking the quiz."""
    questions: list[Question] = []
    seen: set[str] = set()
    for index, item in enumerate(raw or []):
        if not isinstance(item, dict):
            continue
        candidate = normalize_legacy_question(item, index)
        if candidate["id"] in seen:
            # Deterministic: the learner's quiz and the grading must derive the same id.
            candidate = {**candidate, "id": f"{candidate['id']}-{index + 1}"}
        question = _valid_question(candidate)
        if question is not None:
            seen.add(question.id)
            questions.append(question)
    return questions


def normalize_stored_questions(raw: list) -> list[Question]:
    """Canonical questions saved earlier; one invalid question must not break the whole quiz."""
    questions: list[Question] = []
    seen: set[str] = set()
    for item in raw or []:
        question = _valid_question(item)
        if question is not None and question.id not in seen:
            seen.add(question.id)
            questions.append(question)
    return questions


def evaluation_questions(evaluation) -> list[Question]:
    """Questions of an evaluation: the canonical spec, or the previous app's format normalized."""
    if evaluation is None:
        return []
    if evaluation.spec:
        return normalize_stored_questions(evaluation.spec)
    return normalize_legacy_questions(evaluation.questions)


# Older rows can hold NULL (or 0) for these; the defaults are the ones of the `evaluations` columns.
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_PASSING_SCORE = 70


def max_attempts(evaluation) -> int:
    return evaluation.max_attempts or DEFAULT_MAX_ATTEMPTS


def passing_score(evaluation) -> int:
    return evaluation.passing_score or DEFAULT_PASSING_SCORE


def token_secret(app_secret: str) -> str:
    """Key for option tokens, derived so it is never the JWT signing key itself."""
    return hmac.new(app_secret.encode(), b"quiz-option-tokens", hashlib.sha256).hexdigest()


def quiz_scope(evaluation_id: int) -> str:
    return f"evaluation:{evaluation_id}"


# ── Learner view (no answers) ─────────────────────────────────────────


def _token(secret: str, scope: str, question_id: str, kind: str, index: int) -> str:
    message = f"{scope}:{question_id}:{kind}:{index}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()[:12]


def _shuffled_positions(count: int, rng: random.Random, avoid_identity: bool = False) -> list[int]:
    """Positions 0..count-1 in random order; `avoid_identity` retries so they don't come out unshuffled."""
    identity = list(range(count))
    shuffled = identity[:]
    for _ in range(10):
        rng.shuffle(shuffled)
        if not avoid_identity or shuffled != identity:
            break
    return shuffled


def learner_view(questions: list[Question], secret: str, scope: str, rng: random.Random | None = None) -> list[dict]:
    """Questions as shown to a learner: shuffled, tokenized, without answer keys."""
    rng = rng or random.Random()
    view = []
    for q in questions:
        item: dict[str, Any] = {"id": q.id, "type": q.type, "prompt": q.prompt}
        if isinstance(q, SingleChoiceQuestion):
            item["scenario"] = q.scenario
            item["options"] = [
                {"id": _token(secret, scope, q.id, "option", i), "text": q.options[i]}
                for i in _shuffled_positions(len(q.options), rng)
            ]
        elif isinstance(q, OrderingQuestion):
            item["items"] = [
                {"id": _token(secret, scope, q.id, "item", i), "text": q.items[i]}
                for i in _shuffled_positions(len(q.items), rng, avoid_identity=True)
            ]
        elif isinstance(q, MatchingQuestion):
            item["lefts"] = [
                {"id": _token(secret, scope, q.id, "left", i), "text": pair.left} for i, pair in enumerate(q.pairs)
            ]
            item["rights"] = [
                {"id": _token(secret, scope, q.id, "right", i), "text": q.pairs[i].right}
                for i in _shuffled_positions(len(q.pairs), rng, avoid_identity=True)
            ]
        elif isinstance(q, FillBlankQuestion):
            item["hint"] = q.hint
        view.append(item)
    return view


# ── Grading ───────────────────────────────────────────────────────────


def normalize_text(value: str) -> str:
    decomposed = unicodedata.normalize("NFD", value.lower())
    without_accents = "".join(c for c in decomposed if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", without_accents)).strip()


def _grade_one(q: Question, response: Any, secret: str, scope: str) -> tuple[bool, Any]:
    """Return (is_correct, correct_answer_in_learner_terms). A malformed response is simply incorrect."""
    given = response if isinstance(response, dict) else {}

    if isinstance(q, SingleChoiceQuestion):
        expected = _token(secret, scope, q.id, "option", q.correct_index)
        return given.get("option") == expected, expected

    if isinstance(q, TrueFalseQuestion):
        value = given.get("value")
        return (isinstance(value, bool) and value == q.correct), q.correct

    if isinstance(q, OrderingQuestion):
        expected = [_token(secret, scope, q.id, "item", i) for i in range(len(q.items))]
        return given.get("order") == expected, expected

    if isinstance(q, MatchingQuestion):
        expected = {
            _token(secret, scope, q.id, "left", i): _token(secret, scope, q.id, "right", i)
            for i in range(len(q.pairs))
        }
        return given.get("matches") == expected, expected

    # FillBlankQuestion
    text = given.get("text")
    accepted = {normalize_text(answer) for answer in q.answers}
    return (isinstance(text, str) and normalize_text(text) in accepted), q.answers[0]


def grade(questions: list[Question], answers: list[dict], secret: str, scope: str) -> dict:
    """
    Grade learner answers ([{question_id, response}]) and return per-question results.
    Unanswered questions count as incorrect.
    """
    by_id = {answer.get("question_id"): answer.get("response") for answer in answers if isinstance(answer, dict)}
    results = []
    for q in questions:
        correct, expected = _grade_one(q, by_id.get(q.id), secret, scope)
        results.append({"question_id": q.id, "correct": correct, "expected": expected, "explanation": q.explanation})
    total = len(questions)
    correct_count = sum(1 for result in results if result["correct"])
    return {
        "correct": correct_count,
        "total": total,
        "score": percentage(correct_count, total),
        "results": results,
    }
