"""On a real PostgreSQL (foreign keys and row locks): deletes, concurrent submissions and query budgets."""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import text

from app.db.models import ActivityAttempt, Enrollment, Evaluation, EvaluationAttempt, ModuleProgress, User
from tests.conftest import QueryCounter, auth_headers
from tests.test_activity_spec import DECISION, QUICK_ORDER

QUESTION = {"id": "q1", "type": "true_false", "prompt": "El casco es obligatorio", "correct": True, "explanation": ""}


@pytest.fixture
def engine(pg_engine):  # conftest's db, session_factory and client then run on Postgres
    return pg_engine


def _course(client, admin_headers, collaborator, *, quiz=True, max_attempts=3, modules=1):
    course = client.post("/api/v1/courses", headers=admin_headers, json={"title": "C"}).json()
    ids = [
        client.post(
            f"/api/v1/courses/{course['id']}/modules", headers=admin_headers, json={"title": f"M{i}", "content_text": "x"}
        ).json()["id"]
        for i in range(modules)
    ]
    if quiz:
        client.put(
            f"/api/v1/modules/{ids[-1]}/evaluation", headers=admin_headers,
            json={"questions": [QUESTION], "max_attempts": max_attempts},
        )
    assert client.post(f"/api/v1/courses/{course['id']}/publish", headers=admin_headers).status_code == 200
    client.post(f"/api/v1/courses/{course['id']}/participants", headers=admin_headers, json={"user_ids": [collaborator.id]})
    return course, ids


def _count(db, sql: str) -> int:
    return db.execute(text(sql)).scalar()


def test_deletes_respect_foreign_keys(client, db, admin, collaborator):
    admin_headers = auth_headers(client, admin.email)
    course, (first, second) = _course(client, admin_headers, collaborator, modules=2)
    learner = auth_headers(client, collaborator.email)
    client.post(f"/api/v1/learn/modules/{first}/complete", headers=learner)
    assert client.post(f"/api/v1/learn/modules/{second}/quiz/attempts", headers=learner, json={"answers": []}).status_code == 200
    enrollment_id = db.query(Enrollment.id).scalar()
    db.execute(text("INSERT INTO certificates (enrollment_id) VALUES (:id)"), {"id": enrollment_id})
    db.execute(
        text("INSERT INTO user_videos (id, module_id, user_id, storage_url) VALUES ('v1', :a, :u, 'x'), ('v2', :b, :u, 'y')"),
        {"a": first, "b": second, "u": admin.id},
    )
    db.commit()

    # The module with the quiz: attempts, progress and evaluation go; the old recording stays, unlinked.
    assert client.delete(f"/api/v1/modules/{second}", headers=admin_headers).status_code == 204
    db.expire_all()
    assert db.query(EvaluationAttempt).count() == 0 and db.query(Evaluation).count() == 0
    assert _count(db, "SELECT module_id FROM user_videos WHERE id = 'v2'") is None

    # The participant (their certificate from the previous app too).
    assert client.delete(f"/api/v1/courses/{course['id']}/participants/{collaborator.id}", headers=admin_headers).status_code == 204
    assert _count(db, "SELECT count(*) FROM certificates") == 0 and db.query(ModuleProgress).count() == 0

    # The whole course.
    client.post(f"/api/v1/courses/{course['id']}/participants", headers=admin_headers, json={"user_ids": [collaborator.id]})
    db.execute(text("INSERT INTO certificates (enrollment_id) VALUES (:id)"), {"id": db.query(Enrollment.id).scalar()})
    db.commit()
    assert client.delete(f"/api/v1/courses/{course['id']}", headers=admin_headers).status_code == 204
    for table in ("courses", "modules", "enrollments", "certificates"):
        assert _count(db, f"SELECT count(*) FROM {table}") == 0
    assert _count(db, "SELECT count(*) FROM user_videos WHERE module_id IS NULL") == 2


def test_a_new_quiz_starts_with_fresh_attempts(client, db, admin, collaborator):
    admin_headers = auth_headers(client, admin.email)
    _, (module_id,) = _course(client, admin_headers, collaborator, max_attempts=1)
    learner = auth_headers(client, collaborator.email)
    url = f"/api/v1/learn/modules/{module_id}/quiz/attempts"
    client.post(url, headers=learner, json={"answers": []})
    assert client.post(url, headers=learner, json={"answers": []}).status_code == 403  # out of attempts

    assert client.delete(f"/api/v1/modules/{module_id}/evaluation", headers=admin_headers).status_code == 204
    client.put(f"/api/v1/modules/{module_id}/evaluation", headers=admin_headers, json={"questions": [QUESTION], "max_attempts": 1})

    assert client.get(f"/api/v1/learn/modules/{module_id}/quiz", headers=learner).json()["attempts_used"] == 0
    assert client.post(url, headers=learner, json={"answers": []}).status_code == 200


