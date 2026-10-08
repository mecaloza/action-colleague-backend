"""Changing a module's quiz or one of its activities by an observation (offline fake LLM), as its script is changed:
the AI proposes, the server keeps what it doesn't touch as stored, the admin applies or discards it, and every change
(saved by hand too) can be undone."""

import copy
import json
import os
from datetime import timedelta

import pytest

from app.db.models import Activity, ActivityAttempt, Course, Enrollment, Evaluation, Job, Module, ModuleRevision, User
from app.services import activities as engine
from app.services import activity_revisions, quiz_revisions, revisions
from app.services.ai import designer
from app.services.ai.fake import FakeLLM, FakeVoice
from app.worker import queue
from tests.conftest import auth_headers
from tests.test_activity_spec import DECISION, INSPECTION, QUICK_ORDER, SIMULATOR
from tests.test_learning_api import QUESTIONS
from tests.test_revisions import OUTLINE, READING, SCENES, _apply, _ask, _proposed, _revert, _revision

LEARN = "/api/v1/learn"
TITLE = "Cliente que solo pide precio"
INTRO = "Escucha al cliente."
PRICE = DECISION["nodes"][0]["choices"][0]  # the first choice of the first situation (the fake rewords it)


@pytest.fixture
def admin_headers(client, admin):
    return auth_headers(client, admin.email)


@pytest.fixture
def module(db, admin):
    """A published course's module with a quiz (QUESTIONS: 2 attempts, 80 to pass) and a published conversation."""
    course = Course(title="Seguridad", status="published", source="manual", settings={}, created_by=admin.id)
    db.add(course)
    db.flush()
    module = Module(course_id=course.id, title="EPP", order=1, source="text",
                    content_text="Usa siempre el casco y los guantes: el EPP es obligatorio.")
    db.add(module)
    db.flush()
    db.add(Evaluation(module_id=module.id, spec=copy.deepcopy(QUESTIONS), max_attempts=2, passing_score=80))
    db.add(Activity(module_id=module.id, order=1, type="decision", title=TITLE, intro=INTRO,
                    spec=copy.deepcopy(DECISION), status="ready"))
    db.commit()
    return module


@pytest.fixture
def activity(db, module) -> Activity:
    return db.query(Activity).filter(Activity.module_id == module.id).one()


@pytest.fixture
def learner(client, db, module, collaborator):
    db.add(Enrollment(user_id=collaborator.id, course_id=module.course_id, status="assigned", progress_pct=0))
    db.commit()
    return auth_headers(client, collaborator.email)


@pytest.fixture
def spoken(monkeypatch):
    said = []
    original = FakeVoice.speak

    def speak(self, text, voice_id, previous_text="", next_text=""):
        said.append(text)
        return original(self, text, voice_id, previous_text, next_text)

    monkeypatch.setattr(FakeVoice, "speak", speak)
    return said


def _history(client, headers, module_id, **query) -> list[dict]:
    response = client.get(f"/api/v1/modules/{module_id}/revisions", headers=headers, params=query)
    assert response.status_code == 200, response.text
    return response.json()


def _questions(db, module) -> list[dict]:
    db.expire_all()
    return quiz_revisions.content(db.get(Module, module.id))["questions"]


def _save_quiz(client, headers, module_id, edit) -> None:
    """The admin edits the quiz in the editor and saves it."""
    questions = client.get(f"/api/v1/modules/{module_id}/evaluation", headers=headers).json()["questions"]
    edit(questions)
    saved = client.put(f"/api/v1/modules/{module_id}/evaluation", headers=headers, json={"questions": questions})
    assert saved.status_code == 200, saved.text


def _stored(db, activity) -> Activity:
    db.expire_all()
    return db.get(Activity, activity.id)


def _edit_activity(client, headers, activity, edit) -> None:
    """The admin edits the activity's content in the editor and saves it."""
    listed = client.get(f"/api/v1/modules/{activity.module_id}/activities", headers=headers).json()
    spec = next(item for item in listed if item["id"] == activity.id)["spec"]
    edit(spec)
    saved = client.put(f"/api/v1/activities/{activity.id}", headers=headers, json={"spec": spec})
    assert saved.status_code == 200, saved.text


def _quiz_tried_once(client, learner, module) -> str:
    """The learner answers the quiz wrong once (one attempt left); returns the version they answered."""
    version = client.get(f"{LEARN}/modules/{module.id}/quiz", headers=learner).json()["version"]
    tried = client.post(f"{LEARN}/modules/{module.id}/quiz/attempts", headers=learner,
                        json={"answers": [], "version": version})
    assert tried.status_code == 200 and tried.json()["passed"] is False, tried.text
    return version


# ── Quiz ──────────────────────────────────────────────────────────────


def test_a_quiz_proposal_changes_only_the_questions_it_names_and_nothing_until_applied(
    client, db, admin_headers, module, worker
):
    asked = _ask(client, admin_headers, module.id, "La pregunta 2 confunde", target="quiz", question_ids=["q2"])
    assert (asked["revision"]["target"], asked["revision"]["status"]) == ("quiz", "proposing")
    assert asked["job"]["type"] == "ai.revise" and asked["revision"]["changes"] is None
    worker()

    revision = _revision(client, admin_headers, asked["revision"]["id"])
    assert revision["status"] == "proposed" and revision["summary"] and revision["unresolved"] == ""
    assert revision["scope"] == {"scene_ids": [], "question_ids": ["q2"], "qa_issue_id": None, "reverted_id": None}
    [change] = revision["changes"]["questions"]
    assert (change["question_id"], change["action"], change["position"]) == ("q2", "update", {"before": 2, "after": 2})
    assert change["fields"] == {"prompt": {"before": "El EPP es opcional", "after": "Revisado: El EPP es opcional"}}
    assert change["before"] == QUESTIONS[1] and change["reason"]
    new_id = change["after"]["id"]
    assert new_id != "q2"  # what is answered changed: the old answers and stats stay with the old question
    assert revision["changes"]["scenes"] == [] and revision["changes"]["impact"] is None
    assert revision["changes"]["activity"] is None and revision["changes"]["ignored"] == []
    assert _questions(db, module) == QUESTIONS  # nothing changes until it is applied

    applied = _apply(client, admin_headers, revision["id"])

    assert applied.status_code == 200, applied.text
    assert applied.json()["revision"]["status"] == "applied" and applied.json()["render_job"] is None
    questions = _questions(db, module)
    assert questions[0] == QUESTIONS[0] and questions[2:] == QUESTIONS[2:]  # as stored
    assert questions[1] == {**QUESTIONS[1], "id": new_id, "prompt": "Revisado: El EPP es opcional"}
    evaluation = db.query(Evaluation).filter(Evaluation.module_id == module.id).one()
    assert (evaluation.max_attempts, evaluation.passing_score, evaluation.questions_json) == (2, 80, "[]")
    history = _history(client, admin_headers, module.id, target="quiz")
    assert [(item["target"], item["source"], item["status"]) for item in history] == [
        ("quiz", "observation", "applied"),
    ]
    assert _history(client, admin_headers, module.id, target="script") == []
    # A double click on "Aplicar" gets what the first one did.
    again = _apply(client, admin_headers, revision["id"])
    assert again.status_code == 200 and _questions(db, module) == questions


