"""The platform's parameters: defaults, partial changes, validation in the admin's words, evaluation defaults, and what
learners read of them."""

import logging

import pytest

from app.db.models import AppSettings, Course, Evaluation, Module
from app.schemas.settings import PlatformSettings
from app.services import settings as platform
from tests.conftest import auth_headers
from tests.test_learning_api import QUESTIONS
from tests.test_studio import _approved_course

API = "/api/v1"
DEFAULTS = {
    "general": {"company_name": "", "timezone": "America/Mexico_City"},
    "evaluations": {"passing_score": 70, "max_attempts": 3},
    "certificates": {"enabled": True, "validity_months": None, "min_score": None, "signer_name": "",
                     "signer_title": ""},
    "ranking": {"visible_to_learners": True, "show_full_names": True, "points_module": 20, "points_course": 100,
                "points_path": 200},
}


@pytest.fixture
def admin_headers(client, admin):
    return auth_headers(client, admin.email)


def _patch(client, headers, changes: dict):
    return client.patch(f"{API}/settings", headers=headers, json=changes)


def test_every_parameter_has_a_default(client, admin_headers):
    assert client.get(f"{API}/settings", headers=admin_headers).json() == DEFAULTS


def test_changes_merge_section_by_section_and_unknown_keys_are_dropped(client, db, admin, admin_headers, caplog):
    with caplog.at_level(logging.INFO, logger="app.api.routes.settings"):
        first = _patch(client, admin_headers, {"certificates": {"signer_name": "  Laura Gómez ", "validity_months": 24}})
    assert first.status_code == 200, first.text
    (logged,) = [record for record in caplog.records if record.getMessage() == "platform_settings_updated"]
    assert (logged.admin_id, logged.sections) == (admin.id, ["certificates"])  # who changed what reaches everyone
    second = _patch(client, admin_headers, {"general": {"company_name": "Acme", "theme": "x"}, "badges": {"a": 1},
                                            "ranking": {"a": 1}})

    expected = {
        **DEFAULTS,
        "general": {**DEFAULTS["general"], "company_name": "Acme"},
        "certificates": {**DEFAULTS["certificates"], "signer_name": "Laura Gómez", "validity_months": 24},
    }
    assert second.json() == expected
    assert client.get(f"{API}/settings", headers=admin_headers).json() == expected
    row = db.get(AppSettings, platform.ROW_ID)
    db.refresh(row)
    assert row.values == expected and row.updated_by == admin.id  # stored whole, without unknown keys

    cleared = _patch(client, admin_headers, {"certificates": {"validity_months": None, "signer_name": None}})
    assert cleared.status_code == 200, cleared.text  # null empties a name too
    assert cleared.json()["certificates"] == {**expected["certificates"], "validity_months": None, "signer_name": ""}


@pytest.mark.parametrize(("changes", "message"), [
    ({"general": {"timezone": "Mars/Olympus"}}, "La zona horaria no existe"),
    ({"general": {"timezone": "america/bogota"}}, "La zona horaria no existe"),  # spelled exactly, on any disk
    ({"evaluations": {"passing_score": 0}}, "La nota mínima por defecto debe ser un número entero entre 1 y 100."),
    ({"evaluations": {"max_attempts": 11}}, "Los intentos por defecto deben ser un número entero entre 1 y 10."),
    ({"certificates": {"validity_months": 0}}, "La vigencia debe ser un número entero de meses entre 1 y 120"),
    ({"certificates": {"min_score": 101}}, "La nota mínima promedio debe ser un número entero entre 0 y 100"),
    ({"certificates": {"min_score": "alta"}}, "La nota mínima promedio debe ser un número entero entre 0 y 100"),
    ({"certificates": {"signer_name": "x" * 201}}, "El nombre del firmante debe ser un texto de hasta 200"),
    ({"certificates": {"signer_name": 5}}, "El nombre del firmante debe ser un texto de hasta 200"),
    ({"general": {"company_name": ["Acme"]}}, "El nombre de la empresa debe ser un texto de hasta 200"),
    ({"certificates": "sí"}, "Revisa los parámetros: hay un valor que no es válido."),
    ({"ranking": {"visible_to_learners": "quizás"}}, "Indica si los colaboradores ven el ranking (sí o no)."),
    ({"ranking": {"show_full_names": None}}, "Indica si el ranking muestra los nombres completos (sí o no)."),
    ({"ranking": {"points_module": -1}}, "Los puntos por módulo completado deben ser un número entero entre 0 y"),
    ({"ranking": {"points_course": 1001}}, "Los puntos por curso completado deben ser un número entero entre 0 y"),
    ({"ranking": {"points_path": 2.5}}, "Los puntos por ruta completada deben ser un número entero entre 0 y 1000."),
])
def test_wrong_values_are_rejected_in_the_admins_words(client, admin_headers, changes, message):
    _patch(client, admin_headers, {"general": {"company_name": "Acme"}})

    response = _patch(client, admin_headers, changes)

    assert response.status_code == 422
    assert isinstance(response.json()["detail"], str) and response.json()["detail"].startswith(message)
    assert client.get(f"{API}/settings", headers=admin_headers).json()["general"]["company_name"] == "Acme"  # kept


