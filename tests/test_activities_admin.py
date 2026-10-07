"""Admins write, order, publish and try the interactive activities."""

import copy

import pytest

from app.db.models import Activity, Course, MediaAsset, Module
from tests.conftest import auth_headers
from tests.test_activity_spec import DECISION, INSPECTION, QUICK_ORDER, SIMULATOR


@pytest.fixture
def module(db, admin):
    course = Course(title="Llantas", status="draft", source="ai", settings={}, created_by=admin.id)
    db.add(course)
    db.flush()
    module = Module(course_id=course.id, title="Diagnóstico", order=1, source="ai")
    db.add(module)
    db.commit()
    return module


@pytest.fixture
def headers(client, admin):
    return auth_headers(client, admin.email)


def _create(client, headers, module, kind, spec, **extra):
    return client.post(f"/api/v1/modules/{module.id}/activities", headers=headers,
                       json={"type": kind, "title": f"Actividad {kind}", "spec": copy.deepcopy(spec), **extra})


def test_only_admins_reach_the_activities(client, module, collaborator):
    learner = auth_headers(client, collaborator.email)
    assert client.get(f"/api/v1/modules/{module.id}/activities", headers=learner).status_code == 403


def test_an_activity_is_validated_saved_and_listed_in_order(client, module, headers):
    first = _create(client, headers, module, "decision", DECISION, intro="Escucha al cliente.")
    assert first.status_code == 201 and first.json()["max_score"] == 10 and first.json()["problems"] == []
    second = _create(client, headers, module, "simulator", SIMULATOR, status="ready")
    assert second.status_code == 201 and second.json()["status"] == "ready"
    broken = _create(client, headers, module, "simulator", {**SIMULATOR, "formula": "precio ** 2"})
    assert broken.status_code == 422 and "no se entiende" in broken.json()["detail"]

    listed = client.get(f"/api/v1/modules/{module.id}/activities", headers=headers).json()
    assert [a["type"] for a in listed] == ["decision", "simulator"]
    reordered = client.put(f"/api/v1/modules/{module.id}/activities/order", headers=headers,
                           json={"ids": [second.json()["id"], first.json()["id"]]})
    assert [a["type"] for a in reordered.json()] == ["simulator", "decision"]
    partial = client.put(f"/api/v1/modules/{module.id}/activities/order", headers=headers,
                         json={"ids": [first.json()["id"]]})
    assert partial.status_code == 422


def test_editing_what_is_played_makes_a_new_version_but_publishing_does_not(client, module, headers):
    activity = _create(client, headers, module, "quick", QUICK_ORDER).json()
    published = client.put(f"/api/v1/activities/{activity['id']}", headers=headers, json={"status": "ready"}).json()
    assert published["status"] == "ready" and published["version"] == 1
    renamed = client.put(f"/api/v1/activities/{activity['id']}", headers=headers, json={"title": "Revisión"}).json()
    assert renamed["version"] == 2
    same = client.put(f"/api/v1/activities/{activity['id']}", headers=headers,
                      json={"spec": copy.deepcopy(QUICK_ORDER)}).json()
    assert same["version"] == 2  # nothing changed


def test_an_inspection_is_published_only_on_its_picture_with_every_zone_placed(client, db, module, headers):
    unplaced = copy.deepcopy(INSPECTION)
    unplaced["zones"][0]["placed"] = False
    activity = _create(client, headers, module, "inspection", unplaced).json()
    assert set(activity["problems"]) == {"Falta la imagen sobre la que se inspecciona.",
                                         "Ubica cada zona sobre la imagen y confírmala."}
    refused = client.put(f"/api/v1/activities/{activity['id']}", headers=headers, json={"status": "ready"})
    assert refused.status_code == 409 and "No se puede publicar" in refused.json()["detail"]

    picture = MediaAsset(kind="image", status="ready", bucket="b", path="courses/1/visuals/x.png", mime_type="image/png")
    db.add(picture)
    db.commit()
    with_picture = client.put(f"/api/v1/activities/{activity['id']}/image", headers=headers,
                              json={"asset_id": picture.id}).json()
    assert with_picture["problems"] == ["Ubica cada zona sobre la imagen y confírmala."]
    placed = client.put(f"/api/v1/activities/{activity['id']}", headers=headers,
                        json={"spec": copy.deepcopy(INSPECTION), "status": "ready"}).json()
    assert placed["status"] == "ready" and placed["problems"] == []

    other = MediaAsset(kind="image", status="ready", bucket="b", path="courses/1/visuals/y.png", mime_type="image/png")
    db.add(other)
    db.commit()
    moved = client.put(f"/api/v1/activities/{activity['id']}/image", headers=headers, json={"asset_id": other.id})
    assert moved.status_code == 409  # a published inspection can't lose its zones' places
    assert db.get(Activity, activity["id"]).image_asset_id == picture.id


def test_an_admin_tries_an_activity_without_counting_for_anyone(client, db, module, headers):
    activity = _create(client, headers, module, "decision", DECISION).json()  # a draft can be tried too
    play = client.post(f"/api/v1/activities/{activity['id']}/preview", headers=headers).json()
    choice = next(c["token"] for c in play["view"]["node"]["choices"] if c["text"].startswith("Le doy"))
    step = client.post(f"/api/v1/activity-previews/{play['attempt_id']}/moves", headers=headers,
                       json={"move": {"choice": choice}}).json()
    assert step["done"] and step["result"]["points"] == 2


def test_a_module_holds_at_most_six_activities_and_deleting_one_frees_a_place(client, module, headers):
    ids = [_create(client, headers, module, "quick", QUICK_ORDER).json()["id"] for _ in range(6)]
    assert _create(client, headers, module, "quick", QUICK_ORDER).status_code == 409
    assert client.delete(f"/api/v1/activities/{ids[0]}", headers=headers).status_code == 204
    assert _create(client, headers, module, "quick", QUICK_ORDER).status_code == 201


def test_an_activity_s_picture_is_never_discarded_while_in_use(client, db, module, headers):
    from app.services.media import discard_assets

    picture = MediaAsset(kind="image", status="ready", bucket="b", path="courses/1/visuals/z.png", mime_type="image/png")
    db.add(picture)
    db.commit()
    activity = _create(client, headers, module, "inspection", INSPECTION).json()
    client.put(f"/api/v1/activities/{activity['id']}/image", headers=headers, json={"asset_id": picture.id})
    discard_assets(db, [picture.id])
    assert db.get(MediaAsset, picture.id) is not None