@pytest.mark.parametrize("started", [False, True])
def test_concurrent_submissions_respect_max_attempts(client, db, admin, collaborator, started):
    admin_headers = auth_headers(client, admin.email)
    _, (module_id,) = _course(client, admin_headers, collaborator, max_attempts=2)
    learner = auth_headers(client, collaborator.email)
    if started:  # the progress row exists before the race
        client.put(f"/api/v1/learn/modules/{module_id}/position", headers=learner, json={"seconds": 1})
    barrier = threading.Barrier(6)

    def submit(_):
        barrier.wait()
        url = f"/api/v1/learn/modules/{module_id}/quiz/attempts"
        return client.post(url, headers=learner, json={"answers": []}).status_code

    with ThreadPoolExecutor(6) as pool:
        codes = list(pool.map(submit, range(6)))

    db.expire_all()
    assert codes.count(200) == 2
    assert sorted(n for (n,) in db.query(EvaluationAttempt.attempt_number)) == [1, 2]
    assert db.query(ModuleProgress).one().attempts == 2


def test_concurrent_passing_submissions_pass_once(client, db, admin, collaborator):
    admin_headers = auth_headers(client, admin.email)
    _, (module_id,) = _course(client, admin_headers, collaborator, max_attempts=5)
    learner = auth_headers(client, collaborator.email)
    answers = [{"question_id": "q1", "response": {"value": True}}]
    barrier = threading.Barrier(6)

    def submit(_):
        barrier.wait()
        url = f"/api/v1/learn/modules/{module_id}/quiz/attempts"
        return client.post(url, headers=learner, json={"answers": answers}).status_code

    with ThreadPoolExecutor(6) as pool:
        codes = list(pool.map(submit, range(6)))

    db.expire_all()
    assert codes.count(200) == 1 and codes.count(409) == 5
    assert db.query(Enrollment).one().status == "completed"
    assert _count(db, "SELECT count(*) FROM learner_certificates") == 1


def test_saving_the_position_never_undoes_a_completion(client, db, session_factory, admin, collaborator, monkeypatch):
    """The first position save racing with the completion of a one-module course."""
    from app.api.routes import learn
    from app.schemas.learn import PositionUpdate
    from app.services import progress

    _, (module_id,) = _course(client, auth_headers(client, admin.email), collaborator, quiz=False)
    reached, go = threading.Event(), threading.Event()
    mark_completed = progress.mark_completed

    def slow_mark_completed(record):
        reached.set()
        go.wait(5)
        mark_completed(record)

    monkeypatch.setattr(progress, "mark_completed", slow_mark_completed)
    errors = []

    def run(call):
        with session_factory() as session:
            try:
                call(session, session.get(User, collaborator.id))
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(exc)

    complete = threading.Thread(target=run, args=(lambda s, u: learn.complete_module(module_id, db=s, user=u),))
    complete.start()
    assert reached.wait(5)  # the progress row exists and the completion is about to be written
    position = threading.Thread(
        target=run, args=(lambda s, u: learn.save_position(module_id, PositionUpdate(seconds=12), db=s, user=u),)
    )
    position.start()
    time.sleep(0.5)  # the position save read the enrollment as 'assigned' and waits for the row
    go.set()
    complete.join(10)
    position.join(10)

    assert not errors, errors
    db.expire_all()
    enrollment = db.query(Enrollment).one()
    assert enrollment.status == "completed" and enrollment.progress_pct == 100.0


def _seed_audience(db, course_id: int, module_ids: list[int], learners: int) -> None:
    run = lambda sql, **params: db.execute(text(sql), params)  # noqa: E731
    run(
        "INSERT INTO users (name, email, role, password_hash) "
        "SELECT 'u' || g, 'u' || g || '@x.test', 'collaborator', 'x' FROM generate_series(1, :n) g",
        n=learners,
    )
    run(
        "INSERT INTO enrollments (user_id, course_id, status, progress_pct) "
        "SELECT id, :c, 'in_progress', 50 FROM users WHERE email LIKE 'u%@x.test'",
        c=course_id,
    )
    run(
        "INSERT INTO module_progress (enrollment_id, module_id, completed, passed, attempts, completed_at) "
        "SELECT e.id, m, true, true, 1, now() FROM enrollments e CROSS JOIN unnest(CAST(:m AS int[])) m "
        "WHERE e.course_id = :c",
        c=course_id, m=module_ids[:2],
    )
    evaluation_id = run("SELECT id FROM evaluations WHERE module_id = :m", m=module_ids[-1]).scalar()
    run(
        "INSERT INTO evaluation_attempts (evaluation_id, user_id, enrollment_id, module_id, answers_json, results, "
        "score, passed, attempt_number) SELECT :ev, e.user_id, e.id, :m, '[]', CAST(:r AS jsonb), 0, false, 1 "
        "FROM enrollments e WHERE e.course_id = :c",
        ev=evaluation_id, m=module_ids[-1], c=course_id, r=json.dumps([{"question_id": "q1", "correct": False}]),
    )
    db.commit()


