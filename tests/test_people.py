"""A person's overview for admins ("ficha"), their row, giving quiz attempts back, and what the dashboard tells of
plays, certificates and paths."""

import copy
import logging
from datetime import datetime, timezone

import pytest

from app.db.models import (
    Activity,
    ActivityAttempt,
    Certificate,
    Enrollment,
    Evaluation,
    EvaluationAttempt,
    LearningPath,
    LearningPathCourse,
    ModuleProgress,
    PathAssignment,
    RefreshToken,
)
from tests.conftest import QueryCounter, auth_headers
from tests.test_activity_spec import DECISION
from tests.test_certificates import _answer, _enroll, _learner
from tests.test_learning_api import QUESTIONS
from tests.test_paths import _assign, _course, _modules, _path, _person, _profile
from tests.test_points import BEST, POOR, _activity, _converse

API = "/api/v1"


@pytest.fixture
def admin_headers(client, admin):
    return auth_headers(client, admin.email)


def _quiz(db, module, *, max_attempts: int = 3, passing_score: int = 70) -> Evaluation:
    evaluation = Evaluation(module_id=module.id, spec=QUESTIONS, max_attempts=max_attempts,
                            passing_score=passing_score)
    db.add(evaluation)
    db.commit()
    return evaluation


# ── The overview ──────────────────────────────────────────────────────


def test_a_persons_overview(client, db, admin, admin_headers):
    advisor = _profile(client, admin_headers, "Asesor de flotas")
    course = _course(db, admin, "Atención al cliente", modules=2)
    draft = _course(db, admin, "Borrador", status="draft")
    talk, closing = _modules(db, course)
    activity = _activity(db, talk)
    evaluation = _quiz(db, closing)
    path = _path(client, admin_headers, [course], title="Ruta de asesores")
    ana = _learner(db)
    ana.profile_id, ana.position = advisor["id"], "Asesora"
    db.commit()
    _assign(client, admin_headers, path, ana)
    _enroll(client, admin_headers, draft, ana)
    learner = auth_headers(client, ana.email)
    _converse(client, learner, activity.id, *POOR)  # 2 of 10
    _converse(client, learner, activity.id, *BEST)  # 10 of 10
    _answer(client, learner, closing, evaluation, correct=3)  # 60: not passed
    passed = _answer(client, learner, closing, evaluation)  # 100: the course and its path completed
    assert passed["course_completed"] and passed["path_completed"]

    response = client.get(f"{API}/users/{ana.id}/overview", headers=admin_headers)

    assert response.status_code == 200, response.text
    body = response.json()
    user = body["user"]
    assert user == {**user, "id": ana.id, "name": "Ana", "email": "ana@test.dev", "role": "collaborator",
                    "position": "Asesora", "department": "", "is_active": True,
                    "profile": {"id": advisor["id"], "name": "Asesor de flotas"}}
    assert user["created_at"] and user["last_seen_at"]  # signed in
    # Points: the plays 20 + 80, its module 20, the quiz 60 + 40, its module 20, the course 100 and the path 200.
    assert body["kpis"] == {"courses_assigned": 2, "courses_completed": 1, "average_score": 100.0, "points": 540,
                            "certificates": 2, "activities_played": 1}

    (on_path,) = body["paths"]
    assert (on_path["id"], on_path["title"], on_path["status"], on_path["source"]) == (
        path["id"], "Ruta de asesores", "completed", "manual")
    assert on_path["assigned_at"] and on_path["certificate"]["code"].startswith("RC-")
    assert [step["status"] for step in on_path["steps"]] == ["completed"]

    by_id = {item["course_id"]: item for item in body["courses"]}
    assert set(by_id) == {course.id, draft.id}
    done = by_id[course.id]
    assert {key: done[key] for key in ("title", "status", "course_status", "progress_pct", "locked")} == {
        "title": "Atención al cliente", "status": "completed", "course_status": "published", "progress_pct": 100.0,
        "locked": False}
    assert done["completed_at"] and done["enrolled_at"] and done["certificate"]["code"].startswith("RC-")
    first, second = done["modules"]
    assert (first["module_id"], first["title"], first["order"], first["completed"], first["quiz"]) == (
        talk.id, talk.title, 1, True, None)
    (played,) = first["activities"]
    assert played == {**played, "activity_id": activity.id, "title": "Cliente", "type": "decision", "best_score": 10,
                      "max_score": 10, "plays": 2}
    assert played["last_played_at"]
    assert second["completed"] and second["completed_at"] and second["activities"] == []
    assert second["quiz"] == {"question_count": 5, "max_attempts": 3, "attempts_used": 2, "passed": True,
                              "best_score": 100.0, "passing_score": 70}
    assert by_id[draft.id] == {**by_id[draft.id], "status": "assigned", "course_status": "draft",
                               "progress_pct": 0.0, "certificate": None, "completed_at": None}

    assert sorted((c["kind"], c["status"], c["recipient_name"]) for c in body["certificates"]) == [
        ("course", "valid", "Ana"), ("path", "valid", "Ana")]

    recent = body["recent"]
    assert sorted(event["kind"] for event in recent) == sorted([
        "activity_finished", "activity_finished", "module_completed", "quiz_failed", "quiz_passed",
        "module_completed", "course_completed", "certificate_issued", "certificate_issued", "path_completed"])
    assert [event["at"] for event in recent] == sorted((event["at"] for event in recent), reverse=True)
    by_kind = {event["kind"]: event for event in recent}
    assert by_kind["quiz_failed"] == {**by_kind["quiz_failed"], "title": closing.title,
                                      "detail": "Atención al cliente", "score": 60.0}
    assert sorted(e["score"] for e in recent if e["kind"] == "activity_finished") == [20.0, 100.0]
    assert by_kind["path_completed"] == {**by_kind["path_completed"], "title": "Ruta de asesores", "detail": "",
                                         "score": None}
    assert {e["detail"] for e in recent if e["kind"] == "certificate_issued"} == {
        c["code"] for c in body["certificates"]}


