"""Admin API: courses, modules, evaluations, participants, team and dashboard; and access control."""

import pytest

from app.db.models import Enrollment, Evaluation, Module
from tests.conftest import auth_headers

QUESTION = {"id": "q1", "type": "true_false", "prompt": "El casco es obligatorio", "correct": True, "explanation": ""}


@pytest.fixture
def admin_headers(client, admin):
    return auth_headers(client, admin.email)


def _create_course(client, headers, title="Curso de prueba"):
    response = client.post("/api/v1/courses", headers=headers, json={"title": title, "description": "desc"})
    assert response.status_code == 201, response.text
    return response.json()


def test_course_crud_and_library(client, admin_headers):
    course = _create_course(client, admin_headers)
    assert course["status"] == "draft" and course["modules"] == []

    patched = client.patch(
        f"/api/v1/courses/{course['id']}", headers=admin_headers,
        json={"title": "  Nuevo título ", "settings": {"tone": "cercano", "presenter": False}},
    ).json()
    assert patched["title"] == "Nuevo título"
    assert patched["settings"]["tone"] == "cercano" and patched["settings"]["presenter"] is False

    library = client.get("/api/v1/courses", headers=admin_headers, params={"q": "nuevo"}).json()
    assert [c["id"] for c in library] == [course["id"]]

    assert client.delete(f"/api/v1/courses/{course['id']}", headers=admin_headers).status_code == 204
    assert client.get(f"/api/v1/courses/{course['id']}", headers=admin_headers).status_code == 404


def test_modules_are_appended_reordered_and_renumbered(client, admin_headers):
    course = _create_course(client, admin_headers)
    ids = [
        client.post(f"/api/v1/courses/{course['id']}/modules", headers=admin_headers, json={"title": f"M{i}"}).json()["id"]
        for i in range(1, 4)
    ]

    reordered = client.put(
        f"/api/v1/courses/{course['id']}/modules/order", headers=admin_headers, json={"module_ids": [ids[2], ids[0], ids[1]]}
    ).json()
    assert [(m["id"], m["order"]) for m in reordered] == [(ids[2], 1), (ids[0], 2), (ids[1], 3)]

    assert client.delete(f"/api/v1/modules/{ids[2]}", headers=admin_headers).status_code == 204
    detail = client.get(f"/api/v1/courses/{course['id']}", headers=admin_headers).json()
    assert [(m["id"], m["order"]) for m in detail["modules"]] == [(ids[0], 1), (ids[1], 2)]

    bad = client.put(f"/api/v1/courses/{course['id']}/modules/order", headers=admin_headers, json={"module_ids": [ids[0]]})
    assert bad.status_code == 422


def test_publish_requires_content(client, admin_headers):
    course = _create_course(client, admin_headers)
    empty = client.post(f"/api/v1/courses/{course['id']}/publish", headers=admin_headers)
    assert empty.status_code == 409

    module = client.post(f"/api/v1/courses/{course['id']}/modules", headers=admin_headers, json={"title": "Vacío"}).json()
    still_empty = client.post(f"/api/v1/courses/{course['id']}/publish", headers=admin_headers)
    assert still_empty.status_code == 409
    assert still_empty.json()["detail"]["problems"][0]["module_id"] == module["id"]

    client.patch(f"/api/v1/modules/{module['id']}", headers=admin_headers, json={"content_text": "Contenido"})
    published = client.post(f"/api/v1/courses/{course['id']}/publish", headers=admin_headers).json()
    assert published["status"] == "published" and published["published_at"]

    assert client.post(f"/api/v1/courses/{course['id']}/unpublish", headers=admin_headers).json()["status"] == "draft"


def test_evaluation_upsert_validates_questions(client, admin_headers):
    course = _create_course(client, admin_headers)
    module = client.post(f"/api/v1/courses/{course['id']}/modules", headers=admin_headers, json={"title": "M"}).json()

    invalid = client.put(
        f"/api/v1/modules/{module['id']}/evaluation", headers=admin_headers,
        json={"questions": [{"id": "x", "type": "single_choice", "prompt": "?", "options": ["a"], "correct_index": 3}]},
    )
    assert invalid.status_code == 422

    saved = client.put(
        f"/api/v1/modules/{module['id']}/evaluation", headers=admin_headers,
        json={"questions": [QUESTION], "max_attempts": 4, "passing_score": 60},
    ).json()
    assert saved["questions"][0]["correct"] is True and saved["max_attempts"] == 4

    detail = client.get(f"/api/v1/courses/{course['id']}", headers=admin_headers).json()
    assert detail["modules"][0]["evaluation"] == {"question_count": 1, "max_attempts": 4, "passing_score": 60}

    assert client.delete(f"/api/v1/modules/{module['id']}/evaluation", headers=admin_headers).status_code == 204
    assert client.get(f"/api/v1/modules/{module['id']}/evaluation", headers=admin_headers).status_code == 404


