"""
Revisions of a module's quiz (what every target shares is in `revisions`). A quiz revision's content is
`{"questions": [...]}`: its questions in the canonical format (`quiz.dump_questions`), in order.

The model names the questions and the fields it changes; only those are taken from its answer, and every other
question is copied as stored. A question whose answered part changes (anything but its explanation: what
`quiz.questions_version` covers) gets a new id, so the answers given to its old wording and their statistics never
mix with the new one; a fixed explanation keeps the id, and learners answering the quiz are not affected by it.
"""

import re

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.models import Evaluation, Module, ModuleProgress, ModuleRevision
from app.services import quiz, revisions
from app.services import settings as platform
from app.services.ai import designer
from app.services.ai.designer import QuestionChangeDraft, QuestionDraft, QuizPatchDraft

MAX_QUESTIONS = 30  # a quiz's limit, as the editor's (schemas.courses.EvaluationPut)
UNCHANGED = "El cambio propuesto no cambiaba nada en esta pregunta."  # left out, but nothing to apply anyway
_POSITION = re.compile(r"^Pregunta \d+: ")  # a question validated on its own: its position says nothing
# The fields of a question a change names (designer.QuestionField), with the attributes of the model's question they
# come from ("type" brings the whole question).
DRAFT_FIELDS = {
    "prompt": ("prompt",),
    "scenario": ("scenario",),
    "options": ("options",),
    "correct": ("correct_index", "correct_bool"),
    "items": ("items",),
    "pairs": ("pairs",),
    "answers": ("answers",),
    "hint": ("hint",),
    "explanation": ("explanation",),
}


# ── Content ───────────────────────────────────────────────────────────


def content(module: Module) -> dict:
    """What a quiz revision changes: the module's questions, as the editor shows them (none without a quiz)."""
    return {"questions": quiz.dump_questions(quiz.evaluation_questions(module.evaluation))}


def write(db: Session, module: Module, questions: list[quiz.Question]) -> Evaluation:
    """Make `questions` the module's quiz as a save in the editor does: in the canonical format (the previous app's
    is superseded), keeping its attempts and passing score; a new quiz takes the platform's. The caller commits."""
    evaluation = module.evaluation
    if evaluation is None:
        defaults = platform.get_settings(db).evaluations
        evaluation = Evaluation(
            module_id=module.id, max_attempts=defaults.max_attempts, passing_score=defaults.passing_score
        )
    evaluation.spec = quiz.dump_questions(questions)
    evaluation.questions_json = "[]"
    db.add(evaluation)
    return evaluation


def changes_answers(before: dict, after: dict) -> bool:
    """Whether what learners answer changes (more than explanations): a quiz answered on the other version is
    refused when it is sent."""

    def version(questions: list[dict]) -> str:
        return quiz.questions_version(quiz.normalize_stored_questions(questions))

    return version(before["questions"]) != version(after["questions"])


def learners_mid_quiz(db: Session, module: Module) -> int:
    """The learners in the middle of the module's quiz: they tried it without passing it and still have attempts
    left (their next one is on the questions it has then)."""
    if module.evaluation is None:
        return 0
    count = db.query(func.count(ModuleProgress.id)).filter(
        ModuleProgress.module_id == module.id, ModuleProgress.attempts > 0, ModuleProgress.passed.isnot(True),
        ModuleProgress.attempts < quiz.max_attempts(module.evaluation),
    )
    return count.scalar() or 0


# ── What changed ──────────────────────────────────────────────────────


def _answered(question: dict) -> dict:
    """What the learner answers of a question: all of it but its id and its explanation."""
    return {key: value for key, value in question.items() if key not in ("id", "explanation")}


def _question_change(
    question_id: str, action: str, positions: tuple[int | None, int | None], was: dict | None, now: dict | None,
    reasons: dict[str, str],
) -> dict:
    fields = {}
    if was is not None and now is not None:
        for key in dict.fromkeys([*was, *now]):
            if key != "id" and was.get(key) != now.get(key):
                fields[key] = {"before": was.get(key), "after": now.get(key)}
    return {
        "question_id": question_id,
        "action": action,
        "position": {"before": positions[0], "after": positions[1]},
        "fields": fields,
        "reason": reasons.get(question_id, ""),
        "before": was,
        "after": now,
    }


def question_changes(
    before: list[dict], after: list[dict], reasons: dict[str, str] | None = None,
    renamed: dict[str, str] | None = None,
) -> list[dict]:
    """Every question that changed from `before` to `after`, in the quiz's order: updated (and maybe moved), only
    moved, added or removed, with the fields that changed. A question `renamed` gave a new id ({old id: new id}) is
    an update of the old one, under its old id."""
    reasons = reasons or {}
    return revisions.item_changes(before, after, lambda *change: _question_change(*change, reasons), renamed=renamed)


def describe(
    before: dict, after: dict, reasons: dict[str, str] | None = None, renamed: dict[str, str] | None = None
) -> dict:
    """The change from `before` to `after` as a revision keeps it (`changes`; see schemas.revisions)."""
    return {"questions": question_changes(before["questions"], after["questions"], reasons, renamed),
            "ignored": [], "rebased": False}


def renames_of(changes: dict | None) -> dict[str, str]:
    """The questions a described change gave a new id, {old id: new id}."""
    return {
        change["before"]["id"]: change["after"]["id"] for change in (changes or {}).get("questions", [])
        if change["action"] == "update" and change["before"]["id"] != change["after"]["id"]
    }


def reasons_of(changes: dict | None) -> dict[str, str]:
    """Why the model changed each question, as a described change says."""
    return {change["question_id"]: change.get("reason", "") for change in (changes or {}).get("questions", [])}