def test_the_body_must_be_an_object(client, admin_headers):
    assert client.patch(f"{API}/settings", headers=admin_headers, json=["general"]).status_code == 422


def test_a_valid_zone_of_another_region(client, admin_headers):
    response = _patch(client, admin_headers, {"general": {"timezone": "America/Bogota"}})
    assert response.status_code == 200 and response.json()["general"]["timezone"] == "America/Bogota"


def test_values_edited_by_hand_into_something_invalid_fall_back_section_by_section():
    stored = {"general": {"company_name": "Acme", "timezone": "Nowhere/Land"}, "evaluations": {"passing_score": 85}}

    parsed = platform.parse(stored)

    assert parsed.general == PlatformSettings().general  # the broken section: its defaults
    assert parsed.evaluations.passing_score == 85  # the others as stored
    assert platform.parse("not a dict") == PlatformSettings()


def test_collaborators_cannot_read_or_change_the_parameters(client, collaborator):
    headers = auth_headers(client, collaborator.email)
    assert client.get(f"{API}/settings", headers=headers).status_code == 403
    assert _patch(client, headers, {"general": {"company_name": "X"}}).status_code == 403


def test_learners_read_only_what_they_need(client, admin_headers, collaborator):
    headers = auth_headers(client, collaborator.email)
    assert client.get(f"{API}/learn/settings", headers=headers).json() == {"ranking_visible": True, "company_name": ""}

    _patch(client, admin_headers, {"general": {"company_name": "Acme"}, "ranking": {"visible_to_learners": False}})

    assert client.get(f"{API}/learn/settings", headers=headers).json() == {"ranking_visible": False,
                                                                           "company_name": "Acme"}


# ── Evaluation defaults ───────────────────────────────────────────────


def _module(db, admin) -> Module:
    course = Course(title="Curso", status="draft", source="manual", settings={}, created_by=admin.id)
    db.add(course)
    db.flush()
    module = Module(course_id=course.id, title="Módulo", order=1, content_text="Lectura", source="text")
    db.add(module)
    db.commit()
    return module


def test_a_new_evaluation_starts_with_the_platforms_defaults(client, db, admin, admin_headers):
    _patch(client, admin_headers, {"evaluations": {"passing_score": 85, "max_attempts": 5}})
    module, other = _module(db, admin), _module(db, admin)
    url = f"{API}/modules/{module.id}/evaluation"

    created = client.put(url, headers=admin_headers, json={"questions": QUESTIONS})
    assert created.status_code == 200, created.text
    assert (created.json()["passing_score"], created.json()["max_attempts"]) == (85, 5)

    # Left out later, an evaluation keeps its own; sent, they change.
    _patch(client, admin_headers, {"evaluations": {"passing_score": 60}})
    kept = client.put(url, headers=admin_headers, json={"questions": QUESTIONS, "max_attempts": None}).json()
    assert (kept["passing_score"], kept["max_attempts"]) == (85, 5)
    changed = client.put(url, headers=admin_headers, json={"questions": QUESTIONS, "passing_score": 90}).json()
    assert (changed["passing_score"], changed["max_attempts"]) == (90, 5)
    explicit = client.put(f"{API}/modules/{other.id}/evaluation", headers=admin_headers,
                          json={"questions": QUESTIONS, "max_attempts": 2}).json()
    assert (explicit["passing_score"], explicit["max_attempts"]) == (60, 2)


def test_the_ai_drafts_its_quizzes_with_the_platforms_defaults(client, db, admin, admin_headers, worker):
    _patch(client, admin_headers, {"evaluations": {"passing_score": 80, "max_attempts": 4}})
    course = Course(title="Borrador IA", status="draft", source="ai", settings={}, created_by=admin.id)
    db.add(course)
    db.commit()
    _approved_course(client, admin_headers, course.id, worker, modules=2)
    client.post(f"{API}/courses/{course.id}/draft", headers=admin_headers, json={})

    worker()

    db.expire_all()
    (evaluation,) = db.query(Evaluation).all()  # the outline gives the second module a quiz
    assert (evaluation.passing_score, evaluation.max_attempts) == (80, 4)