def test_editor_writes_do_not_grow_with_the_audience(client, db, engine, admin, collaborator):
    admin_headers = auth_headers(client, admin.email)
    course, module_ids = _course(client, admin_headers, collaborator, modules=3)
    _seed_audience(db, course["id"], module_ids, learners=300)
    counter = QueryCounter(engine)

    def statements(method: str, url: str, **kwargs) -> int:
        with counter:
            response = client.request(method, url, headers=admin_headers, **kwargs)
        assert response.status_code < 300, response.text
        return counter.count

    budget = {
        "add module": statements("POST", f"/api/v1/courses/{course['id']}/modules", json={"title": "Nuevo"}),
        "reorder": statements(
            "PUT", f"/api/v1/courses/{course['id']}/modules/order",
            json={"module_ids": [*reversed(module_ids), db.execute(text("SELECT max(id) FROM modules")).scalar()]},
        ),
        "delete module": statements("DELETE", f"/api/v1/modules/{module_ids[-1]}"),
        "delete course": statements("DELETE", f"/api/v1/courses/{course['id']}"),
    }

    assert all(count <= 30 for count in budget.values()), budget


def _race(session_factory, first, second, *, paused: threading.Event, release: threading.Event) -> list:
    """`first` runs until it sets `paused`; `second` starts and blocks on it; then both finish."""
    errors = []

    def run(call):
        session = session_factory()
        try:
            call(session)
        except Exception as exc:  # pragma: no cover - reported by the caller
            errors.append(exc)
        finally:
            session.close()

    one = threading.Thread(target=run, args=(first,))
    one.start()
    assert paused.wait(5)
    two = threading.Thread(target=run, args=(second,))
    two.start()
    time.sleep(0.5)  # the second request is waiting for a row the first one locked
    release.set()
    one.join(15)
    two.join(15)
    return errors


def test_position_save_and_submission_lock_in_the_same_order(client, db, session_factory, admin, collaborator):
    """Old app data: 'assigned' with a progress row (a failed attempt). Both requests on the same module."""
    from app.api.routes import learn
    from app.schemas.learn import AttemptCreate, PositionUpdate

    _, (module_id,) = _course(client, auth_headers(client, admin.email), collaborator, max_attempts=5)
    enrollment = db.query(Enrollment).one()
    db.add(ModuleProgress(enrollment_id=enrollment.id, module_id=module_id, completed=False, passed=False, attempts=1))
    enrollment.status = "assigned"
    db.commit()
    paused, release = threading.Event(), threading.Event()

    def save_position(session):
        real_commit = session.commit

        def commit_later():
            paused.set()  # every statement of the request ran; only the commit is left
            release.wait(5)
            real_commit()

        session.commit = commit_later
        learn.save_position(module_id, PositionUpdate(seconds=12), db=session, user=session.get(User, collaborator.id))

    def submit(session):
        learn.submit_quiz(module_id, AttemptCreate(answers=[]), db=session, user=session.get(User, collaborator.id))

    errors = _race(session_factory, save_position, submit, paused=paused, release=release)

    assert not errors, errors  # without the flush in save_position: DeadlockDetected (enrollment, then progress)
    db.expire_all()
    assert db.query(ModuleProgress).one().attempts == 2


def test_deleting_a_module_waits_for_a_submission_in_flight(client, db, session_factory, admin, collaborator, monkeypatch):
    from app.api.routes import courses, learn
    from app.schemas.learn import AttemptCreate
    from app.services import quiz

    admin_headers = auth_headers(client, admin.email)
    _, (first, second) = _course(client, admin_headers, collaborator, modules=2, max_attempts=5)
    learner = auth_headers(client, collaborator.email)
    client.post(f"/api/v1/learn/modules/{first}/complete", headers=learner)
    client.put(f"/api/v1/learn/modules/{second}/position", headers=learner, json={"seconds": 3})
    paused, release = threading.Event(), threading.Event()
    grade = quiz.grade

    def slow_grade(*args, **kwargs):
        paused.set()  # the progress row is locked
        release.wait(5)
        return grade(*args, **kwargs)

    monkeypatch.setattr(quiz, "grade", slow_grade)

    errors = _race(
        session_factory,
        lambda s: learn.submit_quiz(second, AttemptCreate(answers=[]), db=s, user=s.get(User, collaborator.id)),
        lambda s: courses.delete_module(second, db=s),
        paused=paused,
        release=release,
    )

    assert not errors, errors  # foreign key violation on evaluations, before
    db.expire_all()
    assert db.query(EvaluationAttempt).count() == 0 and db.query(Evaluation).count() == 0


def _activity_course(client, admin_headers, collaborator, kind: str = "decision") -> dict:
    """A published one-module course whose only activity is of this kind, with the collaborator enrolled."""
    _, (module_id,) = _course(client, admin_headers, collaborator, quiz=False)
    spec = {"decision": DECISION, "quick": QUICK_ORDER}[kind]
    return client.post(f"/api/v1/modules/{module_id}/activities", headers=admin_headers,
                       json={"type": kind, "title": "Actividad", "spec": spec, "status": "ready"}).json()


