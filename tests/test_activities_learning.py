"""Interactive activities for learners: playing them through the API and how they complete a module."""

import copy
from datetime import datetime, timedelta, timezone

import pytest

from app.db.models import Activity, ActivityAttempt, Course, Enrollment, Evaluation, MediaAsset, Module
from app.services import activities as engine
from app.services.activities import text_hash
from tests.conftest import QueryCounter, auth_headers
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


def _start(client, headers, activity_id, **body) -> dict:
    response = client.post(f"{API}/activities/{activity_id}/attempts", headers=headers, json=body)
    assert response.status_code == 200, response.text
    return response.json()


def _choose(client, headers, play: dict, starts_with: str, node: dict | None = None):
    """The decision choice whose text starts so, on `node` (by default, the situation the play started on)."""
    choices = (node or play["view"]["node"])["choices"]
    token = next(c["token"] for c in choices if c["text"].startswith(starts_with))
    return client.post(f"{API}/activity-attempts/{play['attempt_id']}/moves", headers=headers,
                       json={"move": {"choice": token}})


def _summary(client, headers, course, activity_id) -> dict:
    """The activity as the learner's course player lists it."""
    modules = client.get(f"{API}/courses/{course.id}", headers=headers).json()["modules"]
    return next(a for module in modules for a in module["activities"] if a["id"] == activity_id)


def _plays(db, activity_id) -> list[ActivityAttempt]:
    db.expire_all()  # written by the API's own sessions
    plays = db.query(ActivityAttempt).filter(ActivityAttempt.activity_id == activity_id)
    return plays.order_by(ActivityAttempt.id).all()


def _published_quick(db, course) -> Activity:
    """The first module's quick challenge (the 4 steps of QUICK_ORDER, 40 s), published."""
    quick = db.query(Activity).filter(Activity.module_id == _modules(course)[0].id, Activity.type == "quick").one()
    quick.status = "ready"
    db.commit()
    return quick


def _run_out_the_clock(db, attempt_id, seconds_ago: int = 60):
    """The learner closed the game and came back once its time was up (a minute ago, by default)."""
    attempt = db.get(ActivityAttempt, attempt_id)
    db.refresh(attempt)
    deadline = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    attempt.state = {**attempt.state, "deadline": deadline.isoformat()}
    db.commit()


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
    first = _start(client, learner, activity.id)
    assert not first["resumed"] and _summary(client, learner, course, activity.id)["resumable"]
    again = _start(client, learner, activity.id)
    assert again["attempt_id"] == first["attempt_id"] and again["resumed"]

    activity.version += 1
    db.commit()
    assert not _summary(client, learner, course, activity.id)["resumable"]
    stale = client.post(f"{API}/activity-attempts/{first['attempt_id']}/moves", headers=learner,
                        json={"move": {"choice": first["view"]["node"]["choices"][0]["token"]}})
    assert stale.status_code == 409 and "vuelve a empezar" in stale.json()["detail"]
    fresh = _start(client, learner, activity.id)
    assert not fresh["resumed"] and fresh["view"]["step"] == 1
    # Untouched, the play of the older version is gone: nothing of it counts.
    assert [(play.version, play.finished_at) for play in _plays(db, activity.id)] == [(activity.version, None)]


def test_a_half_played_game_on_an_older_version_never_counts(client, db, course, learner):
    activity = _activity(db, _modules(course)[0], "decision")
    first = _start(client, learner, activity.id)
    assert _choose(client, learner, first, "¿Qué ruta").json()["result"]["points"] == 6
    activity.version += 1  # an admin fixed a typo mid-game
    db.commit()

    fresh = _start(client, learner, activity.id)
    assert not fresh["resumed"] and fresh["attempt_id"] != first["attempt_id"] and fresh["view"]["step"] == 1
    # The half-played game stays as it was: an edit never turns it into a finished play.
    old, new = _plays(db, activity.id)
    assert (old.id, old.finished_at, old.score) == (first["attempt_id"], None, 6) and new.finished_at is None
    modules = client.get(f"{API}/courses/{course.id}", headers=learner).json()["modules"]
    assert modules[0]["activities"][0] == {**modules[0]["activities"][0], "finished": False, "best_score": None,
                                           "resumable": True}  # the new game
    assert not modules[0]["completed"] and not modules[1]["unlocked"]
    again = _start(client, learner, activity.id)
    assert again["resumed"] and again["attempt_id"] == fresh["attempt_id"]
    assert _plays(db, activity.id)[0].finished_at is None  # still not counted