def test_a_person_with_nothing_yet(client, db, admin_headers):
    nobody = _person(db, "nadie@test.dev")

    body = client.get(f"{API}/users/{nobody.id}/overview", headers=admin_headers).json()

    assert body["kpis"] == {"courses_assigned": 0, "courses_completed": 0, "average_score": None, "points": 0,
                            "certificates": 0, "activities_played": 0}
    assert (body["paths"], body["courses"], body["certificates"], body["recent"]) == ([], [], [], [])
    assert body["user"]["last_seen_at"] is None
    assert client.get(f"{API}/users/999/overview", headers=admin_headers).status_code == 404


def _someone_with(db, admin, email: str, courses: int, modules: int) -> int:
    """A person on a path of `courses` courses of `modules` modules each, every module with a quiz taken and an
    activity played: written straight into the tables."""
    person = _person(db, email)
    path = LearningPath(title=f"Ruta de {email}", status="published", settings={})
    db.add(path)
    db.flush()
    for number in range(courses):
        course = _course(db, admin, f"{email} {number}", modules=modules)
        db.add(LearningPathCourse(path_id=path.id, course_id=course.id, order=number + 1))
        enrollment = Enrollment(user_id=person.id, course_id=course.id, status="in_progress", progress_pct=0)
        db.add(enrollment)
        db.flush()
        for module in _modules(db, course):
            evaluation = _quiz(db, module)
            activity = Activity(module_id=module.id, order=1, type="decision", title="Cliente", intro="",
                                spec=copy.deepcopy(DECISION), status="ready")
            db.add(activity)
            db.flush()
            db.add_all([
                ModuleProgress(enrollment_id=enrollment.id, module_id=module.id, completed=True, passed=True,
                               score=80, attempts=1, completed_at=datetime.now(timezone.utc)),
                EvaluationAttempt(evaluation_id=evaluation.id, user_id=person.id, enrollment_id=enrollment.id,
                                  module_id=module.id, score=80, passed=True, attempt_number=1),
                *(ActivityAttempt(activity_id=activity.id, user_id=person.id, enrollment_id=enrollment.id,
                                  version=1, state={"done": True}, score=6, max_score=10,
                                  finished_at=datetime.now(timezone.utc)) for _ in range(2)),
            ])
        db.add(Certificate(code=f"RC-{email[:4].upper()}-{number:04d}", kind="course", user_id=person.id,
                           course_id=course.id, recipient_name=person.name, title=course.title, details={}))
    db.add(PathAssignment(path_id=path.id, user_id=person.id, source="manual"))
    db.add(RefreshToken(user_id=person.id, token=f"t-{email}", expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc)))
    db.commit()
    return person.id