def _open_plays(db) -> list[int]:
    db.expire_all()
    return [play_id for (play_id,) in db.query(ActivityAttempt.id).filter(ActivityAttempt.finished_at.is_(None))]


@pytest.mark.parametrize("stale", ["edited", "timed_out"])
def test_two_taps_on_start_close_a_stale_play_once(
    client, db, session_factory, admin, collaborator, monkeypatch, stale
):
    """
    A play that can't go on: both requests find it and want to deal with it (an untouched one on an older version is
    deleted, one with a move whose clock ran out is counted). The second waits for the first one's lock, finds
    nothing left to close, then picks up the new play the first one opened instead of opening another.
    """
    from app.api.routes import learn
    from app.schemas.learn import ActivityStart
    from app.services import activities

    admin_headers = auth_headers(client, admin.email)
    activity = _activity_course(client, admin_headers, collaborator, "decision" if stale == "edited" else "quick")
    learner = auth_headers(client, collaborator.email)
    play = client.post(f"/api/v1/learn/activities/{activity['id']}/attempts", headers=learner, json={}).json()
    if stale == "edited":
        client.put(f"/api/v1/activities/{activity['id']}", headers=admin_headers, json={"title": "Otra"})
    else:
        first_step = next(card["token"] for card in play["view"]["cards"] if card["text"] == QUICK_ORDER["items"][0])
        client.post(f"/api/v1/learn/activity-attempts/{play['attempt_id']}/moves", headers=learner,
                    json={"move": {"card": first_step}})
        attempt = db.get(ActivityAttempt, play["attempt_id"])
        db.refresh(attempt)
        attempt.state = {**attempt.state, "deadline": "2020-01-01T00:00:00+00:00"}
        db.commit()
    paused, release = threading.Event(), threading.Event()
    played, closing, starts = activities.played, [], []

    def slow_played(attempt):
        closing.append(attempt.id)
        if not paused.is_set():  # the first request holds the stale play's lock
            paused.set()
            release.wait(5)
        return played(attempt)

    monkeypatch.setattr(activities, "played", slow_played)

    def start(session):
        user = session.get(User, collaborator.id)
        starts.append(learn.start_activity(activity["id"], ActivityStart(), db=session, user=user))

    errors = _race(session_factory, start, start, paused=paused, release=release)

    assert not errors, errors
    assert closing == [play["attempt_id"]]  # without the lock, both close it: deleted (or counted) twice
    assert [s.resumed for s in starts] == [False, True] and starts[0].attempt_id == starts[1].attempt_id
    assert _open_plays(db) == [starts[0].attempt_id]
    old = db.get(ActivityAttempt, play["attempt_id"])
    if stale == "edited":
        assert old is None
    else:
        assert (old.score, old.finished_at is not None) == (10, True)
        assert db.query(Enrollment).one().status == "completed"  # the course's only activity, played


def test_two_taps_on_start_open_one_play(client, db, session_factory, admin, collaborator, monkeypatch):
    """Nothing open yet, so the open plays lock nothing: both requests find no play to resume. The enrollment, locked
    before a play is opened, makes the second wait, then pick up the play the first one opened."""
    from app.api.routes import learn
    from app.schemas.learn import ActivityStart
    from app.services import activities

    activity = _activity_course(client, auth_headers(client, admin.email), collaborator)
    paused, release = threading.Event(), threading.Event()
    new_state, starts = activities.start, []

    def slow_start(*args, **kwargs):
        if not paused.is_set():  # the first request holds the enrollment, about to open its play
            paused.set()
            release.wait(5)
        return new_state(*args, **kwargs)

    monkeypatch.setattr(activities, "start", slow_start)

    def start(session):
        user = session.get(User, collaborator.id)
        starts.append(learn.start_activity(activity["id"], ActivityStart(), db=session, user=user))

    errors = _race(session_factory, start, start, paused=paused, release=release)

    assert not errors, errors
    assert [s.resumed for s in starts] == [False, True] and starts[0].attempt_id == starts[1].attempt_id
    assert _open_plays(db) == [starts[0].attempt_id]  # two, before