def test_a_fixed_explanation_keeps_the_question_and_whoever_is_answering_it(
    client, db, admin_headers, module, worker, learner
):
    version = _quiz_tried_once(client, learner, module)
    explained = _proposed(client, admin_headers, worker, module.id, "Mejora la explicación", target="quiz",
                          question_ids=["q2"])

    [change] = explained["changes"]["questions"]
    assert change["after"]["id"] == "q2" and list(change["fields"]) == ["explanation"]
    assert explained["learners"] == {"published": True, "mid_quiz": 0, "mid_play": 0}  # nobody's answers change
    assert _apply(client, admin_headers, explained["id"]).status_code == 200
    assert client.get(f"{LEARN}/modules/{module.id}/quiz", headers=learner).json()["version"] == version

    reworded = _proposed(client, admin_headers, worker, module.id, "Más clara", target="quiz", question_ids=["q1"])

    assert reworded["learners"] == {"published": True, "mid_quiz": 1, "mid_play": 0}  # tried it, one attempt left
    assert _apply(client, admin_headers, reworded["id"]).status_code == 200
    assert _revision(client, admin_headers, reworded["id"])["learners"] is None  # applied: nothing to warn about
    stale = client.post(f"{LEARN}/modules/{module.id}/quiz/attempts", headers=learner,
                        json={"answers": [], "version": version})
    assert stale.status_code == 409  # "La evaluación cambió mientras la respondías…"


def test_question_changes_left_out_are_reported_and_new_questions_validated(
    client, db, admin_headers, module, worker, monkeypatch
):
    prompts = []

    def patch(self, prompt):
        prompts.append(prompt)
        change, question = designer.QuestionChangeDraft, FakeLLM()._question
        return designer.QuizPatchDraft(summary="Varios cambios", unresolved="", changes=[
            change(action="update", question_id="q1", fields=["options", "correct"], reason="Una opción más",
                   question=question("single_choice", options=["Gorra", "Casco", "Nada"], correct_index=1)),
            change(action="update", question_id="q3", fields=["prompt"], question=question("ordering", prompt="O"),
                   reason="x"),
            change(action="update", question_id="q9", fields=["prompt"], question=question("ordering", prompt="O"),
                   reason="x"),
            change(action="update", question_id="q1", fields=["prompt"], question=question("ordering", prompt="O"),
                   reason="x"),
            change(action="update", question_id="q2", fields=["prompt"], question=question("true_false", prompt="  "),
                   reason="x"),
            change(action="insert_after", question_id="q2", fields=[], reason="Falta el casco",
                   question=question("true_false", prompt="El casco protege la cabeza", correct_bool=True)),
            change(action="insert_after", question_id="q4", fields=[], reason="x",
                   question=question("true_false", prompt="Otra", correct_bool=True)),
            change(action="insert_after", question_id="q2", fields=[], reason="x",
                   question=question("single_choice", prompt="Sin opciones")),
        ])

    monkeypatch.setattr(FakeLLM, "_quiz_patch", patch)
    revision = _proposed(client, admin_headers, worker, module.id, target="quiz", question_ids=["q1", "q2"])

    assert "PREGUNTAS EN ALCANCE: q1, q2" in prompts[0] and "Usa siempre el casco" in prompts[0]
    changes = revision["changes"]["questions"]
    assert [(change["question_id"], change["action"]) for change in changes] == [
        ("q1", "update"), (changes[1]["question_id"], "insert"),
    ]
    assert changes[0]["after"]["options"] == ["Gorra", "Casco", "Nada"] and changes[0]["after"]["correct_index"] == 1
    assert set(changes[0]["fields"]) == {"options", "correct_index"} and changes[0]["after"]["id"] != "q1"
    assert changes[1]["position"] == {"before": None, "after": 3} and changes[1]["reason"] == "Falta el casco"
    assert [(item["question_id"], item["reason"]) for item in revision["changes"]["ignored"]] == [
        ("q3", "La pregunta no está entre las que elegiste."),
        ("q9", "Esa pregunta no existe en la evaluación."),
        ("q1", "Ya había otro cambio para esta pregunta."),
        ("q2", "La pregunta propuesta no es válida: completa el enunciado."),
        ("q4", "La pregunta nueva iba fuera de las preguntas que elegiste."),
        ("q2", "La pregunta nueva no es válida: se necesitan al menos 2 opciones."),
    ]


def test_applying_a_quiz_change_after_other_edits_rebases_or_refuses_by_question(
    client, db, admin_headers, module, worker
):
    revision = _proposed(client, admin_headers, worker, module.id, target="quiz", question_ids=["q2"])
    _save_quiz(client, admin_headers, module.id, lambda questions: questions[4].update(hint="pre..."))

    applied = _apply(client, admin_headers, revision["id"])

    assert applied.status_code == 200 and applied.json()["revision"]["changes"]["rebased"] is True
    questions = _questions(db, module)
    assert questions[4]["hint"] == "pre..." and questions[1]["prompt"] == "Revisado: El EPP es opcional"
    assert [change["question_id"] for change in applied.json()["revision"]["changes"]["questions"]] == ["q2"]

    second = _proposed(client, admin_headers, worker, module.id, "Otra", target="quiz", question_ids=["q1"])
    _save_quiz(client, admin_headers, module.id, lambda questions: questions[0].update(prompt="Editada a mano"))
    stale = _apply(client, admin_headers, second["id"])

    assert stale.status_code == 409
    assert stale.json()["detail"] == {
        "code": "revision_stale", "question_ids": ["q1"],
        "message": "La evaluación cambió en la pregunta 1 desde la propuesta; pide el cambio de nuevo.",
    }
    assert _questions(db, module)[0]["prompt"] == "Editada a mano"
    assert _revision(client, admin_headers, second["id"])["status"] == "proposed"  # still there to discard

    third = _proposed(client, admin_headers, worker, module.id, "Otra más", target="quiz", question_ids=["q3"])
    _save_quiz(client, admin_headers, module.id, lambda questions: questions.pop(2))  # that question, removed
    refused = _apply(client, admin_headers, third["id"])
    assert refused.status_code == 409 and refused.json()["detail"]["question_ids"] == ["q3"]
    assert "en la pregunta 3 desde la propuesta" in refused.json()["detail"]["message"]
    assert "q3" not in [question["id"] for question in _questions(db, module)]  # its new wording didn't come back


