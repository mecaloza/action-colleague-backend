"""Points: what plays and quizzes improve on a learner's best, milestones earned once, written by every completion
(one learner or many at once), and the past derived by the maintenance pass."""

import copy
import logging
from datetime import datetime, timezone

import pytest

from app.db.models import (
    Activity,
    ActivityAttempt,
    Enrollment,
    Evaluation,
    EvaluationAttempt,
    Job,
    ModuleProgress,
    PathAssignment,
    PointsEntry,
)
from app.services import points
from app.worker import queue
from tests.conftest import auth_headers
from tests.test_activities_learning import _choose, _run_out_the_clock, _start
from tests.test_activity_spec import DECISION, QUICK_ORDER
from tests.test_certificates import _answer, _enroll, _learner, _settings
from tests.test_learning_api import QUESTIONS
from tests.test_paths import _assign, _complete, _course, _modules, _path

API = "/api/v1"


@pytest.fixture
def admin_headers(client, admin):
    return auth_headers(client, admin.email)


def _ledger(db, user=None) -> list[tuple[str, int]]:
    """(source_key, points), in the order they were written."""
    db.expire_all()
    query = db.query(PointsEntry).order_by(PointsEntry.id)
    if user is not None:
        query = query.filter(PointsEntry.user_id == user.id)
    return [(row.source_key, row.points) for row in query]


def _activity(db, module, spec=DECISION, kind="decision") -> Activity:
    activity = Activity(module_id=module.id, order=1, type=kind, title="Cliente", intro="",
                        spec=copy.deepcopy(spec), status="ready")
    db.add(activity)
    db.commit()
    return activity


def _converse(client, headers, activity_id, *choices) -> dict:
    """A whole conversation of DECISION, choice by choice (by how each starts); the last move."""
    play, node, move = _start(client, headers, activity_id), None, {}
    for choice in choices:
        response = _choose(client, headers, play, choice, node=node)
        assert response.status_code == 200, response.text
        move = response.json()
        node = move["result"].get("next")
    assert move["done"]
    return move


BEST = ("¿Qué ruta", "Le muestro")  # 6 + 4 of 10: 100 %
GOOD = ("¿Qué ruta", "Le hablo")  # 6 + 1: 70 %
POOR = ("Le doy",)  # 2: 20 %


# ── Plays and quizzes: their improvement only ─────────────────────────


def test_a_play_earns_only_what_it_improves_on_the_learners_best(client, db, admin, admin_headers):
    course = _course(db, admin, "Atención al cliente", modules=2)
    first, _ = _modules(db, course)
    activity = _activity(db, first)
    ana = _learner(db)
    _enroll(client, admin_headers, course, ana)
    learner = auth_headers(client, ana.email)
    played = f"activity:{activity.id}"

    _converse(client, learner, activity.id, *POOR)
    assert _ledger(db) == [(played, 20), (f"module:{first.id}", 20)]  # its only activity: the module is completed
    for choices in (POOR, GOOD, BEST, GOOD, BEST):  # replays: only beating the best earns, by the difference
        _converse(client, learner, activity.id, *choices)

    assert _ledger(db) == [(played, 20), (f"module:{first.id}", 20), (played, 50), (played, 30)]
    (row,) = db.query(PointsEntry).filter(PointsEntry.points == 50).all()
    assert (row.user_id, row.course_id, row.path_id, row.source) == (ana.id, course.id, None, "activity")


def test_an_admins_preview_never_earns(client, db, admin, admin_headers):
    course = _course(db, admin, "Curso")
    (module,) = _modules(db, course)
    activity = _activity(db, module)
    _enroll(client, admin_headers, course, admin)  # even enrolled in the course themselves

    play = client.post(f"{API}/activities/{activity.id}/preview", headers=admin_headers).json()
    choice = next(c["token"] for c in play["view"]["node"]["choices"] if c["text"].startswith("Le doy"))
    moved = client.post(f"{API}/activity-previews/{play['attempt_id']}/moves", headers=admin_headers,
                        json={"move": {"choice": choice}})

    assert moved.json()["done"] and _ledger(db) == []


