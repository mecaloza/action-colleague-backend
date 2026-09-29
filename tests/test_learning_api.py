"""Learner flow: access control, sequential unlocking, quizzes without answer keys, completion."""

import json

import pytest

from app.core.config import get_settings
from app.db.models import Course, Enrollment, Evaluation, Module
from app.services import quiz
from tests.conftest import auth_headers

QUESTIONS = [
    {"id": "q1", "type": "single_choice", "prompt": "¿Qué usar?", "scenario": "", "options": ["Casco", "Gorra"], "correct_index": 0, "explanation": "El casco protege."},
    {"id": "q2", "type": "true_false", "prompt": "El EPP es opcional", "correct": False, "explanation": "Es obligatorio."},
    {"id": "q3", "type": "ordering", "prompt": "Ordena", "items": ["Revisar", "Ajustar", "Trabajar"], "explanation": ""},
    {"id": "q4", "type": "matching", "prompt": "Empareja", "pairs": [{"left": "Casco", "right": "Cabeza"}, {"left": "Guante", "right": "Manos"}], "explanation": ""},
    {"id": "q5", "type": "fill_blank", "prompt": "La _____ es clave", "answers": ["prevención"], "hint": "p...", "explanation": ""},
]


@pytest.fixture
def course(db, admin):
    course = Course(title="Seguridad", description="EPP", status="published", source="manual", settings={}, created_by=admin.id)
    db.add(course)
    db.flush()
    m1 = Module(course_id=course.id, title="Intro", order=1, content_text="Bienvenida", source="text")
    m2 = Module(course_id=course.id, title="EPP", order=2, content_text="Uso del casco", source="text")
    m3 = Module(course_id=course.id, title="Cierre", order=3, content_text="Resumen", source="text")
    db.add_all([m1, m2, m3])
    db.flush()
    db.add(Evaluation(module_id=m2.id, spec=QUESTIONS, max_attempts=2, passing_score=80))
    db.commit()
    return course


@pytest.fixture
def enrolled(db, course, collaborator):
    enrollment = Enrollment(user_id=collaborator.id, course_id=course.id, status="assigned", progress_pct=0)
    db.add(enrollment)
    db.commit()
    return enrollment


def _modules(course):
    return sorted(course.modules, key=lambda m: m.order)


def _perfect_answers(evaluation_id: int) -> list[dict]:
    secret, scope = quiz.token_secret(get_settings().jwt_secret), quiz.quiz_scope(evaluation_id)
    token = lambda qid, kind, i: quiz._token(secret, scope, qid, kind, i)  # noqa: E731
    return [
        {"question_id": "q1", "response": {"option": token("q1", "option", 0)}},
        {"question_id": "q2", "response": {"value": False}},
        {"question_id": "q3", "response": {"order": [token("q3", "item", i) for i in range(3)]}},
        {"question_id": "q4", "response": {"matches": {token("q4", "left", 1): token("q4", "right", 1), token("q4", "left", 0): token("q4", "right", 0)}}},
        {"question_id": "q5", "response": {"text": "Prevencion"}},
    ]


def test_my_courses_lists_only_published_enrollments(client, db, enrolled, collaborator, admin):
    draft = Course(title="Borrador", status="draft", source="manual", settings={}, created_by=admin.id)
    db.add(draft)
    db.flush()
    db.add(Enrollment(user_id=collaborator.id, course_id=draft.id, status="assigned"))
    db.commit()

    response = client.get("/api/v1/learn/courses", headers=auth_headers(client, collaborator.email))

    assert response.status_code == 200
    assert [c["title"] for c in response.json()] == ["Seguridad"]
    assert response.json()[0]["next_module_title"] == "Intro"


def test_course_player_locks_later_modules(client, enrolled, course, collaborator):
    body = client.get(f"/api/v1/learn/courses/{course.id}", headers=auth_headers(client, collaborator.email)).json()

    unlocked = [m["unlocked"] for m in body["modules"]]
    assert unlocked == [True, False, False]
    assert body["modules"][1]["content_text"] is None  # locked content is not sent
    assert body["modules"][1]["quiz"]["question_count"] == 5