def test_the_overview_reads_the_same_whatever_the_persons_size(client, db, engine, admin, admin_headers):
    small = _someone_with(db, admin, "poco@test.dev", courses=1, modules=1)
    big = _someone_with(db, admin, "mucho@test.dev", courses=3, modules=4)
    counter = QueryCounter(engine)
    reads = {}
    for name, user_id in (("small", small), ("big", big)):
        with counter:
            response = client.get(f"{API}/users/{user_id}/overview", headers=admin_headers)
        assert response.status_code == 200, response.text
        reads[name] = counter.count

    body = response.json()
    assert len(body["courses"]) == 3 and sum(len(course["modules"]) for course in body["courses"]) == 12
    assert body["kpis"]["activities_played"] == 12 and len(body["recent"]) == 25
    # The session's user, the person and their last sign-in (a profile would add one), their courses (3), quizzes,
    # activities and plays, their paths (6), certificates, points, and the latest quizzes and plays: never one per
    # course or module.
    assert reads["big"] == reads["small"] <= 20, reads


# ── The person's row ──────────────────────────────────────────────────


def test_a_persons_row(client, db, admin_headers):
    ana = _learner(db)

    row = client.get(f"{API}/users/{ana.id}", headers=admin_headers)

    assert row.status_code == 200
    assert row.json() == {**row.json(), "id": ana.id, "name": "Ana", "email": "ana@test.dev", "profile": None,
                          "is_active": True, "enrolled_count": 0, "completed_count": 0}
    assert client.get(f"{API}/users/999", headers=admin_headers).json() == {"detail": "Persona no encontrada"}


# ── Giving quiz attempts back ─────────────────────────────────────────


def test_giving_quiz_attempts_back(client, db, admin, admin_headers, caplog):
    course = _course(db, admin, "Seguridad")
    (module,) = _modules(db, course)
    evaluation = _quiz(db, module, max_attempts=2, passing_score=90)
    ana = _learner(db)
    _enroll(client, admin_headers, course, ana)
    learner = auth_headers(client, ana.email)
    url = f"{API}/learn/modules/{module.id}/quiz/attempts"
    for _ in range(2):
        _answer(client, learner, module, evaluation, correct=3)
    out = client.post(url, headers=learner, json={"answers": []})
    assert out.status_code == 403  # out of attempts: a sequential path would be stuck here
    reset = f"{API}/users/{ana.id}/modules/{module.id}/quiz-reset"

    with caplog.at_level(logging.INFO, logger="app.api.routes.users"):
        given = client.post(reset, headers=admin_headers)

    assert given.status_code == 200 and given.json() == {"attempts_used": 0, "max_attempts": 2}
    (logged,) = [record for record in caplog.records if record.getMessage() == "quiz_attempts_reset"]
    assert (logged.admin_id, logged.user_id, logged.module_id, logged.attempts_given_back) == (
        admin.id, ana.id, module.id, 2)
    assert client.get(f"{API}/learn/modules/{module.id}/quiz", headers=learner).json()["attempts_used"] == 0
    again = _answer(client, learner, module, evaluation, correct=4)  # 80: still not passed
    assert (again["attempts_used"], again["attempts_remaining"]) == (1, 1)
    db.expire_all()
    # Their history stays, numbered on.
    assert [(a.attempt_number, a.score) for a in db.query(EvaluationAttempt).order_by(EvaluationAttempt.id)] == [
        (1, 60.0), (2, 60.0), (3, 80.0)]
    assert _answer(client, learner, module, evaluation)["passed"]

    passed = client.post(reset, headers=admin_headers)
    assert passed.status_code == 409 and passed.json() == {"detail": "La persona ya aprobó esta evaluación."}