def test_the_last_move_earns_once_and_finish_tells_it_again(client, db, admin, admin_headers):
    course = _course(db, admin, "Curso", modules=2)
    first, _ = _modules(db, course)
    activity = _activity(db, first)
    ana = _learner(db)
    _enroll(client, admin_headers, course, ana)
    learner = auth_headers(client, ana.email)

    play = _start(client, learner, activity.id)
    assert _choose(client, learner, play, "Le doy").json()["done"]
    finished = client.post(f"{API}/learn/activity-attempts/{play['attempt_id']}/finish", headers=learner)

    assert finished.status_code == 200 and finished.json()["score"] == 2
    assert _ledger(db) == [(f"activity:{activity.id}", 20), (f"module:{first.id}", 20)]


def test_a_stale_play_closed_when_starting_again_earns_its_points(client, db, admin, admin_headers):
    """A timed game whose clock ran out with a move made ends for real when the learner starts again: it is counted
    (with its points) by the same helper as a finished play."""
    course = _course(db, admin, "Patio", modules=2)
    first, _ = _modules(db, course)
    quick = _activity(db, first, spec=QUICK_ORDER, kind="quick")
    ana = _learner(db)
    _enroll(client, admin_headers, course, ana)
    learner = auth_headers(client, ana.email)
    play = _start(client, learner, quick.id)  # timed: 4 x (10 + 10) + 20 of time bonus = 100 at most
    token = {card["text"]: card["token"] for card in play["view"]["cards"]}
    client.post(f"{API}/learn/activity-attempts/{play['attempt_id']}/moves", headers=learner,
                json={"move": {"card": token[QUICK_ORDER["items"][0]]}})  # 10 points
    _run_out_the_clock(db, play["attempt_id"])

    assert not _start(client, learner, quick.id)["resumed"]

    assert _ledger(db) == [(f"activity:{quick.id}", 10), (f"module:{first.id}", 20)]


def test_a_quiz_earns_only_what_it_improves_on_the_best_score(client, db, admin, admin_headers):
    course = _course(db, admin, "Seguridad")
    (module,) = _modules(db, course)
    evaluation = Evaluation(module_id=module.id, spec=QUESTIONS, max_attempts=5, passing_score=90)
    db.add(evaluation)
    db.commit()
    ana = _learner(db)
    _enroll(client, admin_headers, course, ana)
    learner = auth_headers(client, ana.email)
    quizzed = f"quiz:{module.id}"

    for correct in (3, 2, 4):  # 60, 40 (worse: nothing), 80
        _answer(client, learner, module, evaluation, correct=correct)
    assert _ledger(db) == [(quizzed, 60), (quizzed, 20)]
    passed = _answer(client, learner, module, evaluation)  # 100: passed, the module and the course completed

    assert passed["passed"] and passed["course_completed"]
    assert _ledger(db) == [(quizzed, 60), (quizzed, 20), (quizzed, 20), (f"module:{module.id}", 20),
                           (f"course:{course.id}", 100)]


# ── Milestones: once ──────────────────────────────────────────────────


def test_modules_courses_and_paths_earn_their_points_once(client, db, admin, admin_headers):
    course = _course(db, admin, "Curso 1", modules=2)
    path = _path(client, admin_headers, [course], title="Ruta")
    ana = _learner(db)
    _assign(client, admin_headers, path, ana)
    learner = auth_headers(client, ana.email)
    one, two = _modules(db, course)

    _complete(client, learner, db, course)
    _complete(client, learner, db, course)  # marked again: nothing more

    earned = [(f"module:{one.id}", 20), (f"module:{two.id}", 20), (f"course:{course.id}", 100),
              (f"path:{path['id']}", 200)]
    assert _ledger(db) == earned
    by_path = db.query(PointsEntry).filter(PointsEntry.source == "path").one()
    assert (by_path.course_id, by_path.path_id) == (None, path["id"])

    # A module added reopens the course and the path: completed again, only the new module earns.
    client.post(f"{API}/courses/{course.id}/modules", headers=admin_headers, json={"title": "Nuevo",
                                                                                    "content_text": "x"})
    db.expire_all()
    assert db.query(Enrollment).one().status == "in_progress"
    _complete(client, learner, db, course)
    three = _modules(db, course)[-1]
    assert _ledger(db) == [*earned, (f"module:{three.id}", 20)]


