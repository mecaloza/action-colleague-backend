"""Interactive activities for learners: playing them through the API and how they complete a module."""

import copy

import pytest

from app.db.models import Activity, ActivityAttempt, Course, Enrollment, Evaluation, MediaAsset, Module
from app.services import activities as engine
from app.services.activities import text_hash
from tests.conftest import auth_headers
from tests.test_activity_spec import DECISION, QUICK_ORDER
from tests.test_learning_api import QUESTIONS, _perfect_answers

API = "/api/v1/learn"


@pytest.fixture
def course(db, admin):
    course = Course(title="Llantas", status="published", source="manual", settings={}, created_by=admin.id)
    db.add(course)
    db.flush()
    plain = Module(course_id=course.id, title="Sin evaluación", order=1, content_text="Lectura", source="text")
    quizzed = Module(course_id=course.id, title="Con evaluación", order=2, content_text="Lectura", source="text")
    last = Module(course_id=course.id, title="Cierre", order=3, content_text="Fin", source="text")
    db.add_all([plain, quizzed, last])
    db.flush()
    db.add(Evaluation(module_id=quizzed.id, spec=QUESTIONS, max_attempts=3, passing_score=80))
    db.add_all([
        Activity(module_id=plain.id, order=1, type="decision", title="Cliente que solo pide precio",
                 intro="Escucha al cliente.", spec=copy.deepcopy(DECISION), status="ready"),
        Activity(module_id=plain.id, order=2, type="quick", title="Revisión en patio", intro="",
                 spec=copy.deepcopy(QUICK_ORDER), status="draft"),  # not published: not required
        Activity(module_id=quizzed.id, order=1, type="quick", title="Ordena la revisión", intro="",
                 spec=copy.deepcopy(QUICK_ORDER), status="ready"),
    ])
    db.commit()
    return course


@pytest.fixture
def learner(client, db, course, collaborator):
    db.add(Enrollment(user_id=collaborator.id, course_id=course.id, status="assigned", progress_pct=0))
    db.commit()
    return auth_headers(client, collaborator.email)


def _modules(course):
    return sorted(course.modules, key=lambda m: m.order)


def _activity(db, module, kind):
    return db.query(Activity).filter(Activity.module_id == module.id, Activity.type == kind,
                                     Activity.status == "ready").one()


def _play_decision(client, headers, activity_id) -> dict:
    """The best path of the DECISION conversation; returns the finish response."""
    play = client.post(f"{API}/activities/{activity_id}/attempts", headers=headers, json={}).json()
    choice = next(c["token"] for c in play["view"]["node"]["choices"] if c["text"].startswith("¿Qué ruta"))
    step = client.post(f"{API}/activity-attempts/{play['attempt_id']}/moves", headers=headers,
                       json={"move": {"choice": choice}}).json()
    cpk = next(c["token"] for c in step["result"]["next"]["choices"] if "costo por kilómetro" in c["text"])
    client.post(f"{API}/activity-attempts/{play['attempt_id']}/moves", headers=headers, json={"move": {"choice": cpk}})
    return client.post(f"{API}/activity-attempts/{play['attempt_id']}/finish", headers=headers).json()


def _play_order(client, headers, activity_id) -> dict:
    play = client.post(f"{API}/activities/{activity_id}/attempts", headers=headers, json={"untimed": True}).json()
    token = {card["text"]: card["token"] for card in play["view"]["cards"]}
    for step in QUICK_ORDER["items"]:
        client.post(f"{API}/activity-attempts/{play['attempt_id']}/moves", headers=headers,
                    json={"move": {"card": token[step]}})
    return client.post(f"{API}/activity-attempts/{play['attempt_id']}/finish", headers=headers).json()


def test_the_player_lists_only_published_activities_of_unlocked_modules(client, course, learner):
    modules = client.get(f"{API}/courses/{course.id}", headers=learner).json()["modules"]
    assert [a["title"] for a in modules[0]["activities"]] == ["Cliente que solo pide precio"]
    assert modules[0]["activities"][0] == {**modules[0]["activities"][0], "finished": False, "best_score": None,
                                           "max_score": 10}
    assert modules[1]["activities"] == []  # locked: nothing of it leaves the server


def test_a_module_with_activities_completes_when_they_are_played(client, db, course, learner):
    plain = _modules(course)[0]
    blocked = client.post(f"{API}/modules/{plain.id}/complete", headers=learner)
    assert blocked.status_code == 409 and "actividades" in blocked.json()["detail"]

    finished = _play_decision(client, learner, _activity(db, plain, "decision").id)
    assert finished["score"] == 10 and finished["module_completed"] and finished["next_module_id"]
    modules = client.get(f"{API}/courses/{course.id}", headers=learner).json()["modules"]
    assert modules[0]["completed"] and modules[1]["unlocked"]
    assert modules[0]["activities"][0]["best_score"] == 10


