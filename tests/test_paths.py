"""Profiles and learning paths: who gets each path, the order its courses open in, deadlines and completion."""

import copy
from datetime import datetime, timedelta, timezone

import pytest

from app.core.security import hash_password
from app.db.models import (
    Activity,
    ActivityAttempt,
    Course,
    Enrollment,
    Evaluation,
    LearningPathCourse,
    Module,
    ModuleProgress,
    PathAssignment,
    User,
)
from tests.conftest import TEST_PASSWORD, auth_headers
from tests.test_activities_learning import _choose, _start
from tests.test_activity_spec import DECISION
from tests.test_learning_api import QUESTIONS, _perfect_answers

API = "/api/v1"
LOCKED = "Completa el curso «Curso 1» para continuar."


@pytest.fixture
def admin_headers(client, admin):
    return auth_headers(client, admin.email)


def _course(db, admin, title: str, modules: int = 1, status: str = "published") -> Course:
    course = Course(title=title, status=status, source="manual", settings={}, created_by=admin.id)
    db.add(course)
    db.flush()
    db.add_all([
        Module(course_id=course.id, title=f"{title} · {i}", order=i, content_text="Lectura", source="text")
        for i in range(1, modules + 1)
    ])
    db.commit()
    return course


def _courses(db, admin, count: int = 2, **kwargs) -> list[Course]:
    return [_course(db, admin, f"Curso {i}", **kwargs) for i in range(1, count + 1)]


def _person(db, email: str, *, role: str = "collaborator", profile: dict | None = None, active: bool = True,
            can_log_in: bool = False) -> User:
    user = User(name=email.split("@")[0].capitalize(), email=email, role=role, is_active=active,
                password_hash=hash_password(TEST_PASSWORD) if can_log_in else "x",
                profile_id=profile["id"] if profile else None)
    db.add(user)
    db.commit()
    return user


