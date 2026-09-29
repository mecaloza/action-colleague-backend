"""
Background worker: claims jobs and runs their handlers.

Handlers are plain functions registered with `@handler("type")`. They receive a JobContext and
either return a result dict (success), return `Reschedule(seconds)` to be called again later
without counting a failure (e.g. while HeyGen renders), or raise: the job is retried with backoff
until `max_attempts`, then marked failed and its `on_failure` hook runs. `JobError` carries a
message meant for people (shown in the UI); other exceptions show a generic message.

A heartbeat thread keeps the job's lease alive while the handler runs, so long FFmpeg renders
are never mistaken for dead workers.
"""

import logging
import os
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.db import session as db_session
from app.db.models import Job
from app.worker import queue

logger = logging.getLogger(__name__)

LEASE_SECONDS = 120
HEARTBEATS_PER_LEASE = 3  # renew the lease this many times per lease period, so a missed beat is harmless
POLL_SECONDS = 2.0
ERROR_PAUSE_POLLS = 5  # after an unexpected error a worker loop waits this many polls before trying again
REAP_EVERY_SECONDS = 60
MAX_STEP_CHARS = 160  # size of the `jobs.step` column
MAX_ERROR_CHARS = 1000  # of a failure message kept on the job
MAX_LOG_ERROR_CHARS = 300  # of an error in a worker log line
GENERIC_ERROR = "Ocurrió un error inesperado al procesar. Intenta de nuevo."


@dataclass
class Reschedule:
    seconds: float


class JobError(Exception):
    """A failure whose message can be shown to the admin as is (in Spanish).

    `permanent`: retrying can't help (e.g. a corrupt file), so the job fails at once.
    """

    def __init__(self, message: str, permanent: bool = False):
        super().__init__(message)
        self.permanent = permanent


class JobCancelled(Exception):
    """Raised inside a handler when the job lost its lease or was canceled."""


def open_session() -> Session:
    # Looked up at call time so tests can point the worker at their database.
    return db_session.SessionLocal()


@dataclass
class JobContext:
    job_id: str
    worker_id: str
    payload: dict[str, Any]
    state: dict[str, Any] = field(default_factory=dict)
    course_id: int | None = None
    module_id: int | None = None
    created_by: int | None = None

    @classmethod
    def from_job(cls, job: Job, worker_id: str) -> "JobContext":
        return cls(
            job_id=job.id,
            worker_id=worker_id,
            payload=dict(job.payload or {}),
            state=dict(job.state or {}),
            course_id=job.course_id,
            module_id=job.module_id,
            created_by=job.created_by,
        )

    def progress(self, percent: int, step: str = "") -> None:
        """Report progress (and persist `state`); raises JobCancelled if the job is no longer ours."""
        with open_session() as db:
            still_ours = queue.extend_lease(
                db, self.job_id, self.worker_id, LEASE_SECONDS,
                progress=max(0, min(100, int(percent))), step=step[:MAX_STEP_CHARS], state=self.state,
            )
        if not still_ours:
            raise JobCancelled(self.job_id)


Handler = Callable[[JobContext], dict | Reschedule | None]
FailureHook = Callable[[Session, Job, str], None]

_handlers: dict[str, Handler] = {}
_failure_hooks: dict[str, FailureHook] = {}


def handler(job_type: str, on_failure: FailureHook | None = None):
    """Register a job handler (and optionally what to do when it finally fails)."""

    def register(func: Handler) -> Handler:
        _handlers[job_type] = func
        if on_failure:
            _failure_hooks[job_type] = on_failure
        return func

    return register


def _heartbeat(job_id: str, worker_id: str, stop: threading.Event) -> None:
    while not stop.wait(LEASE_SECONDS / HEARTBEATS_PER_LEASE):
        try:
            with open_session() as db:
                queue.extend_lease(db, job_id, worker_id, LEASE_SECONDS)
        except Exception as exc:  # the next beat retries; the lease tolerates a missed one
            logger.warning("job_heartbeat_failed", extra={"job_id": job_id, "error": str(exc)[:MAX_LOG_ERROR_CHARS]})


def _log_context(job: Job) -> dict[str, Any]:
    return {"job_id": job.id, "job_type": job.type, "course_id": job.course_id, "module_id": job.module_id}


def _run_failure_hook(db: Session, job: Job, error: str, log_ctx: dict) -> None:
    hook = _failure_hooks.get(job.type)
    if job.status != "failed" or hook is None:
        return
    try:
        hook(db, job, error)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("job_failure_hook_error", extra=log_ctx)


def _fail_job(
    db: Session, job: Job, worker_id: str | None, error: str, state: dict | None, log_ctx: dict,
    permanent: bool = False, lease_expired: bool = False,
) -> None:
    """Count a failed attempt; when it was the last one, the job type's `on_failure` hook runs."""
    if not queue.fail(db, job, worker_id, error, state, permanent=permanent, lease_expired=lease_expired):
        return  # someone else owns (or already finished) the job: its hooks are not ours to run
    db.refresh(job)  # `fail` writes with plain SQL: reload to know whether the job failed for good
    _run_failure_hook(db, job, error, log_ctx)


def _call_handler(job: Job, ctx: JobContext) -> dict | Reschedule | None:
    func = _handlers.get(job.type)
    if func is None:
        raise RuntimeError(f"no handler registered for {job.type}")
    return func(ctx)