def test_a_quizzed_module_needs_the_quiz_passed_and_its_activities_played(client, db, course, learner):
    plain, quizzed, _ = _modules(course)
    _play_decision(client, learner, _activity(db, plain, "decision").id)

    passed = client.post(f"{API}/modules/{quizzed.id}/quiz/attempts", headers=learner,
                         json={"answers": _perfect_answers(quizzed.evaluation.id)}).json()
    assert passed["passed"] and not passed["module_completed"]  # its activity is still pending

    finished = _play_order(client, learner, _activity(db, quizzed, "quick").id)
    assert finished["module_completed"] and not finished["course_completed"]


def test_activities_of_a_locked_module_or_someone_else_s_play_are_out_of_reach(client, db, course, learner, admin):
    _, quizzed, _ = _modules(course)
    locked = client.post(f"{API}/activities/{_activity(db, quizzed, 'quick').id}/attempts", headers=learner, json={})
    assert locked.status_code == 403

    play = client.post(f"{API}/activities/{_activity(db, _modules(course)[0], 'decision').id}/attempts",
                       headers=learner, json={}).json()
    other = auth_headers(client, admin.email)
    stolen = client.post(f"{API}/activity-attempts/{play['attempt_id']}/moves", headers=other,
                         json={"move": {"choice": play["view"]["node"]["choices"][0]["token"]}})
    assert stolen.status_code == 404


def test_a_reload_resumes_the_open_play_and_an_edit_restarts_it(client, db, course, learner):
    activity = _activity(db, _modules(course)[0], "decision")
    first = client.post(f"{API}/activities/{activity.id}/attempts", headers=learner, json={}).json()
    again = client.post(f"{API}/activities/{activity.id}/attempts", headers=learner, json={}).json()
    assert again["attempt_id"] == first["attempt_id"]

    activity.version += 1
    db.commit()
    stale = client.post(f"{API}/activity-attempts/{first['attempt_id']}/moves", headers=learner,
                        json={"move": {"choice": first["view"]["node"]["choices"][0]["token"]}})
    assert stale.status_code == 409 and "vuelve a empezar" in stale.json()["detail"]
    fresh = client.post(f"{API}/activities/{activity.id}/attempts", headers=learner, json={}).json()
    assert fresh["attempt_id"] != first["attempt_id"]


def test_a_play_must_be_over_to_finish_and_can_t_be_played_after(client, db, course, learner):
    activity = _activity(db, _modules(course)[0], "decision")
    play = client.post(f"{API}/activities/{activity.id}/attempts", headers=learner, json={}).json()
    early = client.post(f"{API}/activity-attempts/{play['attempt_id']}/finish", headers=learner)
    assert early.status_code == 409
    bad = client.post(f"{API}/activity-attempts/{play['attempt_id']}/moves", headers=learner,
                      json={"move": {"choice": "inventado"}})
    assert bad.status_code == 422


def test_the_voice_of_each_revealed_line_comes_with_it(client, db, course, learner, storage, tmp_path):
    activity = _activity(db, _modules(course)[0], "decision")
    clips = {}
    clip = tmp_path / "clip.mp3"
    clip.write_bytes(b"ID3")
    for key in ("intro", "inicio.situation", "inicio.pregunta", "ruta.cpk"):
        storage.upload_file(f"activity-audio/{key}.mp3", clip, "audio/mpeg")
        asset = MediaAsset(kind="audio", status="ready", bucket=storage.bucket, path=f"activity-audio/{key}.mp3",
                           mime_type="audio/mpeg")
        db.add(asset)
        db.flush()
        clips[key] = {"asset_id": asset.id, "text_hash": text_hash(engine.line_keys(activity)[key])}
    activity.narration = clips
    db.commit()

    play = client.post(f"{API}/activities/{activity.id}/attempts", headers=learner, json={}).json()
    assert set(play["narration"]) == {"intro", "inicio.situation"}  # not the consequences ahead
    choice = next(c["token"] for c in play["view"]["node"]["choices"] if c["text"].startswith("¿Qué ruta"))
    step = client.post(f"{API}/activity-attempts/{play['attempt_id']}/moves", headers=learner,
                       json={"move": {"choice": choice}}).json()
    assert set(step["narration"]) == {"inicio.pregunta"}  # "ruta.situation" has no clip in this test


def test_finished_plays_count_once_even_if_played_again(client, db, course, learner):
    activity = _activity(db, _modules(course)[0], "decision")
    _play_decision(client, learner, activity.id)
    second = _play_decision(client, learner, activity.id)
    assert second["score"] == 10
    assert db.query(ActivityAttempt).filter(ActivityAttempt.activity_id == activity.id,
                                            ActivityAttempt.finished_at.isnot(None)).count() == 2