def test_the_parameters_set_the_milestones_points(client, db, admin, admin_headers):
    _settings(client, admin_headers, ranking={"points_module": 5, "points_course": 0})
    course = _course(db, admin, "Curso")
    (module,) = _modules(db, course)
    ana = _learner(db)
    _enroll(client, admin_headers, course, ana)

    _complete(client, auth_headers(client, ana.email), db, course)

    # Earned at the value of the day, 0 included: raising it later never pays the course again.
    assert _ledger(db) == [(f"module:{module.id}", 5), (f"course:{course.id}", 0)]


def test_an_admins_change_completing_many_earns_for_everyone(client, db, admin, admin_headers):
    course = _course(db, admin, "Inducción", modules=2)
    done, pending = _modules(db, course)
    people = [_learner(db, f"p{i}@test.dev") for i in range(3)]
    _enroll(client, admin_headers, course, *people)
    for person in people[:2]:
        client.post(f"{API}/learn/modules/{done.id}/complete", headers=auth_headers(client, person.email))

    assert client.delete(f"{API}/modules/{pending.id}", headers=admin_headers).status_code == 204

    for person in people[:2]:
        assert _ledger(db, person) == [(f"module:{done.id}", 20), (f"course:{course.id}", 100)]
    assert _ledger(db, people[2]) == []


def test_learners_waiting_for_an_unpublished_activity_earn_the_module(client, db, admin, admin_headers):
    course = _course(db, admin, "Llantas")
    (module,) = _modules(db, course)
    evaluation = Evaluation(module_id=module.id, spec=QUESTIONS, max_attempts=3, passing_score=70)
    db.add(evaluation)
    db.commit()
    activity = _activity(db, module)
    ana = _learner(db)
    _enroll(client, admin_headers, course, ana)
    passed = _answer(client, auth_headers(client, ana.email), module, evaluation)
    assert passed["passed"] and not passed["module_completed"]  # still waits for the activity

    unpublished = client.put(f"{API}/activities/{activity.id}", headers=admin_headers, json={"status": "draft"})

    assert unpublished.status_code == 200, unpublished.text
    assert _ledger(db) == [(f"quiz:{module.id}", 100), (f"module:{module.id}", 20), (f"course:{course.id}", 100)]


def test_deleting_a_course_or_a_path_takes_its_points_away(client, db, admin, admin_headers):
    kept, gone = _course(db, admin, "Se queda"), _course(db, admin, "Se va")
    path = _path(client, admin_headers, [kept], title="Ruta")
    ana = _learner(db)
    _assign(client, admin_headers, path, ana)
    _enroll(client, admin_headers, gone, ana)
    learner = auth_headers(client, ana.email)
    for course in (kept, gone):
        _complete(client, learner, db, course)

    assert client.delete(f"{API}/courses/{gone.id}", headers=admin_headers).status_code == 204
    assert {key for key, _ in _ledger(db)} == {f"module:{_modules(db, kept)[0].id}", f"course:{kept.id}",
                                                f"path:{path['id']}"}
    assert client.delete(f"{API}/paths/{path['id']}", headers=admin_headers).status_code == 204
    assert {key for key, _ in _ledger(db)} == {f"module:{_modules(db, kept)[0].id}", f"course:{kept.id}"}


def test_percentages_round_half_up_exactly():
    assert [points.percent(score, 200) for score in (0, 1, 29, 199, 200, 260)] == [0, 1, 15, 100, 100, 100]
    assert points.percent(-4, 10) == 0 and points.percent(5, 0) == 0
    assert [points.whole(score) for score in (0.0, 12.5, 66.7, 99.9, 100.0)] == [0, 13, 67, 100, 100]


# ── The past ──────────────────────────────────────────────────────────


def _at(day: int, hour: int = 12) -> datetime:
    return datetime(2026, 3, day, hour, tzinfo=timezone.utc)


def _naive(moment: datetime) -> datetime:
    return moment.replace(tzinfo=None)  # older columns hold naive UTC