# ── Proposals ─────────────────────────────────────────────────────────


def _canonical(draft: QuestionDraft, question_id: str, invalid: str) -> dict:
    """The model's question in the canonical format, with that id. Raises LeftOut, `invalid` and why, when it can't
    be saved."""
    try:
        [question] = quiz.validate_questions([{**designer.question_dict(draft), "id": question_id}])
    except quiz.InvalidQuestions as exc:
        raise revisions.LeftOut(f"{invalid}: {_POSITION.sub('', str(exc)).rstrip('.')}.") from exc
    return question.model_dump(mode="json")


class _Merge(revisions.ItemMerge):
    """The model's changes, taken one by one over the quiz as stored (see `propose`)."""

    id_field, limit = "question_id", MAX_QUESTIONS
    texts = revisions.MergeTexts(
        missing="Esa pregunta no existe en la evaluación.",
        out_of_scope="La pregunta no está entre las que elegiste.",
        repeated="Ya había otro cambio para esta pregunta.",
        unchanged=UNCHANGED,
        insert_missing="La pregunta después de la que iba la nueva no existe.",
        insert_out_of_scope="La pregunta nueva iba fuera de las preguntas que elegiste.",
        full=f"La evaluación ya tiene el máximo de {MAX_QUESTIONS} preguntas.",
        emptied="La propuesta quitaba todas las preguntas de la evaluación. Pide el cambio de otra forma.",
    )

    def _changed(self, old: dict, change: QuestionChangeDraft) -> dict | None:
        fields = set(change.fields)
        if "type" in fields and change.question.type != old["type"]:  # another kind of question: the model's own
            draft = change.question
        else:  # the named fields over the question as it is
            values = {attribute: getattr(change.question, attribute)
                      for name in DRAFT_FIELDS if name in fields for attribute in DRAFT_FIELDS[name]}
            draft = designer.question_draft(old).model_copy(update=values)
        question = _canonical(draft, old["id"], "La pregunta propuesta no es válida")
        if question == old:
            return None
        if _answered(question) != _answered(old):  # another question to answer: its answers and stats start anew
            question["id"] = quiz.new_id()
        return question

    def _added(self, change: QuestionChangeDraft) -> dict:
        return _canonical(change.question, quiz.new_id(), "La pregunta nueva no es válida")

    @property
    def renamed(self) -> dict[str, str]:
        return {old_id: question["id"] for old_id, question in self.updated.items() if question["id"] != old_id}


def propose(before: dict, patch: QuizPatchDraft, *, question_ids: list[str] | None) -> revisions.Proposal:
    """The quiz with the model's patch: the questions (in scope) and fields it names changed, every other question as
    stored. What can't be taken (another question, one that doesn't exist, a question that isn't valid...) is left
    out and listed in `changes.ignored`. Raises ProposalError, saying why, when nothing is left to apply."""
    merge = _Merge(before["questions"], question_ids)
    for change in patch.changes:
        merge.take(change)
    after = {"questions": merge.result()}
    changes = describe(before, after, merge.reasons, merge.renamed)
    if not changes["questions"]:
        left_out = [item["reason"] for item in merge.ignored if item["reason"] != UNCHANGED]
        raise revisions.ProposalError(revisions.nothing_to_apply(patch.unresolved, left_out))
    changes["ignored"] = merge.ignored
    return revisions.Proposal(after, changes)


# ── Applying and undoing ──────────────────────────────────────────────


def three_way(current: dict, base: dict, target: dict, renamed: dict[str, str] | None = None) -> dict:
    """
    The change from `base` to `target` made over `current`, question by question (as `revisions.three_way` does with
    scenes). The questions it gave a new id (`renamed`: {id in base: id in target}) are merged as changes to the old
    one: an edit or a removal of it since conflicts, instead of both versions ending up in the quiz.

    Raises Conflict with the questions (their ids in `base`) it changes that changed since, and RevisionError when the
    quiz would end up empty or too long.
    """
    if revisions.content_hash(current) == revisions.content_hash(base):
        questions = target["questions"]
    else:
        renamed = renamed or {}
        old_ids = {new_id: old_id for old_id, new_id in renamed.items()}
        aliased = [{**question, "id": old_ids.get(question["id"], question["id"])} for question in target["questions"]]
        merged = revisions.merge_items(current["questions"], base["questions"], aliased)
        # A renamed question still in the quiz is the change's version of it (it changed): with its new id.
        questions = [{**question, "id": renamed[question["id"]]} if question["id"] in renamed else question
                     for question in merged]
    if not questions:
        raise revisions.RevisionError("La evaluación quedaría sin preguntas.")
    if len(questions) > MAX_QUESTIONS:
        raise revisions.RevisionError(f"La evaluación quedaría con más de {MAX_QUESTIONS} preguntas.")
    return {"questions": questions}


def record(
    db: Session, module: Module, *, source: str, before: dict, after: dict, user_id: int | None, summary: str = "",
    renamed: dict[str, str] | None = None,
) -> ModuleRevision | None:
    """Keep a change already made to the module's quiz (saved by hand, undone) as an applied revision, so it can be
    undone too. Nothing is kept when nothing changed; the caller commits."""
    changes = describe(before, after, renamed=renamed)
    if not changes["questions"]:
        return None
    if not summary:
        count = len(changes["questions"])
        summary = f"Cambios a mano en {count} pregunta{'s' if count != 1 else ''}."
    return revisions.applied(db, module.id, target="quiz", source=source, before=before, after=after,
                             changes=changes, user_id=user_id, summary=summary)