def test_the_last_move_records_the_play_and_tells_what_it_completed(client, db, course, learner):
    plain, quizzed, _ = _modules(course)
    activity = _activity(db, plain, "decision")
    play = _start(client, learner, activity.id)
    last = _choose(client, learner, play, "Le doy").json()  # it ends the conversation
    assert last["done"] and last["result"]["score"] == 2
    assert (last["module_completed"], last["next_module_id"], last["course_completed"]) == (True, quizzed.id, False)

    # The learner closed the game instead of "Ver resultado": the play counts all the same.
    (recorded,) = _plays(db, activity.id)
    finished_at = recorded.finished_at
    assert finished_at is not None and recorded.score == 2
    player = client.get(f"{API}/courses/{course.id}", headers=learner).json()
    modules = player["modules"]
    assert modules[0]["activities"][0] == {**modules[0]["activities"][0], "finished": True, "best_score": 2,
                                           "resumable": False}
    assert modules[0]["completed"] and modules[1]["unlocked"]  # it was all the module waited for
    assert player["enrollment"]["progress_pct"] == 33.3  # and the course's progress follows

    # "Ver resultado" afterwards, or a retry of it: the result as it was recorded, never an error.
    for _ in range(2):
        finished = client.post(f"{API}/activity-attempts/{play['attempt_id']}/finish", headers=learner)
        assert finished.status_code == 200
        assert finished.json() == {"score": 2, "max_score": 10, "module_completed": True,
                                   "next_module_id": quizzed.id, "course_completed": False,
                                   "next_course": None, "path_completed": None}
    assert _plays(db, activity.id)[0].finished_at == finished_at  # finished once
    fresh = _start(client, learner, activity.id)
    assert not fresh["resumed"] and fresh["view"]["step"] == 1 and fresh["view"]["score"] == 0


def test_a_move_after_the_clock_ran_out_records_the_play_with_its_score(client, db, course, learner):
    quick = _published_quick(db, course)
    play = _start(client, learner, quick.id)
    token = {card["text"]: card["token"] for card in play["view"]["cards"]}
    steps = [token[step] for step in QUICK_ORDER["items"]]
    moves = f"{API}/activity-attempts/{play['attempt_id']}/moves"
    client.post(moves, headers=learner, json={"move": {"card": steps[0]}})
    _run_out_the_clock(db, play["attempt_id"])

    late = client.post(moves, headers=learner, json={"move": {"card": steps[1]}}).json()
    assert late["done"] and late["result"]["timeout"] and late["result"]["score"] == 10
    # Recorded, with what it completed: nothing yet, the module still waits for its conversation.
    assert (late["module_completed"], late["next_module_id"], late["course_completed"]) == (False, None, False)
    (recorded,) = _plays(db, quick.id)
    assert recorded.finished_at is not None and recorded.score == 10
    summary = _summary(client, learner, course, quick.id)
    assert summary == {**summary, "finished": True, "best_score": 10, "resumable": False}


@pytest.mark.parametrize("late", [1, 60])  # seconds since the time was up: within a late move's grace, or well after
def test_a_timed_game_whose_time_ran_out_is_kept_with_its_score_and_a_new_one_starts(client, db, course, learner, late):
    quick = _published_quick(db, course)
    first = _start(client, learner, quick.id)
    token = {card["text"]: card["token"] for card in first["view"]["cards"]}
    right = client.post(f"{API}/activity-attempts/{first['attempt_id']}/moves", headers=learner,
                        json={"move": {"card": token[QUICK_ORDER["items"][0]]}})
    assert right.json()["result"]["score"] == 10
    _run_out_the_clock(db, first["attempt_id"], seconds_ago=late)
    assert not _summary(client, learner, course, quick.id)["resumable"]

    fresh = _start(client, learner, quick.id)
    assert not fresh["resumed"] and fresh["attempt_id"] != first["attempt_id"]
    # A game to play, not one already lost: the whole clock and no step placed.
    assert fresh["view"]["remaining"] > 35 and fresh["view"]["solved"] == 0 and not fresh["view"]["done"]
    old, new = _plays(db, quick.id)
    assert (old.id, old.score, old.state["done"]) == (first["attempt_id"], 10, True) and old.finished_at
    assert (new.id, new.finished_at) == (fresh["attempt_id"], None)
    summary = _summary(client, learner, course, quick.id)
    assert summary == {**summary, "finished": True, "best_score": 10, "resumable": True}