def test_undoing_a_quiz_change_brings_its_questions_back(client, db, admin_headers, module, worker):
    revision = _proposed(client, admin_headers, worker, module.id, target="quiz", question_ids=["q2"])
    assert _apply(client, admin_headers, revision["id"]).status_code == 200
    _save_quiz(client, admin_headers, module.id, lambda questions: questions[0].update(explanation="Editada"))

    undone = _revert(client, admin_headers, revision["id"])

    assert undone.status_code == 200, undone.text
    undo = undone.json()
    assert (undo["target"], undo["source"], undo["status"]) == ("quiz", "revert", "applied")
    assert undo["summary"].startswith("Deshace") and undo["scope"]["reverted_id"] == revision["id"]
    assert undo["scope"]["question_ids"] == ["q2"]
    [change] = undo["changes"]["questions"]
    assert change["action"] == "update" and change["after"] == QUESTIONS[1]  # with its old id
    questions = _questions(db, module)
    assert questions[1] == QUESTIONS[1] and questions[0]["explanation"] == "Editada"  # the later edit stays
    assert _revision(client, admin_headers, revision["id"])["status"] == "reverted"
    assert _revert(client, admin_headers, revision["id"]).status_code == 409

    redone = _revert(client, admin_headers, undo["id"])  # an undo can be undone

    assert redone.status_code == 200
    reworded = _questions(db, module)[1]
    assert reworded["prompt"] == "Revisado: El EPP es opcional"
    _save_quiz(client, admin_headers, module.id, lambda questions: questions[1].update(explanation="A mano"))
    refused = _revert(client, admin_headers, redone.json()["id"])
    assert refused.status_code == 409
    assert refused.json()["detail"] == {
        "code": "revision_conflict", "question_ids": [reworded["id"]],
        "message": "No se puede deshacer: la evaluación cambió después en la pregunta 2. Edítala a mano o pide otro "
                   "cambio.",
    }


def test_saving_the_quiz_by_hand_is_kept_and_can_be_undone(client, db, admin_headers, module):
    _save_quiz(client, admin_headers, module.id, lambda questions: questions[2].update(prompt="Ordena los pasos"))
    _save_quiz(client, admin_headers, module.id, lambda questions: None)  # saved again without changes: nothing new
    passing = client.put(f"/api/v1/modules/{module.id}/evaluation", headers=admin_headers,
                         json={"questions": _questions(db, module), "passing_score": 70})
    assert passing.status_code == 200  # its settings aren't questions: nothing new either

    history = _history(client, admin_headers, module.id)
    assert [(item["target"], item["source"], item["summary"]) for item in history] == [
        ("quiz", "manual", "Cambios a mano en 1 pregunta."),
    ]
    manual = _revision(client, admin_headers, history[0]["id"])
    [change] = manual["changes"]["questions"]
    assert (change["question_id"], change["after"]["id"]) == ("q3", "q3")  # the editor keeps its ids
    assert manual["created_by_name"] == "admin"
    assert _revert(client, admin_headers, manual["id"]).status_code == 200
    assert _questions(db, module) == QUESTIONS

    # A new quiz is kept too, but undoing it would leave a quiz without questions.
    other = Module(course_id=module.course_id, title="Cierre", order=2, source="text", content_text="Resumen")
    db.add(other)
    db.commit()
    created = client.put(f"/api/v1/modules/{other.id}/evaluation", headers=admin_headers,
                         json={"questions": QUESTIONS[:2]})
    assert created.status_code == 200
    [first] = _history(client, admin_headers, other.id)
    assert first["summary"] == "Cambios a mano en 2 preguntas."
    refused = _revert(client, admin_headers, first["id"])
    assert refused.status_code == 409 and refused.json()["detail"] == "La evaluación quedaría sin preguntas."


def test_deleting_the_quiz_retires_the_changes_proposed_to_it(client, db, admin_headers, module, worker):
    revision = _proposed(client, admin_headers, worker, module.id, target="quiz")
    assert client.delete(f"/api/v1/modules/{module.id}/evaluation", headers=admin_headers).status_code == 204

    assert _revision(client, admin_headers, revision["id"])["status"] == "superseded"
    assert _apply(client, admin_headers, revision["id"]).status_code == 409


def test_what_quiz_changes_can_be_asked(client, db, admin, admin_headers, module, worker):
    url = f"/api/v1/modules/{module.id}/revisions"
    unknown = client.post(url, headers=admin_headers,
                          json={"observation": "x", "target": "quiz", "question_ids": ["q9"]})
    assert unknown.status_code == 422
    assert unknown.json()["detail"] == "Alguna pregunta elegida ya no está en la evaluación; vuelve a cargarla"
    for wrong in ({"target": "quiz", "scene_ids": ["s1"]}, {"question_ids": ["q1"]},
                  {"target": "quiz", "activity_id": 1}):
        assert client.post(url, headers=admin_headers, json={"observation": "x", **wrong}).status_code == 422

    first = _ask(client, admin_headers, module.id, "Más claro", target="quiz", question_ids=["q1"])
    again = _ask(client, admin_headers, module.id, "Más claro", target="quiz", question_ids=["q1"])
    assert (again["revision"]["id"], again["job"]["id"]) == (first["revision"]["id"], first["job"]["id"])
    other = client.post(url, headers=admin_headers, json={"observation": "Otra", "target": "quiz"})
    assert other.status_code == 409
    assert other.json()["detail"] == (
        "Ya se está preparando otro cambio de esta evaluación; espera a que termine para pedir otro."
    )
    assert db.get(Job, first["job"]["id"]).dedupe_key == f"revise:quiz:{module.id}"
    [entry] = client.get(f"/api/v1/courses/{module.course_id}", headers=admin_headers).json()["modules"]
    assert entry["revision_pending"] is True
    worker()
    newer = _ask(client, admin_headers, module.id, "Pide otro ajuste", target="quiz")
    assert _revision(client, admin_headers, first["revision"]["id"])["status"] == "superseded"
    assert newer["revision"]["scope"]["question_ids"] == []  # the whole quiz