def test_a_start_waits_for_the_last_move_in_flight(client, db, session_factory, admin, collaborator, monkeypatch):
    """
    The last move holds its play and the module's progress, and records the play with the enrollment last. A start of
    the same activity waits for that play before it locks the enrollment, then opens a new one. Locking the enrollment
    first, the start would hold what the move needs while waiting for the play the move holds: a deadlock.
    """
    from app.api.routes import learn
    from app.schemas.learn import ActivityMoveIn, ActivityStart
    from app.services import progress

    activity = _activity_course(client, auth_headers(client, admin.email), collaborator)
    learner = auth_headers(client, collaborator.email)
    # The video watched first: the move locks the existing progress row (inserting it, it would also hold the
    # enrollment through the foreign key, and the start would wait for it before anything else).
    client.put(f"/api/v1/learn/modules/{activity['module_id']}/position", headers=learner, json={"seconds": 5})
    play = client.post(f"/api/v1/learn/activities/{activity['id']}/attempts", headers=learner, json={}).json()
    price = next(c["token"] for c in play["view"]["node"]["choices"] if c["text"].startswith("Le doy"))  # it ends it
    paused, release = threading.Event(), threading.Event()
    complete_if_done, starts = progress.complete_if_done, []

    def slow_complete_if_done(*args, **kwargs):
        paused.set()  # the move holds its play and the module's progress
        release.wait(5)
        return complete_if_done(*args, **kwargs)

    monkeypatch.setattr(progress, "complete_if_done", slow_complete_if_done)

    def last_move(session):
        learn.play_move(play["attempt_id"], ActivityMoveIn(move={"choice": price}), db=session,
                        user=session.get(User, collaborator.id))

    def start(session):
        user = session.get(User, collaborator.id)
        starts.append(learn.start_activity(activity["id"], ActivityStart(), db=session, user=user))

    errors = _race(session_factory, last_move, start, paused=paused, release=release)

    assert not errors, errors  # DeadlockDetected, with the enrollment locked first
    (fresh,) = starts
    assert not fresh.resumed and _open_plays(db) == [fresh.attempt_id]
    assert db.get(ActivityAttempt, play["attempt_id"]).score == 2 and db.query(Enrollment).one().status == "completed"


def test_deleting_an_activity_waits_for_a_start_closing_its_stale_play(
    client, db, session_factory, admin, collaborator, monkeypatch
):
    """The start holds the stale play and its new play needs the activity (a foreign key); the delete holds the
    activity and its cascade needs that play. Both lock the activity first, so the delete waits: no deadlock."""
    from app.api.routes import activities as admin_activities
    from app.api.routes import learn
    from app.schemas.learn import ActivityStart
    from app.services import activities

    admin_headers = auth_headers(client, admin.email)
    activity = _activity_course(client, admin_headers, collaborator)
    client.post(f"/api/v1/learn/activities/{activity['id']}/attempts", headers=auth_headers(client, collaborator.email),
                json={})
    client.put(f"/api/v1/activities/{activity['id']}", headers=admin_headers, json={"title": "Otra"})
    paused, release = threading.Event(), threading.Event()
    played = activities.played

    def slow_played(attempt):
        paused.set()  # the start holds the stale play
        release.wait(5)
        return played(attempt)

    monkeypatch.setattr(activities, "played", slow_played)

    errors = _race(
        session_factory,
        lambda s: learn.start_activity(activity["id"], ActivityStart(), db=s, user=s.get(User, collaborator.id)),
        lambda s: admin_activities.delete_activity(activity["id"], db=s),
        paused=paused,
        release=release,
    )

    assert not errors, errors  # DeadlockDetected, before
    db.expire_all()
    assert db.query(ActivityAttempt).count() == 0  # the new play went with the activity


def test_modules_added_at_once_by_two_admins_both_count(client, db, session_factory, admin, collaborator, monkeypatch):
    from app.api.routes import courses
    from app.schemas.courses import ModuleCreate
    from app.services import progress

    course, (first,) = _course(client, auth_headers(client, admin.email), collaborator, quiz=False)
    client.post(f"/api/v1/learn/modules/{first}/complete", headers=auth_headers(client, collaborator.email))
    paused, release = threading.Event(), threading.Event()
    apply_progress = progress._apply_progress

    def slow_apply(*args):
        if not paused.is_set():  # the first admin holds the enrollment locks
            paused.set()
            release.wait(5)
        apply_progress(*args)

    monkeypatch.setattr(progress, "_apply_progress", slow_apply)

    def add(title):
        return lambda s: courses.create_module(course["id"], ModuleCreate(title=title, content_text="x"), db=s)

    errors = _race(session_factory, add("A"), add("B"), paused=paused, release=release)

    assert not errors, errors
    db.expire_all()
    assert db.query(Enrollment).one().progress_pct == 33.3  # 1 of 3 (50.0 before: B missed A's module)


def test_assigning_many_people_is_one_insert(client, db, engine, admin, collaborator):
    admin_headers = auth_headers(client, admin.email)
    course = client.post("/api/v1/courses", headers=admin_headers, json={"title": "C"}).json()
    db.execute(text(
        "INSERT INTO users (name, email, role, password_hash, is_active) "
        "SELECT 'u' || g, 'u' || g || '@x.test', 'collaborator', 'x', true FROM generate_series(1, 300) g"
    ))
    db.commit()
    user_ids = [user_id for (user_id,) in db.execute(text("SELECT id FROM users WHERE email LIKE 'u%@x.test'"))]
    url = f"/api/v1/courses/{course['id']}/participants"

    with QueryCounter(engine) as counter:
        assert client.post(url, headers=admin_headers, json={"user_ids": user_ids}).status_code == 200
    assert counter.count <= 15, counter.count  # 910 with a savepoint per person

    barrier = threading.Barrier(4)

    def assign(_):
        barrier.wait()
        return client.post(url, headers=admin_headers, json={"user_ids": [collaborator.id]}).status_code

    with ThreadPoolExecutor(4) as pool:
        assert list(pool.map(assign, range(4))) == [200] * 4
    assert db.query(Enrollment).filter(Enrollment.user_id == collaborator.id).count() == 1