def _history(client, db, admin, admin_headers, email: str):
    """What a learner did before points existed: written straight into the tables, as the previous release left it."""
    course = _course(db, admin, f"Curso de {email}", modules=2)
    first, second = _modules(db, course)
    activity = _activity(db, first)
    evaluation = Evaluation(module_id=second.id, spec=QUESTIONS, max_attempts=3, passing_score=70)
    db.add(evaluation)
    path = _path(client, admin_headers, [course], title=f"Ruta de {email}")
    person = _learner(db, email)
    _assign(client, admin_headers, path, person)
    enrollment = db.query(Enrollment).filter(Enrollment.user_id == person.id).one()
    enrollment.status, enrollment.completed_at = "completed", _at(5)
    db.add_all([
        ModuleProgress(enrollment_id=enrollment.id, module_id=first.id, completed=True, completed_at=_naive(_at(2))),
        ModuleProgress(enrollment_id=enrollment.id, module_id=second.id, completed=True, passed=True, score=100,
                       attempts=2, completed_at=_naive(_at(4))),
        *(EvaluationAttempt(evaluation_id=evaluation.id, user_id=person.id, enrollment_id=enrollment.id,
                            module_id=second.id, score=score, passed=score >= 70, attempt_number=number,
                            created_at=_naive(_at(day)))
          for number, (score, day) in enumerate([(60.0, 3), (40.0, 3), (100.0, 4)], start=1)),
        *(ActivityAttempt(activity_id=activity.id, user_id=person.id, enrollment_id=enrollment.id, version=1,
                          state={"done": True}, score=score, max_score=10, finished_at=_at(day))
          for score, day in [(2, 1), (7, 2), (7, 2)]),
        ActivityAttempt(activity_id=activity.id, user_id=admin.id, enrollment_id=None, version=1,
                        state={"done": True}, score=10, max_score=10, finished_at=_at(1)),  # a preview
    ])
    db.query(PathAssignment).filter(PathAssignment.user_id == person.id).update({"completed_at": _at(6)})
    db.commit()
    keys = {"activity": f"activity:{activity.id}", "quiz": f"quiz:{second.id}", "first": f"module:{first.id}",
            "second": f"module:{second.id}", "course": f"course:{course.id}", "path": f"path:{path['id']}"}
    return person, keys


def _dated(db, user) -> list[tuple[str, int, datetime]]:
    db.expire_all()
    rows = db.query(PointsEntry).filter(PointsEntry.user_id == user.id)
    return sorted((row.source_key, row.points, points.aware(row.earned_at)) for row in rows)


def test_the_maintenance_pass_derives_the_past_dated_when_it_happened(
    client, db, admin, admin_headers, worker, storage, caplog, monkeypatch
):
    from app.worker.jobs import recording

    ana, key = _history(client, db, admin, admin_headers, "ana@test.dev")
    written_before_the_videos, migrate_videos = [], recording._migrate_videos

    def videos(session):
        written_before_the_videos.append(session.query(PointsEntry).count())
        return migrate_videos(session)

    monkeypatch.setattr(recording, "_migrate_videos", videos)

    queue.enqueue(db, "legacy.migrate", dedupe_key="legacy-migrate")
    with caplog.at_level(logging.INFO, logger="app.services.points"):
        worker()

    db.expire_all()
    job = db.query(Job).filter(Job.type == "legacy.migrate").one()
    assert job.status == "succeeded" and job.result["points_backfilled"] == 8
    # Seconds after a deploy, not once every HeyGen video is downloaded: a replay meanwhile finds its past written.
    assert written_before_the_videos == [8]
    assert _dated(db, ana) == sorted([
        (key["first"], 20, _at(2)), (key["second"], 20, _at(4)), (key["course"], 100, _at(5)),
        (key["path"], 200, _at(6)),
        (key["activity"], 20, _at(1)), (key["activity"], 50, _at(2)),  # each time a play beat the best
        (key["quiz"], 60, _at(3)), (key["quiz"], 40, _at(4)),
    ])
    assert _ledger(db, admin) == []  # its preview earned nothing
    (logged,) = [record for record in caplog.records if record.getMessage() == "points_backfilled"]
    assert logged.count == 8

    assert points.backfill(db) == 0  # idempotent