def test_an_inspection_starts_with_its_picture(client, db, course, learner, storage, tmp_path):
    from tests.test_activity_spec import INSPECTION

    plain = _modules(course)[0]
    picture = tmp_path / "pic.png"
    picture.write_bytes(b"\x89PNG")
    storage.upload_file("courses/1/visuals/pic.png", picture, "image/png")
    asset = MediaAsset(kind="image", status="ready", bucket=storage.bucket, path="courses/1/visuals/pic.png",
                       mime_type="image/png")
    db.add(asset)
    db.flush()
    inspection = Activity(module_id=plain.id, order=3, type="inspection", title="Desgaste", intro="",
                          spec=copy.deepcopy(INSPECTION), status="ready", image_asset_id=asset.id)
    db.add(inspection)
    db.commit()
    play = client.post(f"{API}/activities/{inspection.id}/attempts", headers=learner, json={}).json()
    assert play["image_url"] and play["view"]["revealed"] == []



def test_a_clip_of_an_older_wording_is_never_played(client, db, course, learner, storage, tmp_path):
    activity = _activity(db, _modules(course)[0], "decision")
    clip = tmp_path / "clip.mp3"
    clip.write_bytes(b"ID3")
    storage.upload_file("activity-audio/intro.mp3", clip, "audio/mpeg")
    asset = MediaAsset(kind="audio", status="ready", bucket=storage.bucket, path="activity-audio/intro.mp3",
                       mime_type="audio/mpeg")
    db.add(asset)
    db.flush()
    activity.narration = {"intro": {"asset_id": asset.id, "text_hash": text_hash("Lo que decía antes.")}}
    db.commit()
    play = client.post(f"{API}/activities/{activity.id}/attempts", headers=learner, json={}).json()
    assert "intro" not in play["narration"]  # the text on screen is newer than the voice: silence, not a mismatch


def test_a_module_left_waiting_for_a_removed_activity_completes(client, db, course, learner, admin):
    plain, quizzed, _ = _modules(course)
    _play_decision(client, learner, _activity(db, plain, "decision").id)
    passed = client.post(f"{API}/modules/{quizzed.id}/quiz/attempts", headers=learner,
                         json={"answers": _perfect_answers(quizzed.evaluation.id)}).json()
    assert passed["passed"] and not passed["module_completed"]

    admin_headers = auth_headers(client, admin.email)
    gone = client.delete(f"/api/v1/activities/{_activity(db, quizzed, 'quick').id}", headers=admin_headers)
    assert gone.status_code == 204
    modules = client.get(f"{API}/courses/{course.id}", headers=learner).json()["modules"]
    assert modules[1]["completed"] and modules[2]["unlocked"]


def test_a_finished_play_survives_an_edit_of_the_activity(client, db, course, learner):
    activity = _activity(db, _modules(course)[0], "decision")
    play = client.post(f"{API}/activities/{activity.id}/attempts", headers=learner, json={}).json()
    price = next(c["token"] for c in play["view"]["node"]["choices"] if c["text"].startswith("Le doy"))
    client.post(f"{API}/activity-attempts/{play['attempt_id']}/moves", headers=learner, json={"move": {"choice": price}})
    activity.title, activity.version = "Otro título", activity.version + 1  # an admin fixed a typo
    db.commit()
    finished = client.post(f"{API}/activity-attempts/{play['attempt_id']}/finish", headers=learner)
    assert finished.status_code == 200 and finished.json()["score"] == 2


def test_a_quick_challenge_without_its_clock_has_no_time_bonus_to_win(client, db, course, learner):
    plain = _modules(course)[0]
    quick = db.query(Activity).filter(Activity.module_id == plain.id, Activity.type == "quick").one()
    quick.status = "ready"
    db.commit()
    timed = client.post(f"{API}/activities/{quick.id}/attempts", headers=learner, json={}).json()
    assert timed["view"]["max_score"] == 4 * 20 + 40 // 2
    db.query(ActivityAttempt).delete()
    db.commit()
    untimed = client.post(f"{API}/activities/{quick.id}/attempts", headers=learner, json={"untimed": True}).json()
    assert untimed["view"]["max_score"] == 4 * 20


def test_an_activity_broken_by_hand_never_breaks_the_course(client, db, course, learner):
    activity = _activity(db, _modules(course)[0], "decision")
    activity.spec = {"nodes": []}
    db.commit()
    response = client.get(f"{API}/courses/{course.id}", headers=learner)
    assert response.status_code == 200 and response.json()["modules"][0]["activities"][0]["max_score"] == 0
