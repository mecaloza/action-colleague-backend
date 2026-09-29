"""
Postgres-backed job queue.

Jobs are claimed with a lease (`locked_until`) that the running worker keeps extending. A job is
only taken back when its lease expired — never just because a process started — so overlapping
deploys (Railway runs the old and new containers side by side) can't run it twice. Claiming is
a single UPDATE ... WHERE id = (SELECT ... FOR UPDATE SKIP LOCKED) statement, which works
through Supabase's transaction pooler.
"""

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import Job

ACTIVE = ("queued", "running")
LOST_WORKER_ERROR = "El proceso que ejecutaba el trabajo se detuvo (reinicio o despliegue)."
BACKOFF_BASE_SECONDS = 30  # wait after the first failed attempt; it doubles with each one
BACKOFF_MAX_SECONDS = 600


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _from_now(seconds: float) -> datetime:
    return utcnow() + timedelta(seconds=seconds)


def enqueue(
    db: Session,
    job_type: str,
    payload: dict[str, Any] | None = None,
    *,
    course_id: int | None = None,
    module_id: int | None = None,
    created_by: int | None = None,
    dedupe_key: str | None = None,
    delay_seconds: float = 0,
    max_attempts: int = 3,
    commit: bool = True,
) -> Job:
    """Create a job, or return the active one with the same `dedupe_key`."""
    if dedupe_key:
        existing = active_with_key(db, dedupe_key)
        if existing:
            return existing
    job = Job(
        type=job_type,
        status="queued",
        payload=payload or {},
        state={},
        course_id=course_id,
        module_id=module_id,
        created_by=created_by,
        dedupe_key=dedupe_key,
        max_attempts=max_attempts,
        run_after=_from_now(delay_seconds),
    )
    try:
        with db.begin_nested():  # a unique index allows one active job per dedupe key
            db.add(job)
    except IntegrityError:
        existing = active_with_key(db, dedupe_key) if dedupe_key else None
        if existing is None:
            raise
        return existing  # another process enqueued the same work first
    if commit:
        db.commit()
    return job


def active_with_key(db: Session, dedupe_key: str) -> Job | None:
    return db.query(Job).filter(Job.dedupe_key == dedupe_key, Job.status.in_(ACTIVE)).first()


def claim(db: Session, worker_id: str, lease_seconds: int, job_types: tuple[str, ...] | None = None) -> Job | None:
    """Take the next due job; with `job_types`, only those this process can run (overlapping deploys)."""
    now = utcnow()
    conditions = [Job.status == "queued", Job.run_after <= now]
    if job_types is not None:
        conditions.append(Job.type.in_(job_types))
    candidate = (
        select(Job.id)
        .where(*conditions)
        .order_by(Job.run_after, Job.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )
    job_id = db.execute(
        update(Job)
        .where(Job.id == candidate, Job.status == "queued")
        .values(
            status="running",
            locked_by=worker_id,
            locked_until=now + timedelta(seconds=lease_seconds),
            started_at=now,
            updated_at=now,
        )
        .returning(Job.id)
        .execution_options(synchronize_session=False)
    ).scalar_one_or_none()
    db.commit()
    # populate_existing: the session may already hold this job with its pre-claim values.
    return db.get(Job, job_id, populate_existing=True) if job_id else None


def _update_job(db: Session, *conditions, **values) -> bool:
    """UPDATE the jobs that match `conditions` and commit. True when exactly one row changed.

    The conditions are what makes every write safe: a worker only touches a job while it still holds it.
    """
    result = db.execute(
        update(Job)
        .where(*conditions)
        .values(updated_at=utcnow(), **values)
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return result.rowcount == 1


def extend_lease(db: Session, job_id: str, worker_id: str, lease_seconds: int, **fields) -> bool:
    """Keep the lease alive (and optionally store progress). False if the job is no longer ours."""
    return _update_job(
        db,
        Job.id == job_id, Job.locked_by == worker_id, Job.status == "running",
        locked_until=_from_now(lease_seconds), **fields,
    )


def _release(db: Session, job_id: str, worker_id: str | None, *conditions, **fields) -> bool:
    """Give up the lease and store the job's new `fields`. False if `worker_id` no longer holds the job."""
    return _update_job(
        db, Job.id == job_id, Job.locked_by == worker_id, *conditions, locked_by=None, locked_until=None, **fields
    )


def succeed(db: Session, job_id: str, worker_id: str, result: dict | None = None) -> bool:
    return _release(
        db, job_id, worker_id, status="succeeded", result=result or {}, progress=100, finished_at=utcnow(), error=None
    )


def reschedule(db: Session, job_id: str, worker_id: str, delay_seconds: float, state: dict) -> bool:
    """Put the job back in the queue without counting a failed attempt (e.g. waiting on HeyGen)."""
    return _release(db, job_id, worker_id, status="queued", state=state, run_after=_from_now(delay_seconds))


def requeue(db: Session, job_id: str, worker_id: str) -> bool:
    """Hand a running job back at once without counting an attempt (the process is shutting down)."""
    return _release(db, job_id, worker_id, status="queued", run_after=utcnow())


def _backoff_seconds(attempts: int) -> int:
    """Wait before retrying after `attempts` failures: 30 s, 60 s, 120 s... up to 10 minutes."""
    return min(BACKOFF_BASE_SECONDS * 2 ** (attempts - 1), BACKOFF_MAX_SECONDS)


def fail(
    db: Session, job: Job, worker_id: str | None, error: str, state: dict | None,
    permanent: bool = False, lease_expired: bool = False,
) -> bool:
    """Count a failed attempt: retry with exponential backoff until `max_attempts`, then fail.

    `permanent` failures skip the remaining attempts.
    """
    attempts = max(job.max_attempts or 1, (job.attempts or 0) + 1) if permanent else (job.attempts or 0) + 1
    outcome = {"attempts": attempts, "error": error, "state": state or {}}
    # The reaper only takes jobs whose lease is still expired: a late heartbeat wins over it.
    guard = (Job.status == "running", Job.locked_until < utcnow()) if lease_expired else ()
    if attempts < (job.max_attempts or 1):
        return _release(
            db, job.id, worker_id, *guard, status="queued", run_after=_from_now(_backoff_seconds(attempts)), **outcome
        )
    return _release(db, job.id, worker_id, *guard, status="failed", finished_at=utcnow(), **outcome)


def expired_jobs(db: Session) -> list[Job]:
    """Running jobs whose worker stopped renewing the lease (the process died or was replaced)."""
    return (
        db.query(Job)
        .filter(Job.status == "running", Job.locked_until < utcnow())
        .with_for_update(skip_locked=True)
        .all()
    )


def cancel(db: Session, job: Job) -> None:
    """Stop a queued job from running (a running one notices on its next progress update)."""
    job.status = "canceled"
    job.locked_by = None
    job.locked_until = None
    job.finished_at = utcnow()
    db.commit()