def test_the_script_and_the_quiz_have_their_own_proposals(client, db, admin, admin_headers, worker):
    course = Course(title="Llantas", status="draft", source="ai", settings={}, created_by=admin.id)
    db.add(course)
    db.flush()
    module = Module(course_id=course.id, title="Presión", order=1, source="ai", content_text=READING,
                    storyboard={"outline": OUTLINE, "scenes": copy.deepcopy(SCENES)})
    db.add(module)
    db.flush()
    db.add(Evaluation(module_id=module.id, spec=copy.deepcopy(QUESTIONS)))
    db.commit()

    script = _proposed(client, admin_headers, worker, module.id, scene_ids=["s2"])
    first = _proposed(client, admin_headers, worker, module.id, target="quiz")
    second = _proposed(client, admin_headers, worker, module.id, "Otra", target="quiz")  # "Pedir otro ajuste"

    assert _revision(client, admin_headers, script["id"])["status"] == "proposed"
    assert _revision(client, admin_headers, first["id"])["status"] == "superseded"
    assert _apply(client, admin_headers, second["id"]).status_code == 200
    assert _apply(client, admin_headers, script["id"]).status_code == 200  # applying one never retires the other
    assert {item["target"] for item in _history(client, admin_headers, module.id)} == {"script", "quiz"}


# ── Activities ────────────────────────────────────────────────────────


def test_an_activity_proposal_is_validated_and_compared_part_by_part(
    client, db, admin_headers, module, activity, worker, storage, spoken, learner
):
    assert client.post(f"/api/v1/activities/{activity.id}/narration", headers=admin_headers).status_code == 202
    worker()  # every line voiced once
    spoken.clear()
    play = client.post(f"{LEARN}/activities/{activity.id}/attempts", headers=learner, json={}).json()

    asked = _ask(client, admin_headers, module.id, "El feedback del precio es corto", target="activity",
                 activity_id=activity.id)
    assert (asked["revision"]["target"], asked["revision"]["activity_id"]) == ("activity", activity.id)
    worker()

    revision = _revision(client, admin_headers, asked["revision"]["id"])
    assert revision["status"] == "proposed" and revision["summary"]
    change = revision["changes"]["activity"]
    assert change["title"] is None and change["intro"] == {"before": INTRO, "after": f"Revisado: {INTRO}"}
    assert change["entities"] == [{
        "path": "nodes/inicio/choices/precio", "action": "update", "label": "Opción 1 de la situación 1",
        "before": PRICE, "after": {**PRICE, "feedback": f"{PRICE['feedback']} Revisado."},
    }]
    assert change["lines_changed"] == 2  # the intro and that feedback
    assert revision["changes"]["questions"] == [] and revision["changes"]["impact"] is None
    assert revision["learners"] == {"published": True, "mid_quiz": 0, "mid_play": 1}  # their play starts again
    assert _stored(db, activity).version == 1  # nothing changes until it is applied

    applied = _apply(client, admin_headers, revision["id"])

    assert applied.status_code == 200, applied.text
    stored = _stored(db, activity)
    assert (stored.version, stored.source, stored.intro) == (2, "admin", f"Revisado: {INTRO}")
    assert stored.spec["nodes"][0]["choices"][0]["feedback"] == f"{PRICE['feedback']} Revisado."
    assert stored.spec["nodes"][1] == DECISION["nodes"][1]  # as stored
    worker()
    assert sorted(spoken) == sorted([f"Revisado: {INTRO}", f"{PRICE['feedback']} Revisado."])  # only those
    choice = play["view"]["node"]["choices"][0]["token"]
    moved = client.post(f"{LEARN}/activity-attempts/{play['attempt_id']}/moves", headers=learner,
                        json={"move": {"choice": choice}})
    assert moved.status_code == 409  # "La actividad se actualizó: vuelve a empezar."
    history = _history(client, admin_headers, module.id, target="activity", activity_id=activity.id)
    assert [(item["source"], item["status"], item["activity_id"]) for item in history] == [
        ("observation", "applied", activity.id),
    ]


def test_an_activity_the_ai_breaks_is_never_proposed(client, db, admin_headers, module, activity, worker, monkeypatch):
    def answer(spec_json, title=TITLE, intro=INTRO, unresolved=""):
        return lambda self, prompt: designer.ActivityPatchDraft(summary="Cambio", title=title, intro=intro,
                                                                spec_json=spec_json, unresolved=unresolved)

    broken = copy.deepcopy(DECISION)
    broken["nodes"][0]["choices"][1]["next"] = "nada"
    cases = [
        (json.dumps(broken), "La actividad que propuso la IA no se puede guardar: Una opción lleva a «nada», que no "
                             "existe."),
        ("{nodes:", activity_revisions.UNREADABLE),
        ("[]", activity_revisions.UNREADABLE),
    ]
    for spec_json, error in cases:
        monkeypatch.setattr(FakeLLM, "_activity_patch", answer(spec_json))
        asked = _ask(client, admin_headers, module.id, target="activity", activity_id=activity.id)
        worker()
        failed = _revision(client, admin_headers, asked["revision"]["id"])
        assert (failed["status"], failed["error"]) == ("failed", error)

    monkeypatch.setattr(FakeLLM, "_activity_patch", answer(json.dumps(DECISION), unresolved="No hay ese dato."))
    asked = _ask(client, admin_headers, module.id, target="activity", activity_id=activity.id)
    worker()
    failed = _revision(client, admin_headers, asked["revision"]["id"])
    assert failed["error"] == "La IA no propuso cambios: No hay ese dato."
    assert _stored(db, activity).version == 1