def test_not_enrolled_learner_gets_404(client, course, collaborator):
    response = client.get(f"/api/v1/learn/courses/{course.id}", headers=auth_headers(client, collaborator.email))
    assert response.status_code == 404


def test_completing_a_module_without_quiz_unlocks_the_next(client, enrolled, course, collaborator):
    headers = auth_headers(client, collaborator.email)
    intro, epp, _ = _modules(course)

    result = client.post(f"/api/v1/learn/modules/{intro.id}/complete", headers=headers)

    assert result.status_code == 200
    assert result.json() == {"module_completed": True, "next_module_id": epp.id, "course_completed": False}
    body = client.get(f"/api/v1/learn/courses/{course.id}", headers=headers).json()
    assert [m["unlocked"] for m in body["modules"]] == [True, True, False]


def test_locked_module_cannot_be_completed_or_quizzed(client, enrolled, course, collaborator):
    headers = auth_headers(client, collaborator.email)
    _, epp, closing = _modules(course)

    assert client.post(f"/api/v1/learn/modules/{closing.id}/complete", headers=headers).status_code == 403
    assert client.get(f"/api/v1/learn/modules/{epp.id}/quiz", headers=headers).status_code == 403


def test_module_with_quiz_cannot_be_marked_complete(client, enrolled, course, collaborator):
    headers = auth_headers(client, collaborator.email)
    intro, epp, _ = _modules(course)
    client.post(f"/api/v1/learn/modules/{intro.id}/complete", headers=headers)

    assert client.post(f"/api/v1/learn/modules/{epp.id}/complete", headers=headers).status_code == 409


def test_quiz_never_exposes_answer_keys(client, enrolled, course, collaborator):
    headers = auth_headers(client, collaborator.email)
    intro, epp, _ = _modules(course)
    client.post(f"/api/v1/learn/modules/{intro.id}/complete", headers=headers)

    body = client.get(f"/api/v1/learn/modules/{epp.id}/quiz", headers=headers).json()

    text = json.dumps(body)
    for key in ("correct_index", "\"correct\"", "answers", "pairs", "explanation"):
        assert key not in text
    ordering = next(q for q in body["questions"] if q["type"] == "ordering")
    assert sorted(item["text"] for item in ordering["items"]) == ["Ajustar", "Revisar", "Trabajar"]


def test_passing_the_quiz_completes_the_module_and_unlocks_next(client, db, enrolled, course, collaborator):
    headers = auth_headers(client, collaborator.email)
    intro, epp, closing = _modules(course)
    client.post(f"/api/v1/learn/modules/{intro.id}/complete", headers=headers)
    evaluation = db.query(Evaluation).filter(Evaluation.module_id == epp.id).one()

    result = client.post(
        f"/api/v1/learn/modules/{epp.id}/quiz/attempts", headers=headers, json={"answers": _perfect_answers(evaluation.id)}
    ).json()

    assert result["passed"] is True and result["score"] == 100.0
    assert result["next_module_id"] == closing.id
    assert all(r["correct"] for r in result["results"])
    again = client.post(f"/api/v1/learn/modules/{epp.id}/quiz/attempts", headers=headers, json={"answers": []})
    assert again.status_code == 409


def test_failed_attempts_are_limited_and_solutions_never_revealed_without_passing(
    client, db, enrolled, course, collaborator
):
    headers = auth_headers(client, collaborator.email)
    intro, epp, _ = _modules(course)
    client.post(f"/api/v1/learn/modules/{intro.id}/complete", headers=headers)

    first = client.post(f"/api/v1/learn/modules/{epp.id}/quiz/attempts", headers=headers, json={"answers": []}).json()
    assert first["passed"] is False and first["attempts_remaining"] == 1
    assert all(r["expected"] is None for r in first["results"])

    # Out of attempts: still no solution (more attempts later would make it a free pass).
    second = client.post(f"/api/v1/learn/modules/{epp.id}/quiz/attempts", headers=headers, json={"answers": []}).json()
    assert second["attempts_remaining"] == 0
    assert all(r["expected"] is None for r in second["results"])

    third = client.post(f"/api/v1/learn/modules/{epp.id}/quiz/attempts", headers=headers, json={"answers": []})
    assert third.status_code == 403