def _record_failure(job: Job, worker_id: str, exc: Exception, state: dict, log_ctx: dict) -> None:
    """A handler raised: log it and count a failed attempt (retry with backoff, or fail for good)."""
    user_facing = isinstance(exc, JobError)  # only JobError messages are meant to be read by people
    detail = str(exc)[:MAX_ERROR_CHARS]
    logger.error("job_failed", extra={**log_ctx, "error": detail}, exc_info=None if user_facing else exc)
    with open_session() as db:
        fresh = db.get(Job, job.id)
        if fresh is not None:
            permanent = user_facing and exc.permanent
            _fail_job(db, fresh, worker_id, detail if user_facing else GENERIC_ERROR, state, log_ctx, permanent)


def run_job(job: Job, worker_id: str) -> None:
    ctx = JobContext.from_job(job, worker_id)
    log_ctx = _log_context(job)
    stop = threading.Event()
    threading.Thread(target=_heartbeat, args=(job.id, worker_id, stop), daemon=True).start()
    started = time.monotonic()
    try:
        outcome = _call_handler(job, ctx)
        with open_session() as db:
            if isinstance(outcome, Reschedule):
                saved = queue.reschedule(db, job.id, worker_id, outcome.seconds, ctx.state)
                event, details = "job_rescheduled", {"delay_s": outcome.seconds}
            else:
                saved = queue.succeed(db, job.id, worker_id, outcome or {})
                event, details = "job_succeeded", {"duration_s": round(time.monotonic() - started, 1)}
            if saved:
                logger.info(event, extra={**log_ctx, **details})
            else:  # the lease was lost meanwhile (reaped, or handed back at shutdown)
                logger.warning("job_outcome_discarded", extra=log_ctx)
    except JobCancelled:
        logger.warning("job_lease_lost", extra=log_ctx)
    except Exception as exc:
        _record_failure(job, worker_id, exc, ctx.state, log_ctx)
    finally:
        stop.set()


def reap_expired_jobs() -> int:
    """Jobs whose worker died count one failed attempt (so a crashing job can't loop forever)."""
    with open_session() as db:
        expired = queue.expired_jobs(db)
        for job in expired:
            log_ctx = _log_context(job)
            logger.warning("job_lease_expired", extra=log_ctx)
            _fail_job(db, job, job.locked_by, queue.LOST_WORKER_ERROR, job.state, log_ctx, lease_expired=True)
        return len(expired)


def _claim_next(worker_id: str) -> Job | None:
    with open_session() as db:
        # Only known types: during a deploy overlap the old release must not burn the new release's jobs.
        return queue.claim(db, worker_id, LEASE_SECONDS, tuple(_handlers))


def run_pending(max_jobs: int = 100, worker_id: str = "inline") -> int:
    """Run due jobs in the current thread (tests and scripts)."""
    done = 0
    while done < max_jobs:
        job = _claim_next(worker_id)
        if job is None:
            break
        run_job(job, worker_id)
        done += 1
    return done


class WorkerPool:
    def __init__(self, concurrency: int = 2):
        self.concurrency = concurrency
        self.stop_event = threading.Event()
        self.threads: list[threading.Thread] = []
        self.worker_prefix = f"{socket.gethostname()}:{os.getpid()}"
        self._running: dict[str, str] = {}  # job id -> worker id, for the shutdown hand-back
        self._running_lock = threading.Lock()

    def _loop(self, index: int) -> None:
        worker_id = f"{self.worker_prefix}:{index}"
        is_reaper = index == 0  # one thread per process looks for the jobs of dead workers
        last_reap = 0.0
        while not self.stop_event.is_set():
            try:
                if is_reaper and time.monotonic() - last_reap > REAP_EVERY_SECONDS:
                    reap_expired_jobs()
                    last_reap = time.monotonic()
                job = _claim_next(worker_id)
                if job is None:
                    self.stop_event.wait(POLL_SECONDS)
                    continue
                with self._running_lock:
                    self._running[job.id] = worker_id
                try:
                    if self.stop_event.is_set():  # claimed while stopping, maybe after stop() looked
                        with open_session() as db:
                            queue.requeue(db, job.id, worker_id)
                        break
                    run_job(job, worker_id)
                finally:
                    with self._running_lock:
                        self._running.pop(job.id, None)
            except Exception as exc:
                logger.error(
                    "worker_loop_error", extra={"worker_id": worker_id, "error": str(exc)[:MAX_LOG_ERROR_CHARS]}
                )
                self.stop_event.wait(POLL_SECONDS * ERROR_PAUSE_POLLS)

    def start(self) -> None:
        # Import the handlers so every job type is registered before the first claim.
        import app.worker.jobs  # noqa: F401

        for index in range(self.concurrency):
            thread = threading.Thread(target=self._loop, args=(index,), name=f"worker-{index}", daemon=True)
            thread.start()
            self.threads.append(thread)
        logger.info("worker_started", extra={"concurrency": self.concurrency, "worker": self.worker_prefix})

    def stop(self) -> None:
        """
        Stop claiming, and hand the jobs still running back to the queue without counting an attempt:
        a deploy is not their fault. Their threads die with the process; until then a handler must not
        save a result it no longer holds (`media.process` checks the lease in the same transaction).
        """
        self.stop_event.set()
        with self._running_lock:
            running = dict(self._running)
        if not running:
            return
        try:
            with open_session() as db:
                for job_id, worker_id in running.items():
                    if queue.requeue(db, job_id, worker_id):
                        logger.info("job_handed_back", extra={"job_id": job_id, "worker_id": worker_id})
        except Exception:  # database unreachable while shutting down: their leases expire and the reaper retries
            logger.exception("job_hand_back_failed", extra={"jobs": len(running)})