def test_deleting_a_course_removes_its_learning_records(client, db, admin_headers, collaborator):
    course = _create_course(client, admin_headers)
    module = client.post(f"/api/v1/courses/{course['id']}/modules", headers=admin_headers, json={"title": "M", "content_text": "x"}).json()
    client.put(f"/api/v1/modules/{module['id']}/evaluation", headers=admin_headers, json={"questions": [QUESTION]})
    client.post(f"/api/v1/courses/{course['id']}/participants", headers=admin_headers, json={"user_ids": [collaborator.id]})

    assert client.delete(f"/api/v1/courses/{course['id']}", headers=admin_headers).status_code == 204
    db.expire_all()
    assert db.query(Module).count() == 0 and db.query(Evaluation).count() == 0 and db.query(Enrollment).count() == 0


def test_participants_are_assigned_once_and_removed(client, admin_headers, collaborator):
    course = _create_course(client, admin_headers)
    first = client.post(f"/api/v1/courses/{course['id']}/participants", headers=admin_headers, json={"user_ids": [collaborator.id]})
    again = client.post(f"/api/v1/courses/{course['id']}/participants", headers=admin_headers, json={"user_ids": [collaborator.id]})

    assert first.status_code == 200 and len(again.json()) == 1
    assert again.json()[0]["user"]["email"] == collaborator.email
    assert client.delete(f"/api/v1/courses/{course['id']}/participants/{collaborator.id}", headers=admin_headers).status_code == 204
    assert client.get(f"/api/v1/courses/{course['id']}/participants", headers=admin_headers).json() == []


def test_team_management(client, admin, admin_headers):
    created = client.post(
        "/api/v1/users", headers=admin_headers,
        json={"name": "Ana", "email": "ANA@Empresa.com ", "password": "segura-123", "department": "Planta"},
    )
    assert created.status_code == 201 and created.json()["email"] == "ana@empresa.com"
    duplicate = client.post("/api/v1/users", headers=admin_headers, json={"name": "Ana 2", "email": "ana@empresa.com", "password": "segura-123"})
    assert duplicate.status_code == 409

    user_id = created.json()["id"]
    deactivated = client.patch(f"/api/v1/users/{user_id}", headers=admin_headers, json={"is_active": False}).json()
    assert deactivated["is_active"] is False
    assert client.post("/api/v1/auth/login", json={"email": "ana@empresa.com", "password": "segura-123"}).status_code == 403

    self_lockout = client.patch(f"/api/v1/users/{admin.id}", headers=admin_headers, json={"is_active": False})
    assert self_lockout.status_code == 400


def test_dashboard_shape(client, admin_headers):
    body = client.get("/api/v1/dashboard", headers=admin_headers).json()
    assert set(body) >= {"courses", "learners", "enrollments", "completion_rate", "recent_activity", "top_courses"}


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/v1/courses"),
        ("post", "/api/v1/courses"),
        ("get", "/api/v1/users"),
        ("get", "/api/v1/dashboard"),
        ("get", "/api/v1/courses/1/participants"),
        ("get", "/api/v1/courses/1/analytics"),
        ("get", "/api/v1/modules/1/evaluation"),
    ],
)
def test_admin_endpoints_reject_collaborators_and_anonymous(client, collaborator, method, path):
    anonymous = getattr(client, method)(path)
    assert anonymous.status_code == 401
    as_collaborator = getattr(client, method)(path, headers=auth_headers(client, collaborator.email))
    assert as_collaborator.status_code == 403


def test_me_can_update_name_and_password(client, collaborator):
    headers = auth_headers(client, collaborator.email)
    wrong = client.patch("/api/v1/auth/me", headers=headers, json={"current_password": "nope", "new_password": "otra-clave-9"})
    assert wrong.status_code == 400
    updated = client.patch(
        "/api/v1/auth/me", headers=headers,
        json={"name": "Nombre Nuevo", "current_password": "Passw0rd!", "new_password": "otra-clave-9"},
    )
    assert updated.status_code == 200 and updated.json()["name"] == "Nombre Nuevo"
    assert client.post("/api/v1/auth/login", json={"email": collaborator.email, "password": "otra-clave-9"}).status_code == 200


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/v1/courses/ai/voices"),
        ("get", "/api/v1/courses/ai/video-status/1"),
        ("post", "/api/v1/courses/ai/check-all-videos/1"),
        ("get", "/api/v1/videos/"),
        ("post", "/api/v1/slides/generate"),
    ],
)
def test_previous_app_tools_are_admin_only(client, collaborator, method, path):
    response = getattr(client, method)(path, headers=auth_headers(client, collaborator.email))
    assert response.status_code == 403


def test_preview_shows_every_module_unlocked(client, admin_headers):
    course = _create_course(client, admin_headers)
    for title in ("Uno", "Dos"):
        client.post(f"/api/v1/courses/{course['id']}/modules", headers=admin_headers, json={"title": title, "content_text": "x"})

    preview = client.get(f"/api/v1/courses/{course['id']}/preview", headers=admin_headers).json()

    assert [(m["title"], m["unlocked"], m["content_text"]) for m in preview["modules"]] == [("Uno", True, "x"), ("Dos", True, "x")]
    assert preview["enrollment"] is None