def test_an_untouched_game_whose_time_ran_out_is_dropped_and_the_new_one_can_be_untimed(client, db, course, learner):
    quick = _published_quick(db, course)
    first = _start(client, learner, quick.id)
    _run_out_the_clock(db, first["attempt_id"])

    fresh = _start(client, learner, quick.id, untimed=True)
    assert not fresh["resumed"]
    assert fresh["view"]["remaining"] is None and fresh["view"]["max_score"] == 4 * 20  # no clock, no time bonus
    # The untouched play left no trace (a game never played doesn't count); only the new one is open.
    assert [(play.id, play.state["deadline"], play.finished_at) for play in _plays(db, quick.id)] == [
        (fresh["attempt_id"], None, None)]
    assert _summary(client, learner, course, quick.id)["finished"] is False


def test_a_timed_game_still_on_the_clock_is_resumed_as_it_was(client, db, course, learner):
    quick = _published_quick(db, course)
    first = _start(client, learner, quick.id)
    again = _start(client, learner, quick.id, untimed=True)  # the choice is for a new game: this one goes on
    assert again["resumed"] and again["attempt_id"] == first["attempt_id"]
    assert 0 < again["view"]["remaining"] <= first["view"]["remaining"]


def test_a_move_is_authorized_by_the_play_alone(client, db, engine, course, learner):
    """
    A move checks only the play: it is the learner's own and still open, on the current version of an activity and a
    course still published. That's 4 statements (the user, the locked play, its activity joined to its course, the
    update), whatever the course's size. It used to recompute the learner's whole course on every tap (the module, the
    enrollment, the course, its modules, the progress, the evaluations, the activities and the best scores):
    12 statements, with a timed game running. Only the move that ends the game reads the course, to record the play.
    """
    activity = _activity(db, _modules(course)[0], "decision")
    play = _start(client, learner, activity.id)
    with QueryCounter(engine) as counter:
        step = _choose(client, learner, play, "¿Qué ruta")
    assert step.status_code == 200 and counter.count <= 4, counter.count
    assert step.json()["result"]["best_points"] == 6 and step.json()["module_completed"] is None  # not over yet
    route = step.json()["result"]["next"]  # the situation it led to

    activity.status = "draft"  # unpublished mid-game
    db.commit()
    assert _choose(client, learner, play, "Le muestro", node=route).status_code == 404
    activity.status, course.status = "ready", "draft"  # now the whole course
    db.commit()
    assert _choose(client, learner, play, "Le muestro", node=route).status_code == 404
    course.status = "published"
    db.commit()
    last = _choose(client, learner, play, "Le muestro", node=route).json()
    assert last["done"] and last["module_completed"]
    over = _choose(client, learner, play, "Le hablo", node=route)
    assert over.status_code == 409 and "ya terminó" in over.json()["detail"]


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


def test_an_activity_starts_with_its_reference_photo(client, db, course, learner, storage, tmp_path):
    from tests.test_activity_spec import INSPECTION

    plain = _modules(course)[0]
    picture = tmp_path / "pic.png"
    picture.write_bytes(b"\x89PNG")
    storage.upload_file("courses/1/visuals/pic.png", picture, "image/png")
    asset = MediaAsset(kind="image", status="ready", bucket=storage.bucket, path="courses/1/visuals/pic.png",
                       mime_type="image/png")
    db.add(asset)
    db.flush()
    inspection = Activity(module_id=plain.id, order=3, type="inspection", title="Revisión", intro="",
                          spec=copy.deepcopy(INSPECTION), status="ready", image_asset_id=asset.id)
    db.add(inspection)
    db.commit()
    play = client.post(f"{API}/activities/{inspection.id}/attempts", headers=learner, json={}).json()
    assert play["image_url"] and play["view"]["lives"] == 3



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
    quick = _published_quick(db, course)
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
