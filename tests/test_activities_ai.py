"""The AI drafts a module's interactive activities from its script, for the admin to review."""

from datetime import datetime, timezone

import pytest

from app.db.models import Activity, Course, Job, Module
from app.services.ai import designer
from tests.conftest import auth_headers

SCENES = [
    {"id": "s1", "slide": {"layout": "cover", "title": "Diagnóstico de llantas"}, "narration": "Hoy aprendes a leer el desgaste.",
     "visual": {"kind": "none", "query": "", "prompt": ""}},
    {"id": "s2", "slide": {"layout": "visual", "title": "Patrones de desgaste"},
     "narration": "Ambos hombros gastados indican baja presión; el centro, exceso de presión.",
     "visual": {"kind": "infographic", "query": "", "prompt": "Infografía: patrones de desgaste y su causa."}},
]


@pytest.fixture
def module(db, admin):
    course = Course(title="Llantas", status="draft", source="ai", settings={}, created_by=admin.id)
    db.add(course)
    db.flush()
    module = Module(course_id=course.id, title="Diagnóstico", order=1, source="ai", storyboard={"scenes": SCENES},
                    content_text="Cómo leer el desgaste de una llanta y qué hacer con la presión de inflado.")
    db.add(module)
    db.commit()
    return module


@pytest.fixture
def headers(client, admin):
    return auth_headers(client, admin.email)


def _run(db, worker, rounds=8):
    for _ in range(rounds):
        db.query(Job).filter(Job.status == "queued").update({Job.run_after: datetime(2000, 1, 1, tzinfo=timezone.utc)},
                                                            synchronize_session=False)
        db.commit()
        worker()
        if not db.query(Job).filter(Job.status == "queued").count():
            break
    db.expire_all()


def test_the_ai_drafts_activities_on_the_module_s_infographic(client, db, module, headers, storage, worker):
    client.put(f"/api/v1/modules/{module.id}/storyboard", headers=headers, json={"scenes": SCENES})  # infographics
    job = client.post(f"/api/v1/modules/{module.id}/activities/generate", headers=headers)
    assert job.status_code == 202
    _run(db, worker)

    drafted = client.get(f"/api/v1/modules/{module.id}/activities", headers=headers).json()
    assert [a["type"] for a in drafted] == ["decision", "inspection", "simulator"]  # 3 at most, one per type
    assert all(a["status"] == "draft" for a in drafted)
    inspection = drafted[1]
    assert inspection["image_url"]  # the infographic the script's scene has
    assert inspection["problems"] == ["Ubica cada zona sobre la imagen y confírmala."]  # the AI only guessed a region
    assert inspection["spec"]["zones"][0] == {**inspection["spec"]["zones"][0], "x": 0.03, "y": 0.03, "placed": False}


def test_regenerating_replaces_the_drafts_but_keeps_what_was_published(client, db, module, headers, storage, worker):
    client.post(f"/api/v1/modules/{module.id}/activities/generate", headers=headers)
    _run(db, worker)
    first = client.get(f"/api/v1/modules/{module.id}/activities", headers=headers).json()
    client.put(f"/api/v1/activities/{first[0]['id']}", headers=headers, json={"status": "ready"})

    client.post(f"/api/v1/modules/{module.id}/activities/generate", headers=headers)
    _run(db, worker)
    again = client.get(f"/api/v1/modules/{module.id}/activities", headers=headers).json()
    assert again[0]["id"] == first[0]["id"] and again[0]["status"] == "ready"
    # the earlier drafts are gone (else there would be 6), three new ones follow the published one
    assert [a["type"] for a in again] == ["decision", "decision", "inspection", "simulator"]
    assert [a["status"] for a in again[1:]] == ["draft"] * 3


def test_a_module_without_content_gets_no_activities(client, db, admin, headers, worker):
    course = Course(title="Vacío", status="draft", source="ai", settings={}, created_by=admin.id)
    db.add(course)
    db.flush()
    empty = Module(course_id=course.id, title="Vacío", order=1, source="ai", content_text="Hola")
    db.add(empty)
    db.commit()
    client.post(f"/api/v1/modules/{empty.id}/activities/generate", headers=headers)
    _run(db, worker)
    job = db.query(Job).filter(Job.type == "ai.activities", Job.module_id == empty.id).one()
    assert job.status == "failed" and "suficiente contenido" in job.error
    assert db.query(Activity).filter(Activity.module_id == empty.id).count() == 0


def test_drafts_that_don_t_hold_up_are_left_out():
    from app.services.ai.fake import FakeLLM

    drafts = FakeLLM()._activities("con infografías")
    broken = drafts[2].model_copy(update={"formula": "precio ** 2"})  # the simulator
    second_decision = drafts[0].model_copy(update={"title": "Otra conversación"})
    kept = designer.to_activities([drafts[0], broken, second_decision, drafts[3]], limit=3)
    assert [a["type"] for a in kept] == ["decision", "quick"]  # broken dropped, one decision only


def test_writing_a_script_drafts_its_activities_too(client, db, admin, headers, storage, worker):
    course = Course(title="Seguridad", status="draft", source="ai", language="es",
                    settings={"outline": {"title": "Seguridad", "description": "", "audience": "", "objectives": [],
                                          "modules": [{"title": "EPP", "summary": "Equipo de protección",
                                                       "objectives": ["Usar EPP"], "key_points": ["Casco"],
                                                       "estimated_minutes": 3, "include_quiz": False}]}},
                    created_by=admin.id)
    db.add(course)
    db.flush()
    module = Module(course_id=course.id, title="EPP", order=1, source="ai",
                    storyboard={"outline": course.settings["outline"]["modules"][0]})
    db.add(module)
    db.commit()
    client.post(f"/api/v1/modules/{module.id}/storyboard/regenerate", headers=headers, json={"feedback": ""})
    _run(db, worker, rounds=12)
    kinds = [a.type for a in db.query(Activity).filter(Activity.module_id == module.id).order_by(Activity.order)]
    assert kinds == ["decision", "inspection", "simulator"]



def test_a_draft_the_admin_wrote_or_edited_is_never_replaced(client, db, module, headers, storage, worker):
    from tests.test_activity_spec import QUICK_ORDER

    mine = client.post(f"/api/v1/modules/{module.id}/activities", headers=headers,
                       json={"type": "quick", "title": "Mi reto", "spec": QUICK_ORDER}).json()
    client.post(f"/api/v1/modules/{module.id}/activities/generate", headers=headers)
    _run(db, worker)
    proposed = client.get(f"/api/v1/modules/{module.id}/activities", headers=headers).json()
    edited = proposed[1]
    client.put(f"/api/v1/activities/{edited['id']}", headers=headers, json={"title": "Ajustada por mí"})

    client.post(f"/api/v1/modules/{module.id}/activities/generate", headers=headers)
    _run(db, worker)
    again = client.get(f"/api/v1/modules/{module.id}/activities", headers=headers).json()
    titles = [a["title"] for a in again]
    assert titles[:2] == ["Mi reto", "Ajustada por mí"] and len(again) == 2 + 3