def test_archived_courses_disappear_for_learners(client, db, admin_headers, collaborator):
    course = _create_course(client, admin_headers)
    client.post(f"/api/v1/courses/{course['id']}/modules", headers=admin_headers, json={"title": "M", "content_text": "x"})
    client.post(f"/api/v1/courses/{course['id']}/publish", headers=admin_headers)
    client.post(f"/api/v1/courses/{course['id']}/participants", headers=admin_headers, json={"user_ids": [collaborator.id]})
    learner = auth_headers(client, collaborator.email)
    assert len(client.get("/api/v1/learn/courses", headers=learner).json()) == 1

    archived = client.post(f"/api/v1/courses/{course['id']}/archive", headers=admin_headers).json()

    assert archived["status"] == "archived"
    assert client.get("/api/v1/learn/courses", headers=learner).json() == []
    assert [c["id"] for c in client.get("/api/v1/courses", headers=admin_headers, params={"status": "archived"}).json()] == [course["id"]]


def test_module_changes_keep_learner_progress_consistent(client, admin_headers, collaborator):
    course = _create_course(client, admin_headers)
    first = client.post(f"/api/v1/courses/{course['id']}/modules", headers=admin_headers, json={"title": "1", "content_text": "x"}).json()
    client.post(f"/api/v1/courses/{course['id']}/publish", headers=admin_headers)
    client.post(f"/api/v1/courses/{course['id']}/participants", headers=admin_headers, json={"user_ids": [collaborator.id]})
    learner = auth_headers(client, collaborator.email)
    assert client.post(f"/api/v1/learn/modules/{first['id']}/complete", headers=learner).json()["course_completed"] is True

    second = client.post(f"/api/v1/courses/{course['id']}/modules", headers=admin_headers, json={"title": "2", "content_text": "y"}).json()
    [participant] = client.get(f"/api/v1/courses/{course['id']}/participants", headers=admin_headers).json()
    assert participant["status"] == "in_progress" and participant["progress_pct"] == 50.0

    client.delete(f"/api/v1/modules/{second['id']}", headers=admin_headers)
    [participant] = client.get(f"/api/v1/courses/{course['id']}/participants", headers=admin_headers).json()
    assert participant["status"] == "completed" and participant["progress_pct"] == 100.0


def test_person_courses_and_session_revocation(client, admin_headers, collaborator):
    course = _create_course(client, admin_headers)
    client.post(f"/api/v1/courses/{course['id']}/participants", headers=admin_headers, json={"user_ids": [collaborator.id]})
    courses = client.get(f"/api/v1/users/{collaborator.id}/courses", headers=admin_headers).json()
    assert [c["course_id"] for c in courses] == [course["id"]]

    session = client.post("/api/v1/auth/login", json={"email": collaborator.email, "password": "Passw0rd!"}).json()
    client.patch(f"/api/v1/users/{collaborator.id}", headers=admin_headers, json={"password": "nueva-clave-99"})
    refreshed = client.post("/api/v1/auth/refresh", json={"refresh_token": session["refresh_token"]})
    assert refreshed.status_code == 401


def test_login_ignores_email_case(client, collaborator):
    response = client.post("/api/v1/auth/login", json={"email": collaborator.email.upper(), "password": "Passw0rd!"})
    assert response.status_code == 200


def test_deletions_clear_certificates_left_by_the_previous_app(client, db, admin_headers, collaborator):
    from sqlalchemy import text

    db.execute(text("CREATE TABLE certificates (id INTEGER PRIMARY KEY, enrollment_id INTEGER NOT NULL REFERENCES enrollments(id))"))
    db.commit()
    course = _create_course(client, admin_headers)
    client.post(f"/api/v1/courses/{course['id']}/participants", headers=admin_headers, json={"user_ids": [collaborator.id]})
    enrollment = db.query(Enrollment).one()
    db.execute(text("INSERT INTO certificates (enrollment_id) VALUES (:id)"), {"id": enrollment.id})
    db.execute(text("PRAGMA foreign_keys = ON"))
    db.commit()

    assert client.delete(f"/api/v1/courses/{course['id']}", headers=admin_headers).status_code == 204
    assert db.execute(text("SELECT count(*) FROM certificates")).scalar() == 0


def test_explicit_nulls_do_not_break_or_lock_out_users(client, admin, admin_headers, collaborator):
    for body in ({"email": None}, {"name": None}, {"role": None}, {"is_active": None}):
        response = client.patch(f"/api/v1/users/{collaborator.id}", headers=admin_headers, json=body)
        assert response.status_code == 200, body
    assert client.patch(f"/api/v1/users/{admin.id}", headers=admin_headers, json={"is_active": None}).json()["is_active"] is True