def test_finishing_every_module_completes_the_course(client, db, enrolled, course, collaborator):
    headers = auth_headers(client, collaborator.email)
    intro, epp, closing = _modules(course)
    client.post(f"/api/v1/learn/modules/{intro.id}/complete", headers=headers)
    evaluation = db.query(Evaluation).filter(Evaluation.module_id == epp.id).one()
    client.post(f"/api/v1/learn/modules/{epp.id}/quiz/attempts", headers=headers, json={"answers": _perfect_answers(evaluation.id)})

    final = client.post(f"/api/v1/learn/modules/{closing.id}/complete", headers=headers).json()

    assert final["course_completed"] is True
    course_list = client.get("/api/v1/learn/courses", headers=headers).json()
    assert course_list[0]["status"] == "completed" and course_list[0]["progress_pct"] == 100.0


def test_position_is_saved_for_resume(client, enrolled, course, collaborator):
    headers = auth_headers(client, collaborator.email)
    intro = _modules(course)[0]

    assert client.put(f"/api/v1/learn/modules/{intro.id}/position", headers=headers, json={"seconds": 42.5}).status_code == 204
    body = client.get(f"/api/v1/learn/courses/{course.id}", headers=headers).json()
    assert body["modules"][0]["last_position_seconds"] == 42.5


def test_legacy_questions_still_grade_correctly(client, db, enrolled, course, collaborator):
    """Evaluations saved by the previous app (questions_json, true/false as 0/1) keep working."""
    headers = auth_headers(client, collaborator.email)
    intro, epp, _ = _modules(course)
    client.post(f"/api/v1/learn/modules/{intro.id}/complete", headers=headers)
    evaluation = db.query(Evaluation).filter(Evaluation.module_id == epp.id).one()
    evaluation.spec = None
    evaluation.questions_json = json.dumps([{"type": "true_false", "statement": "El EPP es opcional", "correct": 0}])
    evaluation.passing_score = 70
    db.commit()

    quiz_body = client.get(f"/api/v1/learn/modules/{epp.id}/quiz", headers=headers).json()
    qid = quiz_body["questions"][0]["id"]
    result = client.post(
        f"/api/v1/learn/modules/{epp.id}/quiz/attempts", headers=headers,
        json={"answers": [{"question_id": qid, "response": {"value": False}}]},
    ).json()

    assert result["passed"] is True


def test_locked_progress_row_is_reread_not_taken_from_the_session(db, session_factory, enrolled, course):
    from app.db.models import ModuleProgress
    from app.services import progress

    epp = _modules(course)[1]
    record = progress.get_or_create_progress(db, enrolled, epp)
    db.commit()
    assert enrolled.module_progress  # the row is now in this session's identity map
    with session_factory() as other:  # a concurrent submission used an attempt meanwhile
        other.get(ModuleProgress, record.id).attempts = 1
        other.commit()

    locked = progress.get_or_create_progress(db, enrolled, epp, lock=True)

    assert locked.attempts == 1


def test_a_quiz_edited_while_answering_is_not_graded(client, enrolled, course, collaborator, admin):
    headers = auth_headers(client, collaborator.email)
    intro, epp, _ = _modules(course)
    client.post(f"/api/v1/learn/modules/{intro.id}/complete", headers=headers)
    version = client.get(f"/api/v1/learn/modules/{epp.id}/quiz", headers=headers).json()["version"]
    edited = [*QUESTIONS[:4], {**QUESTIONS[4], "answers": ["precaución"]}]
    saved = client.put(
        f"/api/v1/modules/{epp.id}/evaluation", headers=auth_headers(client, admin.email),
        json={"questions": edited, "max_attempts": 2, "passing_score": 80},
    )
    assert saved.status_code == 200

    stale = client.post(f"/api/v1/learn/modules/{epp.id}/quiz/attempts", headers=headers, json={"answers": [], "version": version})

    assert stale.status_code == 409
    fresh = client.get(f"/api/v1/learn/modules/{epp.id}/quiz", headers=headers).json()
    assert fresh["attempts_used"] == 0 and fresh["version"] != version