def test_applying_an_activity_change_after_other_edits(client, db, admin_headers, module, activity, worker):
    revision = _proposed(client, admin_headers, worker, module.id, "Hazlo más claro", target="activity",
                         activity_id=activity.id)
    edited = client.put(f"/api/v1/activities/{activity.id}", headers=admin_headers, json={"title": "Otro título"})
    assert edited.status_code == 200

    applied = _apply(client, admin_headers, revision["id"])

    assert applied.status_code == 200 and applied.json()["revision"]["changes"]["rebased"] is True
    stored = _stored(db, activity)
    assert (stored.title, stored.intro, stored.version) == ("Otro título", f"Revisado: {INTRO}", 3)

    second = _proposed(client, admin_headers, worker, module.id, "Otra", target="activity", activity_id=activity.id)
    _edit_activity(client, admin_headers, activity,
                   lambda spec: spec["nodes"][0]["choices"][0].update(feedback="Editada a mano."))
    stale = _apply(client, admin_headers, second["id"])

    assert stale.status_code == 409
    assert stale.json()["detail"] == {
        "code": "revision_stale", "paths": ["nodes/inicio/choices/precio"],
        "message": "La actividad cambió en la opción 1 de la situación 1 desde la propuesta; pide el cambio de nuevo.",
    }
    assert _stored(db, activity).spec["nodes"][0]["choices"][0]["feedback"] == "Editada a mano."


def test_undoing_an_activity_change(client, db, admin_headers, module, activity, worker):
    revision = _proposed(client, admin_headers, worker, module.id, "Cambia el título", target="activity",
                         activity_id=activity.id)
    assert revision["changes"]["activity"]["title"] == {"before": TITLE, "after": f"Revisado: {TITLE}"}
    assert _apply(client, admin_headers, revision["id"]).status_code == 200

    undone = _revert(client, admin_headers, revision["id"])

    assert undone.status_code == 200, undone.text
    undo = undone.json()
    assert (undo["target"], undo["activity_id"], undo["source"]) == ("activity", activity.id, "revert")
    assert undo["scope"]["reverted_id"] == revision["id"]
    stored = _stored(db, activity)
    assert (stored.title, stored.intro, stored.spec, stored.version) == (TITLE, INTRO, DECISION, 3)

    redone = _revert(client, admin_headers, undo["id"])
    assert redone.status_code == 200 and _stored(db, activity).title == f"Revisado: {TITLE}"
    _edit_activity(client, admin_headers, activity,
                   lambda spec: spec["nodes"][0]["choices"][0].update(feedback="Editada a mano."))
    refused = _revert(client, admin_headers, redone.json()["id"])
    assert refused.status_code == 409
    assert refused.json()["detail"] == {
        "code": "revision_conflict", "paths": ["nodes/inicio/choices/precio"],
        "message": "No se puede deshacer: la actividad cambió después en la opción 1 de la situación 1. Edítala a "
                   "mano o pide otro cambio.",
    }


def test_an_activity_change_is_applied_and_undone_over_edits_to_its_other_parts(
    client, db, admin_headers, module, activity, worker
):
    """A conversation's situations and choices (an inspection's items...) are parts of their own: a change to one goes
    over edits made since to the others, and so does undoing it."""
    revision = _proposed(client, admin_headers, worker, module.id, "Más claro", target="activity",
                         activity_id=activity.id)  # the intro and the first choice's feedback
    _edit_activity(client, admin_headers, activity, lambda spec: spec["nodes"][1].update(situation="Carretera."))

    applied = _apply(client, admin_headers, revision["id"])

    assert applied.status_code == 200, applied.text
    assert applied.json()["revision"]["changes"]["rebased"] is True
    assert [entity["path"] for entity in applied.json()["revision"]["changes"]["activity"]["entities"]] == [
        "nodes/inicio/choices/precio",
    ]
    stored = _stored(db, activity)
    assert stored.spec["nodes"][0]["choices"][0]["feedback"] == f"{PRICE['feedback']} Revisado."
    assert (stored.spec["nodes"][1]["situation"], stored.intro) == ("Carretera.", f"Revisado: {INTRO}")

    _edit_activity(client, admin_headers, activity,
                   lambda spec: spec["nodes"][0]["choices"][1].update(text="¿Qué ruta manejan?"))
    undone = _revert(client, admin_headers, revision["id"])

    assert undone.status_code == 200, undone.text
    stored = _stored(db, activity)
    assert (stored.intro, stored.spec["nodes"][0]["choices"][0]) == (INTRO, PRICE)  # what the change touched, back
    assert stored.spec["nodes"][0]["choices"][1]["text"] == "¿Qué ruta manejan?"  # the edits since stay
    assert stored.spec["nodes"][1]["situation"] == "Carretera."


def test_an_activity_saved_before_some_of_its_fields_existed_changes_only_where_it_really_does(
    client, db, admin_headers, module, activity, worker, monkeypatch
):
    """The defaults it lacks (a situation's speaker, an inspection's lives) are no change: not in what the AI proposes,
    and not in a save that only writes them down (the plays open go on)."""
    legacy = copy.deepcopy(DECISION)
    for node in legacy["nodes"]:
        del node["speaker"]  # stored before situations had one
    activity.spec = legacy
    db.commit()

    proposed = _proposed(client, admin_headers, worker, module.id, target="activity", activity_id=activity.id)
    assert [entity["path"] for entity in proposed["changes"]["activity"]["entities"]] == [
        "nodes/inicio/choices/precio",
    ]

    monkeypatch.setattr(FakeLLM, "_activity_patch", lambda self, prompt: designer.ActivityPatchDraft(
        summary="Sin cambios", title=TITLE, intro=INTRO, spec_json=json.dumps(legacy), unresolved="Ya está clara.",
    ))
    asked = _ask(client, admin_headers, module.id, "Revísala", target="activity", activity_id=activity.id)
    worker()
    failed = _revision(client, admin_headers, asked["revision"]["id"])
    assert (failed["status"], failed["error"]) == ("failed", "La IA no propuso cambios: Ya está clara.")

    saved = client.put(f"/api/v1/activities/{activity.id}", headers=admin_headers, json={"spec": DECISION})
    assert saved.status_code == 200 and _stored(db, activity).version == 1
    assert "manual" not in [item["source"] for item in _history(client, admin_headers, module.id)]

    inspection = Activity(type="inspection", title=TITLE, intro=INTRO, spec=copy.deepcopy(INSPECTION))  # no lives
    same = designer.ActivityPatchDraft(summary="Igual", title=TITLE, intro=INTRO, spec_json=json.dumps(INSPECTION),
                                       unresolved="")
    with pytest.raises(revisions.ProposalError, match="no propuso"):
        activity_revisions.propose("inspection", activity_revisions.content(inspection), same)


