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
import json
import logging
import random
import re
import unicodedata
import uuid
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field, StringConstraints, TypeAdapter, ValidationError, field_validator, model_validator

from app.services.metrics import percentage

logger = logging.getLogger(__name__)


def _new_id() -> str:
    return uuid.uuid4().hex[:10]


# Whitespace is stripped before the length limits apply: "   " is empty, not a valid prompt.
Prompt = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
Choice = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
LongText = Annotated[str, StringConstraints(strip_whitespace=True, max_length=1500)]
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)]


def _repeats(values: list[str]) -> bool:
    return len({value.casefold() for value in values}) != len(values)


class _QuestionBase(BaseModel):
    id: str = Field(default_factory=_new_id, min_length=1, max_length=40)
    prompt: Prompt
    explanation: LongText = ""


class SingleChoiceQuestion(_QuestionBase):
    type: Literal["single_choice"] = "single_choice"
    scenario: LongText = ""
    options: list[Choice] = Field(min_length=2, max_length=6)
    correct_index: int

    @model_validator(mode="after")
    def _check(self) -> "SingleChoiceQuestion":
        # Graded by position: two options that read the same would make a right answer look wrong.
        if _repeats(self.options):
            raise ValueError("Hay opciones repetidas")
        if not 0 <= self.correct_index < len(self.options):
            raise ValueError("Marca la respuesta correcta")
        return self


class TrueFalseQuestion(_QuestionBase):
    type: Literal["true_false"] = "true_false"
    correct: bool


class OrderingQuestion(_QuestionBase):
    type: Literal["ordering"] = "ordering"
    items: list[Choice] = Field(min_length=2, max_length=8)  # in the correct order

    @field_validator("items")
    @classmethod
    def _check_items(cls, items: list[str]) -> list[str]:
        if _repeats(items):
            raise ValueError("Hay pasos repetidos")
        return items


class MatchPair(BaseModel):
    left: Choice
    right: Choice


class MatchingQuestion(_QuestionBase):
    type: Literal["matching"] = "matching"
    pairs: list[MatchPair] = Field(min_length=2, max_length=8)

    @model_validator(mode="after")
    def _check(self) -> "MatchingQuestion":
        if _repeats([pair.left for pair in self.pairs]) or _repeats([pair.right for pair in self.pairs]):
            raise ValueError("Hay elementos repetidos en las parejas")
        return self


class FillBlankQuestion(_QuestionBase):
    type: Literal["fill_blank"] = "fill_blank"
    answers: list[ShortText] = Field(min_length=1, max_length=5)
    hint: ShortText = ""

    @field_validator("answers")
    @classmethod
    def _check_answers(cls, answers: list[str]) -> list[str]:
        # Compared without punctuation: an answer like "%" would accept an empty response.
        answers = [answer for answer in answers if normalize_text(answer)]
        if not answers:
            raise ValueError("Escribe al menos una respuesta aceptada con letras o números")
        return answers


Question = Annotated[
    Union[SingleChoiceQuestion, TrueFalseQuestion, OrderingQuestion, MatchingQuestion, FillBlankQuestion],
    Field(discriminator="type"),
]
QUESTION_TYPES = ("single_choice", "true_false", "ordering", "matching", "fill_blank")
_questions_adapter = TypeAdapter(list[Question])


class InvalidQuestions(ValueError):
    """Questions rejected, with a message the admin can act on."""


_TEXTS = {
    "prompt": "el enunciado",
    "explanation": "la explicación",
    "scenario": "el caso",
    "hint": "la pista",
    "left": "la pareja",
    "right": "la pareja",
    "options": "una opción",
    "items": "un paso",
    "answers": "una respuesta aceptada",
}
_LISTS = {
    "options": ("opción", "opciones"),
    "items": ("paso", "pasos"),
    "pairs": ("pareja", "parejas"),
    "answers": ("respuesta aceptada", "respuestas aceptadas"),
}


def _count(number: Any, field: str) -> str:
    singular, plural = _LISTS[field]
    return f"{number} {singular if number == 1 else plural}"