# ── Learning paths ────────────────────────────────────────────────────


def _audience_with_profile(db, learners: int) -> tuple[int, list[int]]:
    """A profile and `learners` active collaborators who have it."""
    profile_id = db.execute(text("INSERT INTO job_profiles (name) VALUES ('Asesor') RETURNING id")).scalar()
    db.execute(
        text(
            "INSERT INTO users (name, email, role, password_hash, is_active, profile_id) "
            "SELECT 'p' || g, 'p' || g || '@x.test', 'collaborator', 'x', true, :p FROM generate_series(1, :n) g"
        ),
        {"p": profile_id, "n": learners},
    )
    db.commit()
    return profile_id, [user_id for (user_id,) in db.execute(text("SELECT id FROM users WHERE email LIKE 'p%@x.test'"))]


def _path_with(client, admin_headers, course_ids: list[int], **changes) -> dict:
    path = client.post("/api/v1/paths", headers=admin_headers, json={"title": "Ruta"}).json()
    client.put(f"/api/v1/paths/{path['id']}/courses", headers=admin_headers, json={"course_ids": course_ids})
    if changes:
        client.patch(f"/api/v1/paths/{path['id']}", headers=admin_headers, json=changes)
    return path


def test_a_path_reaches_many_people_in_a_few_statements(client, db, engine, admin, collaborator):
    admin_headers = auth_headers(client, admin.email)
    course_ids = [_course(client, admin_headers, collaborator, quiz=False)[0]["id"] for _ in range(3)]
    profile_id, user_ids = _audience_with_profile(db, learners=300)
    by_profile = _path_with(client, admin_headers, course_ids, profile_ids=[profile_id], settings={"due_days": 30})
    by_hand = _path_with(client, admin_headers, course_ids, status="published")
    counter = QueryCounter(engine)

    with counter:
        published = client.patch(f"/api/v1/paths/{by_profile['id']}", headers=admin_headers,
                                 json={"status": "published"})
    assert published.status_code == 200, published.text
    publish_statements = counter.count
    with counter:
        assigned = client.post(f"/api/v1/paths/{by_hand['id']}/people", headers=admin_headers,
                               json={"user_ids": user_ids})
    assert assigned.status_code == 200 and len(assigned.json()) == 300
    assign_statements = counter.count  # the response's progress of 300 people included

    assert publish_statements <= 20 and assign_statements <= 20, (publish_statements, assign_statements)
    assert _count(db, "SELECT count(*) FROM path_assignments WHERE source = 'profile' AND due_at IS NOT NULL") == 300
    assert _count(db, "SELECT count(*) FROM path_assignments WHERE source = 'manual'") == 300
    assert _count(db, "SELECT count(*) FROM enrollments e JOIN users u ON u.id = e.user_id "
                      "WHERE u.email LIKE 'p%@x.test'") == 900


def test_bulk_completion_completes_and_reopens_paths_in_a_few_statements(client, db, engine, admin, collaborator):
    """An admin's module change completes (or reopens) a course for everyone at once: their paths and certificates
    follow, set-based (5 statements more for the 600 certificates: the parameters, a read and an insert each)."""
    admin_headers = auth_headers(client, admin.email)
    course, module_ids = _course(client, admin_headers, collaborator, modules=3)
    _seed_audience(db, course["id"], module_ids, learners=300)  # the first two modules completed by everyone
    learners = [user_id for (user_id,) in db.execute(text("SELECT id FROM users WHERE email LIKE 'u%@x.test'"))]
    path = _path_with(client, admin_headers, [course["id"]], status="published")
    client.post(f"/api/v1/paths/{path['id']}/people", headers=admin_headers, json={"user_ids": learners})
    counter = QueryCounter(engine)

    with counter:
        assert client.delete(f"/api/v1/modules/{module_ids[-1]}", headers=admin_headers).status_code == 204
    completing = counter.count
    completed = _count(db, "SELECT count(*) FROM path_assignments WHERE completed_at IS NOT NULL")
    with counter:
        client.post(f"/api/v1/courses/{course['id']}/modules", headers=admin_headers, json={"title": "Nuevo"})
    reopening = counter.count

    assert completing <= 30 and reopening <= 30, (completing, reopening)
    assert completed == 300
    assert _count(db, "SELECT count(*) FROM path_assignments WHERE completed_at IS NOT NULL") == 0
    issued = "SELECT count(*) FROM learner_certificates WHERE revoked_at IS NULL AND kind = '{}'"
    # The course's: the 300 and the collaborator (enrolled with them, not in the path); none revoked by the reopening.
    assert (_count(db, issued.format("course")), _count(db, issued.format("path"))) == (301, 300)