def test_who_is_mid_play_is_who_can_go_on_with_a_play_of_the_current_version(db, admin, module, activity):
    """Only plays on the activity's current version, not over and (a quick challenge with a clock) still on it count,
    once per learner; an admin's preview never does."""
    learners = {}
    for name in ("ana", "beto", "carla", "dario", "eva", "fer", "gabi"):
        user = User(name=name, email=f"{name}@test.dev", role="collaborator")
        db.add(user)
        db.flush()
        learners[name] = Enrollment(user_id=user.id, course_id=module.course_id, status="assigned", progress_pct=0)
        db.add(learners[name])
    db.flush()

    def play(game, name, *, version=None, finished=False, done=False, **start):
        state = {**engine.start(game, **start), "done": done}
        enrollment = learners[name] if name else None
        db.add(ActivityAttempt(activity_id=game.id, user_id=enrollment.user_id if enrollment else admin.id,
                               enrollment_id=enrollment.id if enrollment else None, version=version or game.version,
                               state=state, finished_at=engine.utcnow() if finished else None))

    activity.version = 2
    play(activity, "ana")
    play(activity, "ana", version=1)  # the same learner, a play left on the older version
    play(activity, "beto", version=1)  # only on the older version: never comes back
    play(activity, "carla", finished=True)
    play(activity, "dario", done=True)  # over, without being closed
    play(activity, None)  # the admin's preview
    quick = Activity(module_id=module.id, order=2, type="quick", title="Ordena", intro="",
                     spec=copy.deepcopy(QUICK_ORDER), status="ready")
    db.add(quick)
    db.flush()
    play(quick, "eva", now=engine.utcnow() - timedelta(minutes=5))  # its time ran out
    play(quick, "fer", untimed=True)
    play(quick, "gabi")
    db.commit()

    assert activity_revisions.learners_mid_play(db, activity) == 1
    assert activity_revisions.learners_mid_play(db, quick) == 2


def test_saving_an_activity_by_hand_is_kept_and_can_be_undone(client, db, admin_headers, module, activity):
    _edit_activity(client, admin_headers, activity, lambda spec: spec["nodes"][1].update(situation="Carretera."))
    for unchanged in ({"status": "draft"}, {"title": TITLE}):  # not what is played, or the same: nothing new
        assert client.put(f"/api/v1/activities/{activity.id}", headers=admin_headers, json=unchanged).status_code == 200

    history = _history(client, admin_headers, module.id, activity_id=activity.id)
    assert [(item["target"], item["source"], item["summary"]) for item in history] == [
        ("activity", "manual", f"Cambios a mano en «{TITLE}»."),
    ]
    manual = _revision(client, admin_headers, history[0]["id"])
    [entity] = manual["changes"]["activity"]["entities"]
    assert (entity["path"], entity["action"], entity["label"]) == ("nodes/ruta", "update", "Situación 2")
    assert entity["before"] == {key: value for key, value in DECISION["nodes"][1].items() if key != "choices"}
    assert manual["changes"]["activity"]["lines_changed"] == 1

    assert _revert(client, admin_headers, manual["id"]).status_code == 200
    stored = _stored(db, activity)
    assert stored.spec == DECISION and stored.version == 3 and stored.status == "draft"


def test_what_activity_changes_can_be_asked(client, db, admin, admin_headers, module, activity, worker):
    url = f"/api/v1/modules/{module.id}/revisions"
    missing = client.post(url, headers=admin_headers, json={"observation": "x", "target": "activity"})
    assert missing.status_code == 422 and missing.json()["detail"] == "Elige la actividad que quieres cambiar"
    elsewhere = Module(course_id=module.course_id, title="Otro", order=2, source="text", content_text="Texto")
    db.add(elsewhere)
    db.flush()
    other_module = Activity(module_id=elsewhere.id, order=1, type="quick", title="Patio", intro="",
                            spec=copy.deepcopy(QUICK_ORDER), status="draft")
    second = Activity(module_id=module.id, order=2, type="quick", title="Ordena", intro="",
                      spec=copy.deepcopy(QUICK_ORDER), status="draft", source="ai")
    db.add_all([other_module, second])
    db.commit()
    foreign = client.post(url, headers=admin_headers,
                          json={"observation": "x", "target": "activity", "activity_id": other_module.id})
    assert foreign.status_code == 404 and foreign.json()["detail"] == "Actividad no encontrada"

    first = _ask(client, admin_headers, module.id, "Más claro", target="activity", activity_id=activity.id)
    busy = client.post(url, headers=admin_headers,
                       json={"observation": "Otra", "target": "activity", "activity_id": activity.id})
    assert busy.status_code == 409
    assert busy.json()["detail"] == (
        "Ya se está preparando otro cambio de esta actividad; espera a que termine para pedir otro."
    )
    assert db.get(Job, first["job"]["id"]).dedupe_key == f"revise:activity:{activity.id}"
    # Another activity of the module has its own proposals, at the same time.
    _ask(client, admin_headers, module.id, "Más corto", target="activity", activity_id=second.id)
    worker()
    newer = _ask(client, admin_headers, module.id, "Otra cosa", target="activity", activity_id=second.id)
    assert _revision(client, admin_headers, first["revision"]["id"])["status"] == "proposed"
    assert [item["id"] for item in _history(client, admin_headers, module.id, activity_id=second.id)][0] == \
        newer["revision"]["id"]

    # An AI draft nobody touched is replaced when the activities are drafted again: not while that runs.
    queue.enqueue(db, "ai.activities", {}, module_id=module.id, dedupe_key=f"activities:{module.id}")
    client.post(f"/api/v1/revisions/{newer['revision']['id']}/discard", headers=admin_headers)
    refused = client.post(url, headers=admin_headers,
                          json={"observation": "Otra", "target": "activity", "activity_id": second.id})
    assert refused.status_code == 409 and "Se están creando las actividades" in refused.json()["detail"]

    # Deleting an activity takes its changes with it.
    assert client.delete(f"/api/v1/activities/{activity.id}", headers=admin_headers).status_code == 204
    assert db.query(ModuleRevision).filter(ModuleRevision.activity_id == activity.id).count() == 0