def test_the_past_of_someone_who_earned_points_since_the_deploy_still_comes(client, db, admin, admin_headers):
    """Points earned between a deploy and its maintenance pass: the past of everything else is still derived, and
    what already has points is left as it is (its first points since wrote its past)."""
    ana, key = _history(client, db, admin, admin_headers, "ana@test.dev")
    db.add(PointsEntry(user_id=ana.id, source="activity", source_key=key["activity"], points=70,
                       earned_at=_at(20)))  # the activity's points, written since the deploy
    db.commit()

    points.backfill(db)

    earned = _dated(db, ana)
    assert [row for row in earned if row[0] == key["activity"]] == [(key["activity"], 70, _at(20))]
    assert sum(row[1] for row in earned) == 70 + 20 + 20 + 100 + 200 + 100


def test_a_replay_before_the_maintenance_pass_writes_its_past_and_earns_only_what_it_improves(
    client, db, admin, admin_headers
):
    """Replaying an old activity after a deploy, before its maintenance pass: its past comes first, dated when it
    happened as the pass would write it, so a replay below the old best earns nothing today (not the old best at
    once); the pass then adds the rest of the past without that activity twice."""
    ana, key = _history(client, db, admin, admin_headers, "ana@test.dev")  # their best play: 70 %
    learner = auth_headers(client, ana.email)
    activity_id = int(key["activity"].removeprefix("activity:"))
    deployed = datetime.now(timezone.utc)

    _converse(client, learner, activity_id, *POOR)  # 20 %

    assert [row for row in _dated(db, ana) if row[0] == key["activity"]] == [
        (key["activity"], 20, _at(1)), (key["activity"], 50, _at(2))]
    _converse(client, learner, activity_id, *BEST)  # 100 %: 30 more, today
    points.backfill(db)

    earned = _dated(db, ana)
    assert [(source_key, gained) for source_key, gained, at in earned if at >= deployed] == [(key["activity"], 30)]
    assert [row for row in earned if row[2] < deployed] == sorted([
        (key["first"], 20, _at(2)), (key["second"], 20, _at(4)), (key["course"], 100, _at(5)),
        (key["path"], 200, _at(6)), (key["activity"], 20, _at(1)), (key["activity"], 50, _at(2)),
        (key["quiz"], 60, _at(3)), (key["quiz"], 40, _at(4)),
    ])


def test_a_quiz_taken_before_the_maintenance_pass_keeps_the_best_of_its_past(client, db, admin, admin_headers):
    """A quiz not passed yet whose best was 80 before points existed: an attempt of 40 after a deploy writes that
    past, dated when it happened, and earns nothing (the quiz is worth 80, not 40, also once the pass comes)."""
    course = _course(db, admin, "Seguridad")
    (module,) = _modules(db, course)
    evaluation = Evaluation(module_id=module.id, spec=QUESTIONS, max_attempts=5, passing_score=90)
    db.add(evaluation)
    db.commit()
    ana = _learner(db)
    _enroll(client, admin_headers, course, ana)
    enrollment = db.query(Enrollment).filter(Enrollment.user_id == ana.id).one()
    db.add_all([
        ModuleProgress(enrollment_id=enrollment.id, module_id=module.id, attempts=2, score=80, passed=False),
        *(EvaluationAttempt(evaluation_id=evaluation.id, user_id=ana.id, enrollment_id=enrollment.id,
                            module_id=module.id, score=score, passed=False, attempt_number=number,
                            created_at=_naive(_at(day)))
          for number, (score, day) in enumerate([(60.0, 3), (80.0, 4)], start=1)),
    ])
    db.commit()
    learner = auth_headers(client, ana.email)
    quizzed = f"quiz:{module.id}"

    assert _answer(client, learner, module, evaluation, correct=2)["score"] == 40

    assert _dated(db, ana) == [(quizzed, 20, _at(4)), (quizzed, 60, _at(3))]
    assert points.backfill(db) == 0
    assert _answer(client, learner, module, evaluation)["passed"]  # 100: 20 more, the module and the course
    assert _ledger(db) == [(quizzed, 60), (quizzed, 20), (quizzed, 20), (f"module:{module.id}", 20),
                           (f"course:{course.id}", 100)]