def _profile(client, headers, name: str = "Asesor de flotas") -> dict:
    response = client.post(f"{API}/profiles", headers=headers, json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()


def _path(client, headers, courses: list[Course], *, profiles: tuple[dict, ...] = (), publish: bool = True,
          title: str = "Ruta de asesores", **settings) -> dict:
    path = client.post(f"{API}/paths", headers=headers, json={"title": title}).json()
    put = client.put(f"{API}/paths/{path['id']}/courses", headers=headers,
                     json={"course_ids": [course.id for course in courses]})
    assert put.status_code == 200, put.text
    body: dict = {"profile_ids": [profile["id"] for profile in profiles]}
    if settings:
        body["settings"] = settings
    if publish:
        body["status"] = "published"
    response = client.patch(f"{API}/paths/{path['id']}", headers=headers, json=body)
    assert response.status_code == 200, response.text
    return response.json()


def _assign(client, headers, path: dict, *users: User) -> list[dict]:
    response = client.post(f"{API}/paths/{path['id']}/people", headers=headers,
                           json={"user_ids": [user.id for user in users]})
    assert response.status_code == 200, response.text
    return response.json()


def _modules(db, course: Course) -> list[Module]:
    return db.query(Module).filter(Module.course_id == course.id).order_by(Module.order).all()


def _complete(client, headers, db, course: Course) -> dict:
    """Every module of the course (none has a quiz), in order; the last response."""
    result = {}
    for module in _modules(db, course):
        response = client.post(f"{API}/learn/modules/{module.id}/complete", headers=headers)
        assert response.status_code == 200, response.text
        result = response.json()
    return result


def _assignments(db, user: User) -> dict[int, tuple[str, bool]]:
    """path id -> (source, completed)"""
    db.expire_all()
    rows = db.query(PathAssignment).filter(PathAssignment.user_id == user.id)
    return {row.path_id: (row.source, row.completed_at is not None) for row in rows}


def _enrolled(db, user: User) -> set[int]:
    db.expire_all()
    return {course_id for (course_id,) in db.query(Enrollment.course_id).filter(Enrollment.user_id == user.id)}


def _my_courses(client, headers) -> dict[int, dict]:
    return {course["course_id"]: course for course in client.get(f"{API}/learn/courses", headers=headers).json()}


# ── Profiles ──────────────────────────────────────────────────────────


def test_profiles_have_unique_names_whatever_the_case(client, admin_headers):
    advisor = _profile(client, admin_headers, "Asesor de flotas")
    assert advisor == {**advisor, "description": "", "user_count": 0, "path_count": 0}

    duplicate = client.post(f"{API}/profiles", headers=admin_headers, json={"name": "  asesor DE flotas "})
    assert duplicate.status_code == 409 and duplicate.json() == {"detail": "Ya existe un perfil con ese nombre."}
    manager = _profile(client, admin_headers, "Gerente de sede")
    renamed = client.patch(f"{API}/profiles/{manager['id']}", headers=admin_headers, json={"name": "ASESOR de flotas"})
    assert renamed.status_code == 409
    assert client.post(f"{API}/profiles", headers=admin_headers, json={"name": "   "}).status_code == 422

    edited = client.patch(f"{API}/profiles/{manager['id']}", headers=admin_headers,
                          json={"name": "Gerente", "description": "Lidera la sede"}).json()
    assert (edited["name"], edited["description"]) == ("Gerente", "Lidera la sede")
    assert client.patch(f"{API}/profiles/{advisor['id']}", headers=admin_headers,
                        json={"name": "Asesor de Flotas"}).status_code == 200  # its own name, other case
    listed = client.get(f"{API}/profiles", headers=admin_headers).json()
    assert [p["name"] for p in listed] == ["Asesor de Flotas", "Gerente"]
    assert client.patch(f"{API}/profiles/999", headers=admin_headers, json={"name": "X"}).status_code == 404


def test_people_have_a_profile_and_the_team_filters_by_it(client, db, admin_headers):
    advisor = _profile(client, admin_headers)
    created = client.post(f"{API}/users", headers=admin_headers, json={
        "name": "Ana", "email": "ana@test.dev", "password": "segura-123", "profile_id": advisor["id"],
    })
    assert created.status_code == 201, created.text
    assert created.json()["profile"] == {"id": advisor["id"], "name": "Asesor de flotas"}
    unknown = {"name": "Beto", "email": "beto@test.dev", "password": "segura-123", "profile_id": 999}
    assert client.post(f"{API}/users", headers=admin_headers, json=unknown).status_code == 422
    _person(db, "dani@test.dev")

    filtered = client.get(f"{API}/users", headers=admin_headers, params={"profile_id": advisor["id"]}).json()
    assert [user["email"] for user in filtered] == ["ana@test.dev"]
    assert client.get(f"{API}/profiles", headers=admin_headers).json()[0]["user_count"] == 1

    ana = created.json()["id"]
    assert client.patch(f"{API}/users/{ana}", headers=admin_headers, json={"profile_id": 999}).status_code == 422
    assert client.patch(f"{API}/users/{ana}", headers=admin_headers, json={"name": "Ana M."}).json()["profile"]
    removed = client.patch(f"{API}/users/{ana}", headers=admin_headers, json={"profile_id": None})
    assert removed.json()["profile"] is None


# ── Assignments ───────────────────────────────────────────────────────


def test_publishing_gives_the_path_to_the_active_collaborators_of_its_profiles(client, db, admin, admin_headers):
    advisor = _profile(client, admin_headers)
    ana = _person(db, "ana@test.dev", profile=advisor)
    _person(db, "beto@test.dev", profile=advisor, active=False)
    carla = _person(db, "carla@test.dev", role="admin", profile=advisor)  # admins never get one by profile
    _person(db, "dani@test.dev")
    first = _course(db, admin, "Curso 1")
    draft = _course(db, admin, "Curso 2", status="draft")
    path = _path(client, admin_headers, [first, draft], profiles=(advisor,), publish=False, due_days=15)
    assert path["people_count"] == 0  # a draft gives nothing yet

    published = client.patch(f"{API}/paths/{path['id']}", headers=admin_headers, json={"status": "published"}).json()

    assert published["status"] == "published" and published["published_at"] and published["people_count"] == 1
    assert _assignments(db, ana) == {path["id"]: ("profile", False)}
    assert _enrolled(db, ana) == {first.id, draft.id}  # every course of the path, drafts too
    assert _assignments(db, carla) == {}
    (person,) = client.get(f"{API}/paths/{path['id']}/people", headers=admin_headers).json()
    assert person["user"] == {"id": ana.id, "name": "Ana", "email": "ana@test.dev", "position": "",
                              "department": "", "profile": {"id": advisor["id"], "name": "Asesor de flotas"}}
    assert person == {**person, "source": "profile", "removable": False, "status": "not_started",
                      "progress_pct": 0.0, "current_course_id": first.id, "completed_at": None}
    assigned_at, due_at = (datetime.fromisoformat(person[key]) for key in ("assigned_at", "due_at"))
    assert due_at - assigned_at == timedelta(days=15)
    assert person["steps"] == [{"course_id": first.id, "status": "available", "progress_pct": 0.0}]  # published only
    assert client.get(f"{API}/profiles", headers=admin_headers).json()[0]["path_count"] == 1

    # Someone who gets the profile later gets the path too.
    eva = client.post(f"{API}/users", headers=admin_headers, json={
        "name": "Eva", "email": "eva@test.dev", "password": "segura-123", "profile_id": advisor["id"],
    }).json()
    assert _assignments(db, db.get(User, eva["id"])) == {path["id"]: ("profile", False)}


def test_people_assigned_by_hand_can_be_removed_but_not_those_of_its_profile(client, db, admin, admin_headers):
    advisor = _profile(client, admin_headers)
    ana = _person(db, "ana@test.dev", profile=advisor)
    dani = _person(db, "dani@test.dev")
    inactive = _person(db, "beto@test.dev", active=False)
    (course,) = _courses(db, admin, 1)
    path = _path(client, admin_headers, [course], profiles=(advisor,))

    people = _assign(client, admin_headers, path, dani, admin)
    assert [(p["user"]["id"], p["source"], p["removable"]) for p in people] == [
        (admin.id, "manual", True), (dani.id, "manual", True),
    ]
    assert course.id in _enrolled(db, dani) and course.id in _enrolled(db, admin)
    for user_ids in ([inactive.id], [999]):
        bad = client.post(f"{API}/paths/{path['id']}/people", headers=admin_headers, json={"user_ids": user_ids})
        assert bad.status_code == 422
    assert client.post(f"{API}/paths/{path['id']}/people", headers=admin_headers,
                       json={"user_ids": []}).status_code == 422
    # Hers by profile, now by hand too: it stays if her profile changes, and can't go while her profile gives it.
    again = _assign(client, admin_headers, path, ana)
    assert [(p["source"], p["removable"]) for p in again] == [("manual", False)]

    assert client.delete(f"{API}/paths/{path['id']}/people/{dani.id}", headers=admin_headers).status_code == 204
    assert _assignments(db, dani) == {} and course.id in _enrolled(db, dani)  # the course and progress stay
    by_profile = client.delete(f"{API}/paths/{path['id']}/people/{ana.id}", headers=admin_headers)
    assert by_profile.status_code == 409
    assert by_profile.json() == {
        "detail": "Esta persona tiene la ruta por su perfil «Asesor de flotas». Cambia su perfil para quitarla."
    }
    assert client.delete(f"{API}/paths/{path['id']}/people/{dani.id}", headers=admin_headers).status_code == 404
    listed = client.get(f"{API}/paths/{path['id']}/people", headers=admin_headers).json()
    assert [p["user"]["id"] for p in listed] == [admin.id, ana.id]

    client.patch(f"{API}/users/{ana.id}", headers=admin_headers, json={"profile_id": None})
    assert _assignments(db, ana) == {path["id"]: ("manual", False)}
    assert client.delete(f"{API}/paths/{path['id']}/people/{ana.id}", headers=admin_headers).status_code == 204


def test_a_new_profile_brings_its_paths_and_takes_the_old_ones_unless_completed(client, db, admin, admin_headers):
    advisor, manager = _profile(client, admin_headers), _profile(client, admin_headers, "Gerente")
    ana = _person(db, "ana@test.dev", profile=advisor, can_log_in=True)
    pending_course, done_course, manager_course = _course(db, admin, "Pendiente"), _course(db, admin, "Hecho"), \
        _course(db, admin, "Gerencia")
    pending = _path(client, admin_headers, [pending_course], profiles=(advisor,), title="Pendiente")
    done = _path(client, admin_headers, [done_course], profiles=(advisor,), title="Hecha")
    managers = _path(client, admin_headers, [manager_course], profiles=(manager,), title="Gerentes")
    learner = auth_headers(client, ana.email)
    client.put(f"{API}/learn/modules/{_modules(db, pending_course)[0].id}/position", headers=learner,
               json={"seconds": 30})
    completed = _complete(client, learner, db, done_course)["path_completed"]
    assert (completed["id"], completed["title"]) == (done["id"], "Hecha")

    client.patch(f"{API}/users/{ana.id}", headers=admin_headers, json={"profile_id": manager["id"]})

    assert _assignments(db, ana) == {done["id"]: ("profile", True), managers["id"]: ("profile", False)}
    assert {pending_course.id, done_course.id, manager_course.id} <= _enrolled(db, ana)  # enrollments are kept
    assert db.query(ModuleProgress).filter(ModuleProgress.last_position_seconds == 30).count() == 1

    client.patch(f"{API}/users/{ana.id}", headers=admin_headers, json={"profile_id": None})
    assert _assignments(db, ana) == {done["id"]: ("profile", True)}
    # A completed path no longer backed by a profile can be taken away by hand.
    assert client.delete(f"{API}/paths/{done['id']}/people/{ana.id}", headers=admin_headers).status_code == 204


def test_a_completed_path_stays_whatever_the_path_gets_after_a_profile_change(client, db, admin, admin_headers):
    """Her history: a module added to its course or one more course reopen the path only for whoever still takes it
    (the next sync would drop a reopened one). She is enrolled in the new course all the same."""
    advisor, manager = _profile(client, admin_headers), _profile(client, admin_headers, "Gerente")
    ana = _person(db, "ana@test.dev", profile=advisor, can_log_in=True)
    beto = _person(db, "beto@test.dev", profile=advisor, can_log_in=True)
    first, second = _courses(db, admin, 2)
    path = _path(client, admin_headers, [first], profiles=(advisor,))
    for person in (ana, beto):
        _complete(client, auth_headers(client, person.email), db, first)
    client.patch(f"{API}/users/{ana.id}", headers=admin_headers, json={"profile_id": manager["id"]})

    client.post(f"{API}/courses/{first.id}/modules", headers=admin_headers,
                json={"title": "Nuevo", "content_text": "x"})
    assert _assignments(db, ana) == {path["id"]: ("profile", True)}
    assert _assignments(db, beto) == {path["id"]: ("profile", False)}
    client.put(f"{API}/paths/{path['id']}/courses", headers=admin_headers, json={"course_ids": [first.id, second.id]})
    client.patch(f"{API}/users/{ana.id}", headers=admin_headers, json={"profile_id": None})

    assert _assignments(db, ana) == {path["id"]: ("profile", True)}
    assert second.id in _enrolled(db, ana)


def test_a_path_completed_while_archived_stays_after_a_profile_change(client, db, admin, admin_headers):
    advisor, manager = _profile(client, admin_headers), _profile(client, admin_headers, "Gerente")
    ana = _person(db, "ana@test.dev", profile=advisor, can_log_in=True)
    (course,) = _courses(db, admin, 1)
    path = _path(client, admin_headers, [course], profiles=(advisor,))
    client.patch(f"{API}/paths/{path['id']}", headers=admin_headers, json={"status": "archived"})
    _complete(client, auth_headers(client, ana.email), db, course)  # an archived path stores nothing

    client.patch(f"{API}/users/{ana.id}", headers=admin_headers, json={"profile_id": manager["id"]})

    assert _assignments(db, ana) == {path["id"]: ("profile", False)}  # kept: its courses are completed
    client.patch(f"{API}/paths/{path['id']}", headers=admin_headers, json={"status": "published"})
    assert _assignments(db, ana) == {path["id"]: ("profile", True)}


def test_deactivated_people_keep_their_paths_and_get_them_once_active(client, db, admin, admin_headers):
    advisor = _profile(client, admin_headers)
    ana = _person(db, "ana@test.dev", profile=advisor)
    beto = _person(db, "beto@test.dev", profile=advisor, active=False)
    (course,) = _courses(db, admin, 1)
    path = _path(client, admin_headers, [course], profiles=(advisor,))

    client.patch(f"{API}/users/{ana.id}", headers=admin_headers, json={"is_active": False})
    client.patch(f"{API}/users/{beto.id}", headers=admin_headers, json={"is_active": True})

    assert _assignments(db, ana) == {path["id"]: ("profile", False)}
    assert _assignments(db, beto) == {path["id"]: ("profile", False)}  # active again: their profile's paths


def test_admins_keep_no_path_of_their_profile(client, db, admin, admin_headers):
    """Made admin: the paths their profile gave them go, unless completed; a collaborator again, they come back."""
    advisor = _profile(client, admin_headers)
    ana = _person(db, "ana@test.dev", profile=advisor, can_log_in=True)
    first, second = _courses(db, admin, 2)
    pending = _path(client, admin_headers, [first], profiles=(advisor,), title="Pendiente")
    done = _path(client, admin_headers, [second], profiles=(advisor,), title="Hecha")
    _complete(client, auth_headers(client, ana.email), db, second)

    client.patch(f"{API}/users/{ana.id}", headers=admin_headers, json={"role": "admin"})

    assert _assignments(db, ana) == {done["id"]: ("profile", True)}
    assert _enrolled(db, ana) == {first.id, second.id}
    client.patch(f"{API}/users/{ana.id}", headers=admin_headers, json={"role": "collaborator"})
    assert _assignments(db, ana) == {pending["id"]: ("profile", False), done["id"]: ("profile", True)}


def test_deleting_a_profile_keeps_courses_and_completed_paths(client, db, admin, admin_headers):
    advisor = _profile(client, admin_headers)
    ana = _person(db, "ana@test.dev", profile=advisor, can_log_in=True)
    first, second = _courses(db, admin, 2)
    pending = _path(client, admin_headers, [first], profiles=(advisor,), title="Pendiente")
    done = _path(client, admin_headers, [second], profiles=(advisor,), title="Hecha")
    _complete(client, auth_headers(client, ana.email), db, second)

    assert client.delete(f"{API}/profiles/{advisor['id']}", headers=admin_headers).status_code == 204

    db.expire_all()
    assert db.get(User, ana.id).profile_id is None
    assert _assignments(db, ana) == {done["id"]: ("profile", True)}
    assert _enrolled(db, ana) == {first.id, second.id}
    assert client.get(f"{API}/paths/{pending['id']}", headers=admin_headers).json()["profiles"] == []
    assert client.get(f"{API}/profiles", headers=admin_headers).json() == []
    assert client.delete(f"{API}/profiles/{advisor['id']}", headers=admin_headers).status_code == 404


def test_unlinking_a_profile_takes_the_path_from_its_people(client, db, admin, admin_headers):
    advisor, manager = _profile(client, admin_headers), _profile(client, admin_headers, "Gerente")
    ana = _person(db, "ana@test.dev", profile=advisor)
    gina = _person(db, "gina@test.dev", profile=manager)
    (course,) = _courses(db, admin, 1)
    path = _path(client, admin_headers, [course], profiles=(advisor,))

    relinked = client.patch(f"{API}/paths/{path['id']}", headers=admin_headers, json={"profile_ids": [manager["id"]]})

    assert relinked.json()["profiles"] == [{"id": manager["id"], "name": "Gerente"}]
    assert _assignments(db, ana) == {} and course.id in _enrolled(db, ana)
    assert _assignments(db, gina) == {path["id"]: ("profile", False)}
    unknown = client.patch(f"{API}/paths/{path['id']}", headers=admin_headers, json={"profile_ids": [999]})
    assert unknown.status_code == 422


def test_a_course_of_a_path_stays_while_the_person_has_the_path(client, db, admin, admin_headers):
    """Taken out of one course of their path, the courses after it would stay locked for good."""
    ana = _person(db, "ana@test.dev")
    first, second = _courses(db, admin, 2)
    loose = _course(db, admin, "Suelto")
    path = _path(client, admin_headers, [first, second], title="Inducción")
    _assign(client, admin_headers, path, ana)
    client.post(f"{API}/courses/{loose.id}/participants", headers=admin_headers, json={"user_ids": [ana.id]})

    def unassign(course: Course):
        return client.delete(f"{API}/courses/{course.id}/participants/{ana.id}", headers=admin_headers)

    blocked = unassign(first)
    assert blocked.status_code == 409 and blocked.json() == {
        "detail": "Esta persona tiene este curso por la ruta «Inducción». Quítale la ruta para retirarla del curso."
    }
    assert unassign(loose).status_code == 204
    client.patch(f"{API}/paths/{path['id']}", headers=admin_headers, json={"status": "archived"})
    assert unassign(second).status_code == 204  # an archived path holds none of its courses
    assert client.delete(f"{API}/paths/{path['id']}/people/{ana.id}", headers=admin_headers).status_code == 204
    assert unassign(first).status_code == 204
    assert _enrolled(db, ana) == set()


# ── Paths ─────────────────────────────────────────────────────────────


def test_path_crud_list_and_detail(client, db, admin, admin_headers):
    first, second = _courses(db, admin, 2, modules=2)
    created = client.post(f"{API}/paths", headers=admin_headers,
                          json={"title": " Inducción ", "description": "Bienvenida"})
    assert created.status_code == 201
    path = created.json()
    assert path == {**path, "title": "Inducción", "status": "draft", "profiles": [], "courses": [], "people_count": 0,
                    "completed_count": 0, "published_at": None,
                    "settings": {"sequential": True, "due_days": None, "certificate": True}}
    detail = client.put(f"{API}/paths/{path['id']}/courses", headers=admin_headers,
                        json={"course_ids": [second.id, first.id]}).json()
    assert [(c["course_id"], c["order"], c["module_count"], c["status"]) for c in detail["courses"]] == [
        (second.id, 1, 2, "published"), (first.id, 2, 2, "published"),
    ]
    assert detail["courses"][0] == {**detail["courses"][0], "title": "Curso 2", "cover_url": None}

    patched = client.patch(f"{API}/paths/{path['id']}", headers=admin_headers,
                           json={"settings": {"due_days": 30}, "title": None}).json()
    assert patched["title"] == "Inducción"
    assert patched["settings"] == {"sequential": True, "due_days": 30, "certificate": True}
    patched = client.patch(f"{API}/paths/{path['id']}", headers=admin_headers,
                           json={"settings": {"certificate": False}}).json()
    assert patched["settings"] == {"sequential": True, "due_days": 30, "certificate": False}  # merged
    assert client.patch(f"{API}/paths/{path['id']}", headers=admin_headers,
                        json={"settings": {"due_days": 0}}).status_code == 422
    assert client.patch(f"{API}/paths/{path['id']}", headers=admin_headers, json={"status": "x"}).status_code == 422

    other = client.post(f"{API}/paths", headers=admin_headers, json={"title": "Archivada"}).json()
    client.patch(f"{API}/paths/{other['id']}", headers=admin_headers, json={"status": "archived"})
    rows = client.get(f"{API}/paths", headers=admin_headers).json()
    assert [row["id"] for row in rows] == [path["id"]]  # archived ones only when asked for
    assert rows[0] == {**rows[0], "course_count": 2, "people_count": 0, "completed_count": 0, "completion_rate": 0.0}
    archived = client.get(f"{API}/paths", headers=admin_headers, params={"status": "archived"}).json()
    assert [row["id"] for row in archived] == [other["id"]]

    assert client.delete(f"{API}/paths/{path['id']}", headers=admin_headers).status_code == 204
    assert client.get(f"{API}/paths/{path['id']}", headers=admin_headers).status_code == 404
    assert db.query(LearningPathCourse).count() == 0


def test_path_courses_are_validated(client, db, admin, admin_headers):
    first, second = _courses(db, admin, 2)
    archived = _course(db, admin, "Viejo", status="archived")
    path = client.post(f"{API}/paths", headers=admin_headers, json={"title": "Ruta"}).json()
    url = f"{API}/paths/{path['id']}/courses"

    assert client.put(url, headers=admin_headers, json={"course_ids": [first.id, first.id]}).status_code == 422
    assert client.put(url, headers=admin_headers, json={"course_ids": [first.id, 999]}).status_code == 422
    assert client.put(url, headers=admin_headers, json={"course_ids": [archived.id]}).status_code == 422
    assert client.put(url, headers=admin_headers, json={"course_ids": list(range(1, 32))}).status_code == 422
    assert client.put(url, headers=admin_headers, json={"course_ids": [first.id, second.id]}).status_code == 200
    # A course archived while in the path stays when the list is sent again (to reorder).
    client.post(f"{API}/courses/{first.id}/archive", headers=admin_headers)
    reordered = client.put(url, headers=admin_headers, json={"course_ids": [second.id, first.id]})
    assert reordered.status_code == 200
    assert [c["course_id"] for c in reordered.json()["courses"]] == [second.id, first.id]
    emptied = client.put(url, headers=admin_headers, json={"course_ids": []}).json()
    assert emptied["courses"] == []


def test_a_path_without_courses_can_be_published_and_is_never_completed(client, db, admin, admin_headers):
    ana = _person(db, "ana@test.dev", can_log_in=True)
    path = _path(client, admin_headers, [])
    _assign(client, admin_headers, path, ana)

    (mine,) = client.get(f"{API}/learn/paths", headers=auth_headers(client, ana.email)).json()

    assert mine == {**mine, "status": "not_started", "steps": [], "progress_pct": 0.0, "current_course_id": None}
    assert _assignments(db, ana) == {path["id"]: ("manual", False)}


# ── Locks ─────────────────────────────────────────────────────────────


def test_a_sequential_path_opens_its_courses_in_order(client, db, admin, admin_headers):
    ana = _person(db, "ana@test.dev", can_log_in=True)
    first, second = _courses(db, admin, 2)
    path = _path(client, admin_headers, [first, second])
    _assign(client, admin_headers, path, ana)
    learner = auth_headers(client, ana.email)
    (locked_module,) = _modules(db, second)

    courses = _my_courses(client, learner)
    assert courses[first.id] == {**courses[first.id], "locked": False, "locked_by": None,
                                 "path": {"id": path["id"], "title": "Ruta de asesores", "step": 1, "steps": 2}}
    assert courses[second.id] == {**courses[second.id], "locked": True,
                                  "locked_by": {"course_id": first.id, "title": "Curso 1"},
                                  "path": {"id": path["id"], "title": "Ruta de asesores", "step": 2, "steps": 2}}
    player = client.get(f"{API}/learn/courses/{second.id}", headers=learner)
    assert player.status_code == 200  # opens, to say what unlocks it
    body = player.json()
    assert body["locked"] and body["locked_by"] == {"course_id": first.id, "title": "Curso 1"}
    assert body["path"] == {"id": path["id"], "title": "Ruta de asesores", "step": 2, "steps": 2}
    assert [(m["unlocked"], m["content_text"]) for m in body["modules"]] == [(False, None)]
    for method, url, payload in (
        ("post", f"{API}/learn/modules/{locked_module.id}/complete", None),
        ("put", f"{API}/learn/modules/{locked_module.id}/position", {"seconds": 5}),
        ("get", f"{API}/learn/modules/{locked_module.id}/quiz", None),
        ("post", f"{API}/learn/modules/{locked_module.id}/quiz/attempts", {"answers": []}),
    ):
        response = client.request(method, url, headers=learner, json=payload)
        assert response.status_code == 403 and response.json() == {"detail": LOCKED}, (method, url)
    assert not client.get(f"{API}/learn/courses/{first.id}", headers=learner).json()["locked"]

    finished_first = _complete(client, learner, db, first)
    assert finished_first == {**finished_first, "course_completed": True, "path_completed": None,
                              "next_course": {"course_id": second.id, "title": "Curso 2"}}
    assert not _my_courses(client, learner)[second.id]["locked"]

    finished_second = _complete(client, learner, db, second)
    assert finished_second == {**finished_second, "course_completed": True, "next_course": None}
    completed = finished_second["path_completed"]
    assert (completed["id"], completed["title"]) == (path["id"], "Ruta de asesores")
    assert _assignments(db, ana) == {path["id"]: ("manual", True)}


def test_without_sequential_order_every_course_is_open(client, db, admin, admin_headers):
    ana = _person(db, "ana@test.dev", can_log_in=True)
    first, second = _courses(db, admin, 2)
    path = _path(client, admin_headers, [first, second], sequential=False)
    _assign(client, admin_headers, path, ana)
    learner = auth_headers(client, ana.email)

    assert not _my_courses(client, learner)[second.id]["locked"]
    finished = _complete(client, learner, db, second)  # out of order
    assert finished["next_course"] == {"course_id": first.id, "title": "Curso 1"}

    client.patch(f"{API}/paths/{path['id']}", headers=admin_headers, json={"settings": {"sequential": True}})
    assert not _my_courses(client, learner)[second.id]["locked"]  # completed: never locked again


def test_unpublished_courses_are_skipped(client, db, admin, admin_headers):
    ana = _person(db, "ana@test.dev", can_log_in=True)
    draft = _course(db, admin, "Borrador", status="draft")
    first, second = _courses(db, admin, 2)
    path = _path(client, admin_headers, [draft, first, second])
    _assign(client, admin_headers, path, ana)
    learner = auth_headers(client, ana.email)

    courses = _my_courses(client, learner)
    assert draft.id not in courses
    assert not courses[first.id]["locked"] and courses[first.id]["path"]["step"] == 1
    assert courses[second.id]["locked_by"] == {"course_id": first.id, "title": "Curso 1"}
    (mine,) = client.get(f"{API}/learn/paths", headers=learner).json()
    assert [step["course_id"] for step in mine["steps"]] == [first.id, second.id]


@pytest.mark.parametrize("progress", ["position", "play"])
def test_a_course_already_started_stays_open(client, db, admin, admin_headers, progress):
    ana = _person(db, "ana@test.dev", can_log_in=True)
    first, second = _courses(db, admin, 2)
    enrollment = Enrollment(user_id=ana.id, course_id=second.id, status="in_progress", progress_pct=0)
    db.add(enrollment)
    db.flush()
    (module,) = _modules(db, second)
    if progress == "position":
        db.add(ModuleProgress(enrollment_id=enrollment.id, module_id=module.id, last_position_seconds=12))
    else:
        activity = Activity(module_id=module.id, type="decision", title="A", spec={}, status="ready")
        db.add(activity)
        db.flush()
        db.add(ActivityAttempt(activity_id=activity.id, user_id=ana.id, enrollment_id=enrollment.id, version=1,
                               state={}, score=0, max_score=10))
    db.commit()
    path = _path(client, admin_headers, [first, second])

    _assign(client, admin_headers, path, ana)

    learner = auth_headers(client, ana.email)
    assert not _my_courses(client, learner)[second.id]["locked"]
    (mine,) = client.get(f"{API}/learn/paths", headers=learner).json()
    assert [step["status"] for step in mine["steps"]] == ["available", "in_progress"]
    assert mine["status"] == "in_progress"


def test_a_course_in_two_paths_opens_when_one_of_them_allows_it(client, db, admin, admin_headers):
    ana = _person(db, "ana@test.dev", can_log_in=True)
    first, second, shared = _courses(db, admin, 3)
    one = _path(client, admin_headers, [first, shared], title="Uno")
    two = _path(client, admin_headers, [second, shared], title="Dos")
    _assign(client, admin_headers, one, ana)
    _assign(client, admin_headers, two, ana)
    learner = auth_headers(client, ana.email)

    assert _my_courses(client, learner)[shared.id]["locked_by"] == {"course_id": first.id, "title": "Curso 1"}
    _complete(client, learner, db, first)  # path "Uno" lets it open, even if "Dos" still waits for its course
    courses = _my_courses(client, learner)
    assert not courses[shared.id]["locked"] and courses[shared.id]["path"]["title"] == "Uno"
    statuses = {path["title"]: [s["status"] for s in path["steps"]]
                for path in client.get(f"{API}/learn/paths", headers=learner).json()}
    assert statuses == {"Uno": ["completed", "available"], "Dos": ["available", "available"]}


def test_draft_and_archived_paths_lock_nothing(client, db, admin, admin_headers):
    ana = _person(db, "ana@test.dev", can_log_in=True)
    first, second = _courses(db, admin, 2)
    path = _path(client, admin_headers, [first, second])
    _assign(client, admin_headers, path, ana)
    learner = auth_headers(client, ana.email)
    assert _my_courses(client, learner)[second.id]["locked"]

    for status in ("archived", "draft"):
        client.patch(f"{API}/paths/{path['id']}", headers=admin_headers, json={"status": status})
        courses = _my_courses(client, learner)
        assert not courses[second.id]["locked"] and courses[second.id]["path"] is None
        assert client.get(f"{API}/learn/paths", headers=learner).json() == []
        assert _assignments(db, ana) == {path["id"]: ("manual", False)}  # kept
    (module,) = _modules(db, second)
    assert client.post(f"{API}/learn/modules/{module.id}/complete", headers=learner).status_code == 200


def test_the_admin_sees_a_persons_locks(client, db, admin, admin_headers):
    ana = _person(db, "ana@test.dev")
    first, second = _courses(db, admin, 2)
    _assign(client, admin_headers, _path(client, admin_headers, [first, second]), ana)

    courses = {c["course_id"]: c for c in client.get(f"{API}/users/{ana.id}/courses", headers=admin_headers).json()}

    assert courses[second.id]["locked_by"] == {"course_id": first.id, "title": "Curso 1"}
    assert not courses[first.id]["locked"]


# ── The learner's paths, deadlines and completion ─────────────────────


def test_my_paths(client, db, admin, admin_headers):
    ana = _person(db, "ana@test.dev", can_log_in=True)
    first, second = _courses(db, admin, 2, modules=2)
    later = _course(db, admin, "Curso 3")
    path = _path(client, admin_headers, [first, second], due_days=10)
    other = _path(client, admin_headers, [later], title="Otra")
    _assign(client, admin_headers, path, ana)
    _assign(client, admin_headers, other, ana)
    learner = auth_headers(client, ana.email)
    client.post(f"{API}/learn/modules/{_modules(db, first)[0].id}/complete", headers=learner)

    mine = client.get(f"{API}/learn/paths", headers=learner).json()

    assert [p["id"] for p in mine] == [path["id"], other["id"]]  # by assignment
    current = mine[0]
    assert current == {**current, "title": "Ruta de asesores", "description": "", "status": "in_progress",
                       "progress_pct": 25.0, "current_course_id": first.id, "completed_at": None}
    assigned_at = datetime.fromisoformat(current["assigned_at"])
    assert datetime.fromisoformat(current["due_at"]) - assigned_at == timedelta(days=10)
    assert current["steps"] == [
        {"course_id": first.id, "title": "Curso 1", "cover_url": None, "status": "in_progress", "progress_pct": 50.0,
         "total_modules": 2, "completed_modules": 1, "locked_by": None},
        {"course_id": second.id, "title": "Curso 2", "cover_url": None, "status": "locked", "progress_pct": 0.0,
         "total_modules": 2, "completed_modules": 0, "locked_by": {"course_id": first.id, "title": "Curso 1"}},
    ]
    assert mine[1]["status"] == "not_started" and mine[1]["due_at"] is None


def test_deadlines_follow_the_path(client, db, admin, admin_headers):
    ana = _person(db, "ana@test.dev", can_log_in=True)
    beto = _person(db, "beto@test.dev")
    (course,) = _courses(db, admin, 1)
    path = _path(client, admin_headers, [course], due_days=10)
    _assign(client, admin_headers, path, ana, beto)

    def deadlines() -> dict[int, timedelta | None]:
        db.expire_all()
        return {row.user_id: (row.due_at - row.assigned_at) if row.due_at else None
                for row in db.query(PathAssignment)}

    assert deadlines() == {ana.id: timedelta(days=10), beto.id: timedelta(days=10)}
    _complete(client, auth_headers(client, ana.email), db, course)
    client.patch(f"{API}/paths/{path['id']}", headers=admin_headers, json={"settings": {"due_days": 20}})
    assert deadlines() == {ana.id: timedelta(days=10), beto.id: timedelta(days=20)}  # completed: as it was
    client.patch(f"{API}/paths/{path['id']}", headers=admin_headers, json={"settings": {"due_days": None}})
    assert deadlines() == {ana.id: timedelta(days=10), beto.id: None}

    # Past the deadline without completing it: overdue, for the learner and for the admin.
    assignment = db.query(PathAssignment).filter(PathAssignment.user_id == beto.id).one()
    assignment.assigned_at = datetime.now(timezone.utc) - timedelta(days=5)
    db.commit()
    client.patch(f"{API}/paths/{path['id']}", headers=admin_headers, json={"settings": {"due_days": 2}})
    people = {p["user"]["id"]: p["status"] for p in
              client.get(f"{API}/paths/{path['id']}/people", headers=admin_headers).json()}
    assert people == {ana.id: "completed", beto.id: "overdue"}


def test_completion_follows_the_courses_of_the_path(client, db, admin, admin_headers):
    """Completed when every published course is (one module removed for everyone, in bulk); a new module or a newly
    published course reopens it, like an enrollment."""
    ana = _person(db, "ana@test.dev", can_log_in=True)
    first = _course(db, admin, "Curso 1", modules=2)
    draft = _course(db, admin, "Curso 2", status="draft")
    path = _path(client, admin_headers, [first, draft])
    _assign(client, admin_headers, path, ana)
    learner = auth_headers(client, ana.email)
    done, pending = _modules(db, first)
    client.post(f"{API}/learn/modules/{done.id}/complete", headers=learner)
    assert _assignments(db, ana) == {path["id"]: ("manual", False)}

    assert client.delete(f"{API}/modules/{pending.id}", headers=admin_headers).status_code == 204
    assert _assignments(db, ana) == {path["id"]: ("manual", True)}
    (mine,) = client.get(f"{API}/learn/paths", headers=learner).json()
    assert mine == {**mine, "status": "completed", "progress_pct": 100.0, "current_course_id": None}
    assert mine["completed_at"]
    detail = client.get(f"{API}/paths/{path['id']}", headers=admin_headers).json()
    assert (detail["people_count"], detail["completed_count"]) == (1, 1)
    assert client.get(f"{API}/paths", headers=admin_headers).json()[0]["completion_rate"] == 100.0

    client.post(f"{API}/courses/{first.id}/modules", headers=admin_headers,
                json={"title": "Nuevo", "content_text": "x"})
    assert _assignments(db, ana) == {path["id"]: ("manual", False)}
    client.post(f"{API}/learn/modules/{_modules(db, first)[-1].id}/complete", headers=learner)
    assert _assignments(db, ana) == {path["id"]: ("manual", True)}

    assert client.post(f"{API}/courses/{draft.id}/publish", headers=admin_headers).status_code == 200
    assert _assignments(db, ana) == {path["id"]: ("manual", False)}  # one more course to take
    client.post(f"{API}/courses/{draft.id}/unpublish", headers=admin_headers)
    assert _assignments(db, ana) == {path["id"]: ("manual", True)}


def test_deleting_a_course_takes_it_out_of_its_paths(client, db, admin, admin_headers):
    ana = _person(db, "ana@test.dev", can_log_in=True)
    first, second = _courses(db, admin, 2)
    path = _path(client, admin_headers, [first, second])
    _assign(client, admin_headers, path, ana)
    _complete(client, auth_headers(client, ana.email), db, first)
    assert _assignments(db, ana) == {path["id"]: ("manual", False)}

    assert client.delete(f"{API}/courses/{second.id}", headers=admin_headers).status_code == 204

    assert [c["course_id"] for c in client.get(f"{API}/paths/{path['id']}", headers=admin_headers).json()["courses"]] \
        == [first.id]
    assert db.query(LearningPathCourse).filter(LearningPathCourse.course_id == second.id).count() == 0
    assert _assignments(db, ana) == {path["id"]: ("manual", True)}  # nothing left to take


def test_finishing_the_last_activity_reports_the_path(client, db, admin, admin_headers):
    """A course completed by a play (its last move): the move tells what it opened, and `finish` the same again. The
    next course's activities can't be played until then."""
    ana = _person(db, "ana@test.dev", can_log_in=True)
    first, second = _courses(db, admin, 2)
    activity, later = (
        Activity(module_id=_modules(db, course)[0].id, order=1, type="decision", title="Cliente", intro="",
                 spec=copy.deepcopy(DECISION), status="ready")
        for course in (first, second)
    )
    db.add_all([activity, later])
    db.commit()
    path = _path(client, admin_headers, [first, second])
    _assign(client, admin_headers, path, ana)
    learner = auth_headers(client, ana.email)
    locked = client.post(f"{API}/learn/activities/{later.id}/attempts", headers=learner, json={})
    assert locked.status_code == 403 and locked.json() == {"detail": LOCKED}

    play = _start(client, learner, activity.id)
    last = _choose(client, learner, play, "Le doy").json()  # it ends the conversation

    assert last["done"] and last["course_completed"]
    assert last["next_course"] == {"course_id": second.id, "title": "Curso 2"} and last["path_completed"] is None
    finished = client.post(f"{API}/learn/activity-attempts/{play['attempt_id']}/finish", headers=learner).json()
    assert finished["next_course"] == {"course_id": second.id, "title": "Curso 2"}
    assert not _start(client, learner, later.id)["resumed"]  # open now


def test_collaborators_cannot_manage_paths_or_profiles(client, collaborator):
    headers = auth_headers(client, collaborator.email)
    for method, url in (("get", "/paths"), ("post", "/paths"), ("get", "/profiles"), ("post", "/profiles"),
                        ("get", "/paths/1/people"), ("delete", "/profiles/1")):
        assert client.request(method, f"{API}{url}", headers=headers, json={}).status_code == 403, url
    assert client.get(f"{API}/learn/paths", headers=headers).json() == []


def test_passing_the_last_quiz_reports_the_next_course(client, db, admin, admin_headers):
    ana = _person(db, "ana@test.dev", can_log_in=True)
    first, second = _courses(db, admin, 2)
    (module,) = _modules(db, first)
    evaluation = Evaluation(module_id=module.id, spec=QUESTIONS, max_attempts=2, passing_score=80)
    db.add(evaluation)
    db.commit()
    _assign(client, admin_headers, _path(client, admin_headers, [first, second]), ana)
    learner = auth_headers(client, ana.email)

    failed = client.post(f"{API}/learn/modules/{module.id}/quiz/attempts", headers=learner, json={"answers": []})
    passed = client.post(f"{API}/learn/modules/{module.id}/quiz/attempts", headers=learner,
                         json={"answers": _perfect_answers(evaluation.id)})

    assert (failed.json()["next_course"], failed.json()["path_completed"]) == (None, None)
    assert passed.json()["course_completed"] and passed.json()["next_course"] == {"course_id": second.id,
                                                                                  "title": "Curso 2"}