# ── The merge, the diff and the prompts, on their own ─────────────────


def _quiz_patch(*changes, unresolved=""):
    return designer.QuizPatchDraft(summary="Cambios", changes=list(changes), unresolved=unresolved)


def _question_change(action="update", question_id="q1", fields=("prompt",), **question):
    kind = question.pop("type", "single_choice")
    return designer.QuestionChangeDraft(action=action, question_id=question_id, fields=list(fields), reason="Porque",
                                        question=FakeLLM()._question(kind, **question))


def test_the_quiz_merge_keeps_ids_while_what_is_answered_stays():
    before = {"questions": copy.deepcopy(QUESTIONS)}

    explained = quiz_revisions.propose(before, _quiz_patch(_question_change(fields=["explanation"],
                                                                            explanation="Protege la cabeza.")),
                                       question_ids=None)
    assert explained.after["questions"][0] == {**QUESTIONS[0], "explanation": "Protege la cabeza."}
    assert not quiz_revisions.changes_answers(before, explained.after)

    other_type = quiz_revisions.propose(before, _quiz_patch(_question_change(
        fields=["type"], type="true_false", prompt="El casco es obligatorio", correct_bool=True,
    )), question_ids=None)
    question = other_type.after["questions"][0]
    assert (question["type"], question["correct"]) == ("true_false", True) and question["id"] != "q1"
    [change] = other_type.changes["questions"]
    assert change["action"] == "update" and {"type", "options", "correct"} <= set(change["fields"])
    assert quiz_revisions.changes_answers(before, other_type.after)

    with pytest.raises(revisions.ProposalError, match="todas las preguntas"):
        quiz_revisions.propose(before, _quiz_patch(*(_question_change(action="delete", question_id=q["id"], fields=[])
                                                     for q in QUESTIONS)), question_ids=None)
    with pytest.raises(revisions.ProposalError) as nothing:  # the same prompt: nothing changes
        quiz_revisions.propose(before, _quiz_patch(_question_change(prompt="¿Qué usar?"), unresolved="No hay datos."),
                               question_ids=None)
    assert str(nothing.value) == "La IA no propuso cambios: No hay datos."

    full = {"questions": [{**QUESTIONS[1], "id": f"q{number}"} for number in range(1, 31)]}
    added = quiz_revisions.propose(full, _quiz_patch(
        _question_change(prompt="Revisado"),
        _question_change(action="insert_after", question_id="q30", fields=[], type="true_false", prompt="Nueva"),
    ), question_ids=None)
    assert len(added.after["questions"]) == 30
    assert added.changes["ignored"][-1]["reason"] == "La evaluación ya tiene el máximo de 30 preguntas."


def test_the_quiz_three_way_rebases_by_question_and_a_reworded_one_is_the_old_one_changed():
    base = {"questions": copy.deepcopy(QUESTIONS)}
    reworded = {**QUESTIONS[1], "id": "q2b", "prompt": "Nuevo"}  # what is answered changed: a new id
    target = {"questions": [base["questions"][0], reworded, *base["questions"][2:]]}
    current = copy.deepcopy(base)
    current["questions"][4]["hint"] = "Otra"  # elsewhere
    current["questions"].append({**QUESTIONS[1], "id": "q6", "prompt": "Agregada a mano"})

    merged = quiz_revisions.three_way(current, base, target, {"q2": "q2b"})

    assert [question["id"] for question in merged["questions"]] == ["q1", "q2b", "q3", "q4", "q5", "q6"]
    assert merged["questions"][1] == reworded and merged["questions"][4]["hint"] == "Otra"
    edited = copy.deepcopy(current)
    edited["questions"][1]["prompt"] = "Editada"
    with pytest.raises(revisions.Conflict) as conflict:
        quiz_revisions.three_way(edited, base, target, {"q2": "q2b"})
    assert conflict.value.ids == ["q2"]
    removed = {"questions": [question for question in current["questions"] if question["id"] != "q2"]}
    with pytest.raises(revisions.Conflict):  # removed by hand: its new wording doesn't come back on its own
        quiz_revisions.three_way(removed, base, target, {"q2": "q2b"})
    # Undone: the old question back, with its id, over what the quiz has then.
    undone = quiz_revisions.three_way(merged, target, base, {"q2b": "q2"})
    assert undone["questions"][1] == QUESTIONS[1] and undone["questions"][4]["hint"] == "Otra"


def test_an_activity_is_compared_part_by_part_with_names_for_the_admin():
    inspection = copy.deepcopy(INSPECTION)
    inspection["items"][2]["explanation"] = "Otra explicación."
    inspection["items"].insert(0, inspection["items"].pop(3))  # the fourth tire first
    inspection["verdicts"].append({"id": "revisar", "label": "Revisar"})
    inspection["prompt"] = "Revisa las llantas."

    changes = activity_revisions.entity_changes(INSPECTION, inspection)

    assert [(change["path"], change["action"], change["label"]) for change in changes] == [
        ("prompt", "update", "Instrucción"),
        ("verdicts/revisar", "insert", "Decisión «Revisar»"),
        ("items/l4", "move", "Elemento «Llanta 4 · tracción derecha»"),
        ("items/l3", "update", "Elemento «Llanta 3 · tracción izquierda»"),
    ]

    simulator = copy.deepcopy(SIMULATOR)
    simulator["variables"][1]["max"] = 300000
    simulator["target"]["value"] = 0.04
    assert [(change["path"], change["label"]) for change in activity_revisions.entity_changes(SIMULATOR, simulator)] \
        == [("variables/km", "Control «Kilómetros de vida»"), ("target", "Objetivo")]

    quick = {**QUICK_ORDER, "items": list(reversed(QUICK_ORDER["items"]))}
    assert [(change["path"], change["label"]) for change in activity_revisions.entity_changes(QUICK_ORDER, quick)] \
        == [("items", "Pasos")]  # a quick challenge's steps go together

    decision = copy.deepcopy(DECISION)
    removed = decision["nodes"].pop(1)
    decision["nodes"][0]["choices"][1]["next"] = ""
    changes = activity_revisions.entity_changes(DECISION, decision)
    assert [(change["path"], change["action"]) for change in changes] == [
        ("nodes/inicio/choices/pregunta", "update"), ("nodes/ruta", "delete"),  # its choices go with it
    ]
    assert changes[1]["before"] == removed and changes[1]["label"] == "Situación 2"