def test_path_deletes_respect_foreign_keys(client, db, admin, collaborator):
    admin_headers = auth_headers(client, admin.email)
    (course, _), (kept, _) = (_course(client, admin_headers, collaborator, quiz=False) for _ in range(2))
    profile = client.post("/api/v1/profiles", headers=admin_headers, json={"name": "Asesor"}).json()
    client.patch(f"/api/v1/users/{collaborator.id}", headers=admin_headers, json={"profile_id": profile["id"]})
    path = _path_with(client, admin_headers, [course["id"], kept["id"]], status="published",
                      profile_ids=[profile["id"]])
    assert _count(db, "SELECT count(*) FROM path_assignments") == 1

    assert client.delete(f"/api/v1/courses/{course['id']}", headers=admin_headers).status_code == 204
    assert _count(db, "SELECT count(*) FROM learning_path_courses") == 1
    assert client.delete(f"/api/v1/profiles/{profile['id']}", headers=admin_headers).status_code == 204
    assert _count(db, "SELECT count(*) FROM users WHERE profile_id IS NOT NULL") == 0
    assert _count(db, "SELECT count(*) FROM learning_path_profiles") == 0
    assert _count(db, "SELECT count(*) FROM path_assignments") == 0  # given by the profile, not completed
    client.post(f"/api/v1/paths/{path['id']}/people", headers=admin_headers, json={"user_ids": [collaborator.id]})
    assert client.delete(f"/api/v1/paths/{path['id']}", headers=admin_headers).status_code == 204
    for table in ("learning_paths", "learning_path_courses", "path_assignments"):
        assert _count(db, f"SELECT count(*) FROM {table}") == 0
    assert _count(db, f"SELECT count(*) FROM enrollments WHERE course_id = {kept['id']}") == 1  # kept


def _paused_before_commit(call, paused: threading.Event, release: threading.Event):
    """`call`, with every statement run and only its commit left until `release`."""

    def run(session):
        real_commit = session.commit

        def commit_later():
            paused.set()
            release.wait(5)
            real_commit()

        session.commit = commit_later
        call(session)

    return run


@pytest.mark.parametrize("first", ["path", "person"])
@pytest.mark.parametrize("change", ["publish", "link"])
def test_changing_a_path_while_giving_its_profile_assigns_it(
    client, db, session_factory, admin, collaborator, change, first
):
    """The path published, or linked to the profile, while someone gets that profile: whichever request commits last
    waited for the other one, and reads what it did."""
    from app.api.routes import paths as path_routes
    from app.api.routes import users as user_routes
    from app.schemas.paths import PathUpdate
    from app.schemas.users import UserUpdate

    admin_headers = auth_headers(client, admin.email)
    course, _ = _course(client, admin_headers, collaborator, quiz=False)
    profile = client.post("/api/v1/profiles", headers=admin_headers, json={"name": "Asesor"}).json()
    if change == "publish":
        path = _path_with(client, admin_headers, [course["id"]], profile_ids=[profile["id"]])  # a draft
        update = PathUpdate(status="published")
    else:
        path = _path_with(client, admin_headers, [course["id"]], status="published")
        update = PathUpdate(profile_ids=[profile["id"]])
    paused, release = threading.Event(), threading.Event()

    def change_path(session):
        path_routes.update_path(path["id"], update, db=session, admin=session.get(User, admin.id))

    def give_profile(session):
        user_routes.update_user(collaborator.id, UserUpdate(profile_id=profile["id"]), db=session,
                                admin=session.get(User, admin.id))

    one, two = (change_path, give_profile) if first == "path" else (give_profile, change_path)
    errors = _race(session_factory, _paused_before_commit(one, paused, release), two, paused=paused, release=release)

    assert not errors, errors
    assert _count(db, f"SELECT count(*) FROM path_assignments WHERE user_id = {collaborator.id}") == 1


def test_two_courses_of_a_path_completed_at_once_complete_it(client, db, session_factory, admin, collaborator):
    """Two tabs: the second completion waits for the first one's, then stores and reports the completed path."""
    from app.api.routes import learn

    admin_headers = auth_headers(client, admin.email)
    (one, (first,)), (two, (second,)) = (_course(client, admin_headers, collaborator, quiz=False) for _ in range(2))
    path = _path_with(client, admin_headers, [one["id"], two["id"]], status="published", settings={"sequential": False})
    client.post(f"/api/v1/paths/{path['id']}/people", headers=admin_headers, json={"user_ids": [collaborator.id]})
    paused, release = threading.Event(), threading.Event()
    results = {}

    def complete(module_id: int):
        def run(session):
            results[module_id] = learn.complete_module(module_id, db=session, user=session.get(User, collaborator.id))

        return run

    errors = _race(session_factory, _paused_before_commit(complete(first), paused, release), complete(second),
                   paused=paused, release=release)

    assert not errors, errors
    assert _count(db, "SELECT count(*) FROM path_assignments WHERE completed_at IS NOT NULL") == 1
    completed = results[second].path_completed
    assert (completed.id, completed.title) == (path["id"], "Ruta")
    assert completed.certificate is not None  # issued once, by whichever completion stored the path's
    assert _count(db, "SELECT count(*) FROM learner_certificates WHERE kind = 'path'") == 1


