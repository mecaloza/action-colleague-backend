"""On a real PostgreSQL (foreign keys and row locks): deletes, concurrent submissions and query budgets."""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import text

from app.db.models import Enrollment, Evaluation, EvaluationAttempt, ModuleProgress, User
from tests.conftest import QueryCounter, auth_headers

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
