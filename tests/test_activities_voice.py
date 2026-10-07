"""Each line of a published activity is voiced once; unchanged lines are never paid for twice."""

import copy
from datetime import datetime, timezone

import pytest

from app.db.models import Activity, Course, Job, MediaAsset, Module
from app.services.ai.fake import FakeVoice
from tests.conftest import auth_headers
from tests.test_activity_spec import DECISION


@pytest.fixture
def module(db, admin):
    course = Course(title="Llantas", status="draft", source="ai", created_by=admin.id,
                    settings={"voice_id": "narrador", "co_voice_id": "cliente"})
    db.add(course)
    db.flush()
    module = Module(course_id=course.id, title="Ventas", order=1, source="ai")
    db.add(module)
    db.commit()
    return module


@pytest.fixture
def headers(client, admin):
    return auth_headers(client, admin.email)


@pytest.fixture
def spoken(monkeypatch):
    said = []
    original = FakeVoice.speak

    def speak(self, text, voice_id, previous_text="", next_text=""):
        said.append((voice_id, text))
        return original(self, text, voice_id, previous_text, next_text)

    monkeypatch.setattr(FakeVoice, "speak", speak)
    return said


def _run(db, worker):
    for _ in range(4):
        db.query(Job).filter(Job.status == "queued").update({Job.run_after: datetime(2000, 1, 1, tzinfo=timezone.utc)},
                                                            synchronize_session=False)
        db.commit()
        worker()
    db.expire_all()


def test_publishing_voices_every_line_with_the_client_in_the_second_voice(client, db, module, headers, storage,
                                                                         worker, spoken):
    created = client.post(f"/api/v1/modules/{module.id}/activities", headers=headers, json={
        "type": "decision", "title": "Cliente", "intro": "Escucha al cliente.", "spec": copy.deepcopy(DECISION),
        "status": "ready"}).json()
    assert created["voice_status"] == "pending"
    _run(db, worker)

    activity = db.get(Activity, created["id"])
    assert activity.voice_status == "ready" and len(activity.narration) == 1 + 2 + 4
    voices = dict((text, voice) for voice, text in spoken)
    assert voices["Solo quiero la llanta más barata para mi flota."] == "cliente"  # the client's situation
    assert voices["Escucha al cliente."] == "narrador"
    assert all(db.get(MediaAsset, clip["asset_id"]).kind == "audio" for clip in activity.narration.values())


def test_an_edit_voices_only_the_lines_that_changed_and_drops_the_old_clip(client, db, module, headers, storage,
                                                                          worker, spoken):
    created = client.post(f"/api/v1/modules/{module.id}/activities", headers=headers, json={
        "type": "decision", "title": "Cliente", "intro": "Escucha al cliente.", "spec": copy.deepcopy(DECISION),
        "status": "ready"}).json()
    _run(db, worker)
    old_intro = db.get(Activity, created["id"]).narration["intro"]["asset_id"]
    spoken.clear()

    client.put(f"/api/v1/activities/{created['id']}", headers=headers, json={"intro": "Atiende al cliente."})
    _run(db, worker)
    assert spoken == [("narrador", "Atiende al cliente.")]  # nothing else is paid again
    activity = db.get(Activity, created["id"])
    assert activity.narration["intro"]["asset_id"] != old_intro
    assert db.get(MediaAsset, old_intro) is None  # the clip nobody says any more is gone


def test_a_draft_is_not_voiced_and_a_clip_two_activities_share_stays(client, db, module, headers, storage, worker,
                                                                    spoken):
    draft = client.post(f"/api/v1/modules/{module.id}/activities", headers=headers, json={
        "type": "decision", "title": "Borrador", "spec": copy.deepcopy(DECISION)}).json()
    assert draft["voice_status"] == "none"
    first = client.post(f"/api/v1/modules/{module.id}/activities", headers=headers, json={
        "type": "decision", "title": "Uno", "intro": "Escucha al cliente.", "spec": copy.deepcopy(DECISION),
        "status": "ready"}).json()
    second = client.post(f"/api/v1/modules/{module.id}/activities", headers=headers, json={
        "type": "decision", "title": "Dos", "intro": "Escucha al cliente.", "spec": copy.deepcopy(DECISION),
        "status": "ready"}).json()
    _run(db, worker)
    assert len(spoken) == 7  # the second activity says the same lines: no clip is made twice
    shared = db.get(Activity, first["id"]).narration["intro"]["asset_id"]
    assert db.get(Activity, second["id"]).narration["intro"]["asset_id"] == shared

    client.put(f"/api/v1/activities/{first['id']}", headers=headers, json={"intro": "Otra introducción."})
    _run(db, worker)
    assert db.get(MediaAsset, shared) is not None  # the second activity still says it