def test_completed_modules_stay_open_after_a_reorder(client, enrolled, course, collaborator, admin):
    headers = auth_headers(client, collaborator.email)
    intro, epp, closing = _modules(course)
    client.post(f"/api/v1/learn/modules/{intro.id}/complete", headers=headers)
    client.put(
        f"/api/v1/courses/{course.id}/modules/order", headers=auth_headers(client, admin.email),
        json={"module_ids": [epp.id, closing.id, intro.id]},
    )

    modules = {m["id"]: m for m in client.get(f"/api/v1/learn/courses/{course.id}", headers=headers).json()["modules"]}

    assert modules[intro.id]["unlocked"] and modules[intro.id]["content_text"] == "Bienvenida"
    assert modules[epp.id]["unlocked"] and not modules[closing.id]["unlocked"]


def test_answers_have_a_bounded_shape(client, enrolled, course, collaborator):
    headers = auth_headers(client, collaborator.email)
    intro, epp, _ = _modules(course)
    client.post(f"/api/v1/learn/modules/{intro.id}/complete", headers=headers)
    url = f"/api/v1/learn/modules/{epp.id}/quiz/attempts"

    assert client.post(url, headers=headers, json={"answers": [{"question_id": "q5", "response": {"text": "x" * 301}}]}).status_code == 422
    assert client.post(url, headers=headers, json={"answers": [{"question_id": "q1", "response": {"essay": "x"}}]}).status_code == 422
    assert client.get(f"/api/v1/learn/modules/{epp.id}/quiz", headers=headers).json()["attempts_used"] == 0


def test_missing_and_foreign_modules_look_the_same(client, course, collaborator):
    headers = auth_headers(client, collaborator.email)

    missing = client.get("/api/v1/learn/modules/999999/quiz", headers=headers)
    foreign = client.get(f"/api/v1/learn/modules/{_modules(course)[1].id}/quiz", headers=headers)  # not enrolled

    assert missing.status_code == foreign.status_code == 404
    assert missing.json()["detail"] == foreign.json()["detail"]


def test_legacy_questions_without_a_known_answer_are_dropped():
    questions = quiz.normalize_legacy_questions([
        {"question": "Sin clave", "options": ["A", "B"]},
        {"question": "Repetidas", "options": ["Sí", "No", "sí"], "correct": 2},
        {"question": "Rara", "options": ["A", "B"], "correct": [1, "0"]},
        {"type": "ordering", "items": ["a", "b"], "correct_order": [1, "0"]},
    ])

    assert [q.prompt for q in questions] == ["Repetidas"]
    assert questions[0].options == ["Sí", "No"] and questions[0].correct_index == 0


def test_fixing_an_explanation_does_not_invalidate_a_quiz_in_progress(client, enrolled, course, collaborator, admin):
    headers = auth_headers(client, collaborator.email)
    intro, epp, _ = _modules(course)
    client.post(f"/api/v1/learn/modules/{intro.id}/complete", headers=headers)
    version = client.get(f"/api/v1/learn/modules/{epp.id}/quiz", headers=headers).json()["version"]
    fixed = [{**QUESTIONS[0], "explanation": "El casco protege la cabeza."}, *QUESTIONS[1:]]
    client.put(
        f"/api/v1/modules/{epp.id}/evaluation", headers=auth_headers(client, admin.email),
        json={"questions": fixed, "max_attempts": 2, "passing_score": 80},
    )

    attempt = client.post(f"/api/v1/learn/modules/{epp.id}/quiz/attempts", headers=headers, json={"answers": [], "version": version})

    assert attempt.status_code == 200