def _describe(error: dict) -> str:
    """One pydantic error as a short Spanish sentence ("Pregunta 2: completa el enunciado")."""
    loc = [part for part in error.get("loc", ()) if part not in QUESTION_TYPES]
    position = f"Pregunta {loc[0] + 1}: " if loc and isinstance(loc[0], int) else ""
    names = [part for part in loc if isinstance(part, str)]
    field = names[-1] if names else ""
    kind, ctx = error.get("type", ""), error.get("ctx") or {}
    if kind == "value_error":
        message = str(ctx.get("error") or error.get("msg", ""))
    elif field in {"correct", "correct_index"}:
        message = "marca la respuesta correcta"
    elif kind in {"union_tag_invalid", "union_tag_not_found"}:
        message = "el tipo de pregunta no es válido"
    elif kind in {"string_too_short", "missing"} and field in _TEXTS:
        message = f"completa {_TEXTS[field]}"
    elif kind == "string_too_long" and field in _TEXTS:
        message = f"{_TEXTS[field]} supera los {ctx.get('max_length')} caracteres"
    elif kind == "too_short" and field in _LISTS:
        message = f"se necesitan al menos {_count(ctx.get('min_length'), field)}"
    elif kind == "too_long" and field in _LISTS:
        message = f"se admiten máximo {_count(ctx.get('max_length'), field)}"
    else:
        message = "hay un dato que no es válido"
    message = message[:1].lower() + message[1:] if position else message[:1].upper() + message[1:]
    return position + message


def validate_questions(raw: list[dict]) -> list[Question]:
    """Canonical questions, or InvalidQuestions with a readable reason."""
    try:
        questions = _questions_adapter.validate_python(raw)
    except ValidationError as exc:
        raise InvalidQuestions(_describe(exc.errors()[0])) from exc
    ids = [question.id for question in questions]
    if len(set(ids)) != len(ids):
        raise InvalidQuestions("Hay preguntas repetidas")
    return questions


def questions_version(questions: list[Question]) -> str:
    """Changes whenever what the learner answers changes (texts, options or keys); fixing an explanation doesn't."""
    answered = [{k: v for k, v in question.items() if k != "explanation"} for question in dump_questions(questions)]
    payload = json.dumps(answered, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


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
    """Index of the correct option; ValueError when the old data doesn't say which one it is."""
    correct = question.get("correct", question.get("answer"))
    if correct is None or isinstance(correct, bool):
        raise ValueError("no correct option")
    number = isinstance(correct, int) or (isinstance(correct, float) and correct.is_integer())
    text = str(int(correct) if number else correct).strip().lower()
    lowered = [option.strip().lower() for option in options]
    if len(text) == 1 and text in "abcdef" and not number:
        index = ord(text) - ord("a")
    elif (number or text.isdecimal()) and int(text) < len(options):
        index = int(text)
    elif text in lowered:  # the option's own text, numbers included ("30" among "15", "30", "45")
        index = lowered.index(text)
    else:
        raise ValueError("unknown correct option")
    if not 0 <= index < len(options):
        raise ValueError("correct option out of range")
    return index


def _clip(value: Any, limit: int = 300) -> str:
    """Old data had no per-item limit: clipping keeps the question (graded by token, not by text)."""
    return str(value).strip()[:limit]


def _without_repeats(options: list[str], correct_index: int) -> tuple[list[str], int]:
    """The old editor allowed repeated options: keep the first of each, pointing at the same answer."""
    kept: list[str] = []
    for option in options:
        if option.strip().casefold() not in {k.strip().casefold() for k in kept}:
            kept.append(option)
    correct = options[correct_index].strip().casefold()
    return kept, next(i for i, option in enumerate(kept) if option.strip().casefold() == correct)


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
        items = [_clip(item) for item in question.get("items") or []]
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

    options = [_clip(option) for option in question.get("options") or []]
    options, correct_index = _without_repeats(options, _legacy_choice_index(question, options))
    return {"id": qid, "type": "single_choice", "prompt": prompt, "scenario": str(question.get("scenario") or ""),
            "options": options, "correct_index": correct_index, "explanation": explanation}


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
    for index, item in enumerate(raw if isinstance(raw, list) else []):
        if not isinstance(item, dict):
            continue
        try:
            candidate = normalize_legacy_question(item, index)
        except (TypeError, ValueError, IndexError, AttributeError):
            logger.warning("legacy_question_dropped", extra={"question_index": index})
            continue
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
    try:
        raw = evaluation.questions
    except ValueError:  # questions_json that is not JSON
        logger.warning("legacy_questions_unreadable", extra={"evaluation_id": evaluation.id})
        return []
    return normalize_legacy_questions(raw)


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


def _shuffled_positions(count: int, rng: random.Random) -> list[int]:
    """Positions 0..count-1 uniformly shuffled (avoiding the correct order would give it away with 2 items)."""
    positions = list(range(count))
    rng.shuffle(positions)
    return positions


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
                for i in _shuffled_positions(len(q.items), rng)
            ]
        elif isinstance(q, MatchingQuestion):
            item["lefts"] = [
                {"id": _token(secret, scope, q.id, "left", i), "text": pair.left} for i, pair in enumerate(q.pairs)
            ]
            item["rights"] = [
                {"id": _token(secret, scope, q.id, "right", i), "text": q.pairs[i].right}
                for i in _shuffled_positions(len(q.pairs), rng)
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