def test_the_activity_three_way_merges_part_by_part():
    base = {"title": TITLE, "intro": INTRO, "spec": copy.deepcopy(DECISION)}
    target = copy.deepcopy(base)
    target["spec"]["nodes"][1]["situation"] = "Otra situación."  # the change: the second situation
    edited = copy.deepcopy(base)
    edited["title"] = "Otro título"
    edited["spec"]["nodes"][0]["choices"][0]["feedback"] = "Editada."  # since: another part of the same list

    merged = activity_revisions.three_way(edited, base, target)

    assert merged["title"] == "Otro título"
    assert merged["spec"] == {"nodes": [edited["spec"]["nodes"][0], target["spec"]["nodes"][1]]}
    # Undone over a later edit to another part: only what the change touched goes back.
    later = copy.deepcopy(merged)
    later["spec"]["nodes"][0]["choices"][1]["text"] = "¿Qué ruta manejan?"
    undone = activity_revisions.three_way(later, merged, edited)
    assert undone["spec"]["nodes"][1] == DECISION["nodes"][1]
    assert undone["spec"]["nodes"][0] == later["spec"]["nodes"][0] and undone["title"] == "Otro título"

    # The same part changed since: refused, named as the admin knows it.
    edited["spec"]["nodes"][1]["situation"] = "Editada a mano."
    retitled = {**target, "title": "Revisado"}
    with pytest.raises(revisions.Conflict) as conflict:
        activity_revisions.three_way(edited, base, retitled)
    assert conflict.value.ids == ["title", "nodes/ruta"]
    assert activity_revisions.places(conflict.value.ids, edited) == "el título y la situación 2"
    assert activity_revisions.places(["title", "variables/km"], {**base, "spec": SIMULATOR}) == (
        "el título y el control «Kilómetros de vida»"
    )

    # A situation the change removes, given a choice since; and the other way around.
    removed = copy.deepcopy(base)
    removed["spec"]["nodes"].pop(1)
    removed["spec"]["nodes"][0]["choices"][1]["next"] = ""
    grown = copy.deepcopy(base)
    grown["spec"]["nodes"][1]["choices"].append({"id": "flota", "text": "¿Cuántas unidades tiene?", "points": 3,
                                                 "feedback": "Conoces su flota.", "next": ""})
    with pytest.raises(revisions.Conflict) as conflict:
        activity_revisions.three_way(grown, base, removed)
    assert conflict.value.ids == ["nodes/ruta/choices/flota"]
    with pytest.raises(revisions.Conflict) as conflict:
        activity_revisions.three_way(removed, base, grown)
    assert conflict.value.ids == ["nodes/ruta"]

    # An element the change moves goes where it puts it, with the edits made to it since; one added since stays.
    inspection = {"title": TITLE, "intro": INTRO, "spec": copy.deepcopy(INSPECTION)}
    moved = copy.deepcopy(inspection)
    moved["spec"]["items"].insert(0, moved["spec"]["items"].pop(3))  # the fourth tire first
    since = copy.deepcopy(inspection)
    since["spec"]["items"][3]["explanation"] = "Editada a mano."
    since["spec"]["verdicts"].insert(1, {"id": "revisar", "label": "Revisar"})

    merged = activity_revisions.three_way(since, inspection, moved)

    assert [item["id"] for item in merged["spec"]["items"]] == ["l4", "l1", "l2", "l3"]
    assert merged["spec"]["items"][0]["explanation"] == "Editada a mano."
    assert [verdict["id"] for verdict in merged["spec"]["verdicts"]] == ["rodar", "revisar", "reencauchar", "retirar"]


def test_the_revision_prompts_put_what_repeats_first_and_the_observation_last():
    prompt = designer.quiz_revision_prompt("Seguridad", "EPP", "Usa el casco.", QUESTIONS, ["q2"], "Más clara")
    order = ["REGLAS DE LAS PREGUNTAS", "CÓMO RESPONDER", "CURSO: Seguridad", "MÓDULO: EPP", "Usa el casco.",
             "PREGUNTAS ACTUALES", "PREGUNTAS EN ALCANCE: q2", "OBSERVACIÓN DEL ADMINISTRADOR:"]
    assert [prompt.index(part) for part in order] == sorted(prompt.index(part) for part in order)
    assert prompt.endswith("OBSERVACIÓN DEL ADMINISTRADOR:\nMás clara")
    listed = json.loads(designer.questions_json(QUESTIONS))
    assert listed[0] == {"number": 1, "id": "q1", "type": "single_choice", "prompt": "¿Qué usar?",
                         "options": ["Casco", "Gorra"], "correct_index": 0, "explanation": "El casco protege."}
    assert listed[1] == {"number": 2, "id": "q2", "type": "true_false", "prompt": "El EPP es opcional",
                         "correct_bool": False, "explanation": "Es obligatorio."}
    assert listed[4]["answers"] == ["prevención"] and listed[4]["hint"] == "p..."

    activity = {"type": "decision", "title": TITLE, "intro": INTRO, "spec": DECISION}
    prompt = designer.activity_revision_prompt("Llantas", "Ventas", "El guion.", "Material", activity, "Más corto")
    order = ["Material", "CURSO: Llantas", "CÓMO RESPONDER", "MÓDULO: Ventas", "El guion.", "«Decisión con cliente»",
             "ACTIVIDAD ACTUAL", "OBSERVACIÓN DEL ADMINISTRADOR:"]
    assert [prompt.index(part) for part in order] == sorted(prompt.index(part) for part in order)
    assert json.dumps(activity, ensure_ascii=False) in prompt and prompt.endswith("Más corto")
    # Another kind of activity of another module starts the same, up to its module (what the provider reuses).
    other = designer.activity_revision_prompt("Llantas", "Rotación", "Otro guion.", "Material",
                                              {**activity, "type": "quick", "spec": QUICK_ORDER}, "x")
    assert len(os.path.commonprefix([prompt, other])) >= prompt.index("MÓDULO: Ventas")
    # The quiz's rules are the module's: one set of them.
    outline = designer.CourseOutline(title="C", description="D", audience="A", objectives=[],
                                     modules=[designer.OutlineModule(**OUTLINE)])
    assert designer._QUESTION_RULES in designer.module_prompt(outline, outline.modules[0], 1, "", "")