def test_giving_attempts_back_needs_a_quiz_and_an_enrollment(client, db, admin, admin_headers):
    course, other = _course(db, admin, "Con evaluación"), _course(db, admin, "Sin evaluación")
    (module,) = _modules(db, course)
    _quiz(db, module)
    ana, beto = _learner(db, "ana@test.dev"), _person(db, "beto@test.dev")
    _enroll(client, admin_headers, course, ana)
    _enroll(client, admin_headers, other, ana)

    def reset(user_id: int, module_id: int):
        return client.post(f"{API}/users/{user_id}/modules/{module_id}/quiz-reset", headers=admin_headers)

    untouched = reset(ana.id, module.id)  # nothing used yet: nothing to give back
    assert untouched.status_code == 200 and untouched.json() == {"attempts_used": 0, "max_attempts": 3}
    assert reset(ana.id, _modules(db, other)[0].id).json() == {"detail": "Este módulo no tiene evaluación"}
    assert reset(beto.id, module.id).json() == {"detail": "Esta persona no está inscrita en el curso"}
    assert reset(999, module.id).status_code == 404 and reset(ana.id, 999).status_code == 404
    assert db.query(ModuleProgress).count() == 0  # nothing was written for them


# ── The dashboard ─────────────────────────────────────────────────────


def test_the_dashboard_counts_plays_and_tells_certificates_and_paths(client, db, admin, admin_headers):
    course = _course(db, admin, "Curso 1")
    (module,) = _modules(db, course)
    path = _path(client, admin_headers, [course], title="Ruta de asesores")
    playing = _course(db, admin, "Con actividad")
    activity = _activity(db, _modules(db, playing)[0])
    ana, beto = _learner(db, "ana@test.dev"), _learner(db, "beto@test.dev")
    _assign(client, admin_headers, path, ana)
    _enroll(client, admin_headers, playing, beto)
    client.post(f"{API}/learn/modules/{module.id}/complete", headers=auth_headers(client, ana.email))
    client.post(f"{API}/learn/activities/{activity.id}/attempts", headers=auth_headers(client, beto.email), json={})
    client.post(f"{API}/activities/{activity.id}/preview", headers=admin_headers)  # an admin's preview: no learner

    body = client.get(f"{API}/dashboard", headers=admin_headers).json()

    assert body["learners"]["active_30d"] == 2  # Ana completed a module, Beto only started a play
    items = body["recent_activity"]
    assert sorted(item["kind"] for item in items) == [
        "certificate_issued", "certificate_issued", "completed_course", "path_completed"]
    by_kind = {(item["kind"], item["path_id"] is None): item for item in items}
    codes = {c.kind: c.code for c in db.query(Certificate)}
    assert by_kind[("path_completed", False)] == {
        "kind": "path_completed", "user_id": ana.id, "user_name": "Ana", "course_id": None, "course_title": "",
        "module_title": None, "path_id": path["id"], "path_title": "Ruta de asesores", "certificate_code": None,
        "score": None, "at": by_kind[("path_completed", False)]["at"]}
    assert by_kind[("certificate_issued", True)] == {**by_kind[("certificate_issued", True)], "course_id": course.id,
                                                     "course_title": "Curso 1", "path_title": None,
                                                     "certificate_code": codes["course"]}
    assert by_kind[("certificate_issued", False)] == {**by_kind[("certificate_issued", False)], "course_id": None,
                                                      "path_id": path["id"], "path_title": "Ruta de asesores",
                                                      "certificate_code": codes["path"]}
    assert by_kind[("completed_course", True)]["user_id"] == ana.id