@pytest.mark.parametrize("change", ["profile", "path"])
def test_a_course_completed_during_a_sync_never_deadlocks(
    client, db, session_factory, admin, collaborator, monkeypatch, change
):
    """A sync writes enrollments before it locks assignments, the order of a learner completing a course (its
    enrollment, then their assignments): one of the two waits, never both. The path completed meanwhile stays."""
    from app.api.routes import learn
    from app.api.routes import paths as path_routes
    from app.api.routes import users as user_routes
    from app.schemas.paths import PathSettings, PathUpdate
    from app.schemas.users import UserUpdate
    from app.services import paths

    admin_headers = auth_headers(client, admin.email)
    course, (module_id,) = _course(client, admin_headers, collaborator, quiz=False)
    advisor, manager = (
        client.post("/api/v1/profiles", headers=admin_headers, json={"name": name}).json() for name in ("A", "B")
    )
    client.patch(f"/api/v1/users/{collaborator.id}", headers=admin_headers, json={"profile_id": advisor["id"]})
    path = _path_with(client, admin_headers, [course["id"]], status="published", profile_ids=[advisor["id"]])
    _path_with(client, admin_headers, [course["id"]], status="published", profile_ids=[manager["id"]])
    if change == "profile":  # loses the path, gets the other one: the same course in both
        update = UserUpdate(profile_id=manager["id"])
        sync = lambda s: user_routes.update_user(collaborator.id, update, db=s, admin=s.get(User, admin.id))  # noqa: E731
    else:  # one more profile and a deadline, saved at once
        update = PathUpdate(profile_ids=[advisor["id"], manager["id"]], settings=PathSettings(due_days=10))
        sync = lambda s: path_routes.update_path(path["id"], update, db=s, admin=s.get(User, admin.id))  # noqa: E731
    paused, release = threading.Event(), threading.Event()
    ensure_enrollments = paths.ensure_enrollments

    def slow_ensure_enrollments(*args, **kwargs):
        paused.set()
        release.wait(5)
        ensure_enrollments(*args, **kwargs)

    monkeypatch.setattr(paths, "ensure_enrollments", slow_ensure_enrollments)

    errors = _race(
        session_factory,
        sync,
        lambda s: learn.complete_module(module_id, db=s, user=s.get(User, collaborator.id)),
        paused=paused,
        release=release,
    )

    assert not errors, errors  # DeadlockDetected, locking the assignment before the enrollment
    assert _count(db, f"SELECT count(*) FROM path_assignments WHERE path_id = {path['id']} "
                      "AND completed_at IS NOT NULL") == 1


def test_a_path_completed_while_an_admin_changes_it_never_deadlocks(
    client, db, session_factory, admin, collaborator, monkeypatch
):
    """The learner's completion holds their assignment, then writes the path's certificate (a foreign key check on the
    path's row). The admin's change holds the path's row, then updates its assignments. With the path locked FOR
    UPDATE each waits for the other; NO KEY UPDATE lets the certificate's check through."""
    from app.api.routes import learn
    from app.api.routes import paths as path_routes
    from app.schemas.paths import PathSettings, PathUpdate
    from app.services import certificates

    admin_headers = auth_headers(client, admin.email)
    course, (module_id,) = _course(client, admin_headers, collaborator, quiz=False)
    path = _path_with(client, admin_headers, [course["id"]], status="published", settings={"due_days": 30})
    client.post(f"/api/v1/paths/{path['id']}/people", headers=admin_headers, json={"user_ids": [collaborator.id]})
    paused, release = threading.Event(), threading.Event()
    issue = certificates.issue_for_assignments

    def slow_issue(session, assignment_ids):
        if assignment_ids and not paused.is_set():
            paused.set()  # the learner holds their assignment, completed
            release.wait(5)
        return issue(session, assignment_ids)

    monkeypatch.setattr(certificates, "issue_for_assignments", slow_issue)

    def complete(session):
        learn.complete_module(module_id, db=session, user=session.get(User, collaborator.id))

    def drop_deadline(session):
        path_routes.update_path(path["id"], PathUpdate(settings=PathSettings(due_days=None)), db=session,
                                admin=session.get(User, admin.id))

    errors = _race(session_factory, complete, drop_deadline, paused=paused, release=release)

    assert not errors, errors  # DeadlockDetected, with the path locked FOR UPDATE
    assert _count(db, "SELECT count(*) FROM learner_certificates WHERE kind = 'path'") == 1
    assert _count(db, "SELECT count(*) FROM path_assignments WHERE completed_at IS NOT NULL") == 1
