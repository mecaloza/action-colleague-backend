"""Job queue: claiming with leases, retries, rescheduling and recovery from dead workers."""

from datetime import datetime, timedelta, timezone

import pytest

from app.db.models import Job
from app.worker import queue, runner


@pytest.fixture
def jobs_db(session_factory, monkeypatch):
    from app.db import session as db_session

    monkeypatch.setattr(db_session, "SessionLocal", session_factory)
    return session_factory


@pytest.fixture
def registry(monkeypatch):
    """Isolated handler registry for each test."""
    monkeypatch.setattr(runner, "_handlers", {})
    monkeypatch.setattr(runner, "_failure_hooks", {})
    return runner


def _utc(value: datetime) -> datetime:
    """SQLite returns naive datetimes (stored in UTC); Postgres returns aware ones."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _job(db_factory, job_id: str) -> Job:
    with db_factory() as db:
        return db.get(Job, job_id)


def test_enqueue_deduplicates_active_jobs(db):
    first = queue.enqueue(db, "demo", {"n": 1}, dedupe_key="same")
    second = queue.enqueue(db, "demo", {"n": 2}, dedupe_key="same")
    assert first.id == second.id
    assert db.query(Job).count() == 1


def test_claim_takes_due_jobs_only_once(db):
    due = queue.enqueue(db, "demo")
    queue.enqueue(db, "demo", delay_seconds=3600)

    claimed = queue.claim(db, "w1", 60)
    assert claimed.id == due.id and claimed.status == "running" and claimed.locked_by == "w1"
    assert queue.claim(db, "w2", 60) is None


def test_successful_job_stores_its_result(jobs_db, registry, db):
    registry.handler("demo")(lambda ctx: {"echo": ctx.payload["value"]})
    job = queue.enqueue(db, "demo", {"value": 7})

    assert runner.run_pending() == 1
    stored = _job(jobs_db, job.id)
    assert stored.status == "succeeded" and stored.result == {"echo": 7} and stored.progress == 100


def test_failures_retry_with_backoff_then_fail_and_run_the_hook(jobs_db, registry, db):
    failures = []

    def boom(ctx):
        raise runner.JobError("No se pudo")

    registry.handler("demo", on_failure=lambda session, job, error: failures.append((job.id, error)))(boom)
    job = queue.enqueue(db, "demo", max_attempts=2)

    runner.run_pending()
    first = _job(jobs_db, job.id)
    assert first.status == "queued" and first.attempts == 1 and first.error == "No se pudo"
    assert _utc(first.run_after) > queue.utcnow() + timedelta(seconds=20)  # backoff

    with jobs_db() as session:
        session.get(Job, job.id).run_after = queue.utcnow()
        session.commit()
    runner.run_pending()
    final = _job(jobs_db, job.id)
    assert final.status == "failed" and final.attempts == 2
    assert failures == [(job.id, "No se pudo")]


def test_unexpected_errors_show_a_generic_message(jobs_db, registry, db):
    def boom(ctx):
        raise KeyError("internal detail")

    registry.handler("demo")(boom)
    job = queue.enqueue(db, "demo", max_attempts=1)
    runner.run_pending()
    assert _job(jobs_db, job.id).error == runner.GENERIC_ERROR


def test_reschedule_does_not_count_an_attempt(jobs_db, registry, db):
    def wait(ctx):
        ctx.state["polls"] = ctx.state.get("polls", 0) + 1
        return runner.Reschedule(30)

    registry.handler("demo")(wait)
    job = queue.enqueue(db, "demo")
    runner.run_pending()

    stored = _job(jobs_db, job.id)
    assert stored.status == "queued" and stored.attempts == 0 and stored.state == {"polls": 1}
    assert _utc(stored.run_after) > queue.utcnow()


def test_progress_is_saved_and_a_lost_lease_stops_the_handler(jobs_db, registry, db):
    seen = {}

    def work(ctx):
        ctx.progress(40, "A mitad")
        seen["progress"] = _job(jobs_db, ctx.job_id).progress
        with jobs_db() as session:  # another worker took the job over
            session.get(Job, ctx.job_id).locked_by = "someone-else"
            session.commit()
        ctx.progress(60, "Nunca llega")
        return {"done": True}

    registry.handler("demo")(work)
    job = queue.enqueue(db, "demo")
    runner.run_pending()

    assert seen["progress"] == 40
    stored = _job(jobs_db, job.id)
    assert stored.status == "running" and stored.locked_by == "someone-else"  # untouched by the old worker


def test_jobs_of_dead_workers_are_retried_and_eventually_failed(jobs_db, registry, db):
    failures = []
    registry.handler("demo", on_failure=lambda session, job, error: failures.append(error))(lambda ctx: {})
    job = queue.enqueue(db, "demo", max_attempts=2)
    for _ in range(2):
        with jobs_db() as session:
            claimed = queue.claim(session, "dead-worker", 60)
            claimed.locked_until = queue.utcnow() - timedelta(seconds=1)  # the worker stopped renewing
            session.commit()
        assert runner.reap_expired_jobs() == 1
        with jobs_db() as session:
            session.get(Job, job.id).run_after = queue.utcnow()
            session.commit()

    stored = _job(jobs_db, job.id)
    assert stored.status == "failed" and stored.attempts == 2
    assert failures == [queue.LOST_WORKER_ERROR]


def test_canceled_jobs_are_not_claimed(db):
    job = queue.enqueue(db, "demo")
    queue.cancel(db, job)
    assert queue.claim(db, "w1", 60) is None


def test_concurrent_enqueues_with_the_same_key_create_one_job(db, session_factory, monkeypatch):
    # The other process wins the race after our check but before our insert.
    real_lookup = queue._active_with_key
    calls = []

    def lookup_after_race(session, key):
        calls.append(key)
        if len(calls) == 1:
            with session_factory() as other:
                queue.enqueue(other, "demo", dedupe_key=key)
            return None
        return real_lookup(session, key)

    monkeypatch.setattr(queue, "_active_with_key", lookup_after_race)
    job = queue.enqueue(db, "demo", dedupe_key="same")

    assert db.query(Job).count() == 1 and job.dedupe_key == "same"
