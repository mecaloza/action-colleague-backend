"""The job queue on a real PostgreSQL: SKIP LOCKED claims, leases, reaper and overlapping deploys."""

import threading
import time
from collections import Counter
from datetime import timedelta

import pytest
from sqlalchemy import text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.db.models import Course, Job, MediaAsset, Module
from app.services import media as media_service
from app.worker import queue, runner


@pytest.fixture
def pg(pg_engine, monkeypatch):
    """Session factory on a migrated database, used by the worker too; only test job types are known."""
    from app.db import session as db_session

    pg_engine.pool._pool.maxsize = 30  # many threads at once
    factory = sessionmaker(bind=pg_engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(db_session, "SessionLocal", factory)
    monkeypatch.setattr(runner, "_handlers", {})
    monkeypatch.setattr(runner, "_failure_hooks", {})
    return factory


def _get(factory, job_id: str) -> Job:
    with factory() as db:
        return db.get(Job, job_id)


def _run_threads(target, count: int) -> None:
    threads = [threading.Thread(target=target, args=(index,)) for index in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


def test_concurrent_claims_hand_out_each_job_once(pg):
    with pg() as db:
        ids = [queue.enqueue(db, "demo").id for _ in range(200)]
    claimed, lock, barrier = [], threading.Lock(), threading.Barrier(12)

    def claimer(index: int) -> None:
        barrier.wait()
        while True:
            with pg() as db:
                job = queue.claim(db, f"w{index}", 60)
            if job is None:
                return
            with lock:
                claimed.append(job.id)

    _run_threads(claimer, 12)

    counts = Counter(claimed)
    assert set(counts) == set(ids) and max(counts.values()) == 1


def test_the_heartbeat_keeps_a_long_job_alive_while_the_reaper_runs(pg, monkeypatch):
    monkeypatch.setattr(runner, "LEASE_SECONDS", 1.5)  # a heartbeat every 0.5 s
    runs = []

    def slow(ctx):
        runs.append(ctx.worker_id)
        time.sleep(4)  # more than two leases without progress() (one long FFmpeg run)
        return {"ok": True}

    runner.handler("demo")(slow)
    with pg() as db:
        job = queue.enqueue(db, "demo")
    stop = threading.Event()

    def reaper() -> None:
        while not stop.is_set():
            runner.reap_expired_jobs()
            time.sleep(0.1)

    reaping = threading.Thread(target=reaper)
    reaping.start()
    runner.run_pending(worker_id="w1")
    stop.set()
    reaping.join()

    stored = _get(pg, job.id)
    assert stored.status == "succeeded" and stored.attempts == 0 and runs == ["w1"]


def test_the_reaper_leaves_a_job_whose_lease_was_renewed_in_time(pg, monkeypatch):
    """The reaper's first commit releases the batch's row locks; a late heartbeat must still win."""
    with pg() as db:
        first, second = queue.enqueue(db, "demo").id, queue.enqueue(db, "demo").id
    owners = {}
    for worker in ("wa", "wb"):
        with pg() as db:
            owners[queue.claim(db, worker, 60).id] = worker
    with pg() as db:
        db.execute(update(Job).values(locked_until=queue.utcnow() - timedelta(seconds=5)))
        db.commit()
    real_fail, renewed = queue.fail, []

    def fail_then_the_other_worker_beats(db, job, worker_id, *args, **kwargs):
        result = real_fail(db, job, worker_id, *args, **kwargs)
        if not renewed:
            other = second if job.id == first else first
            with pg() as late:
                renewed.append((other, queue.extend_lease(late, other, owners[other], 120)))
        return result

    monkeypatch.setattr(queue, "fail", fail_then_the_other_worker_beats)
    runner.reap_expired_jobs()

    [(other, extended)] = renewed
    stored = _get(pg, other)
    assert extended and stored.status == "running" and stored.locked_by == owners[other]


def test_concurrent_enqueues_with_one_dedupe_key_make_one_job(pg):
    barrier, ids, errors = threading.Barrier(10), [], []

    def enqueuer(_: int) -> None:
        barrier.wait()
        try:
            with pg() as db:
                job = queue.enqueue(db, "demo", dedupe_key="media:x", commit=False)
                time.sleep(0.05)
                db.commit()
                ids.append(job.id)
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(repr(exc))

    _run_threads(enqueuer, 10)

    with pg() as db:
        total = db.execute(text("SELECT count(*) FROM jobs")).scalar()
    assert errors == [] and total == 1 and len(set(ids)) == 1


def test_two_pools_like_an_overlapping_deploy_run_each_job_once(pg, monkeypatch):
    monkeypatch.setattr(runner, "POLL_SECONDS", 0.05)
    executed, lock = [], threading.Lock()

    def work(ctx):
        with lock:
            executed.append(ctx.job_id)
        time.sleep(0.02)
        return {}

    runner.handler("demo")(work)
    with pg() as db:
        ids = [queue.enqueue(db, "demo").id for _ in range(60)]
    old, new = runner.WorkerPool(3), runner.WorkerPool(3)
    new.worker_prefix = "new-container:1"
    for pool in (old, new):
        for index in range(pool.concurrency):
            thread = threading.Thread(target=pool._loop, args=(index,), daemon=True)
            thread.start()
            pool.threads.append(thread)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        with pg() as db:
            if db.query(Job).filter(Job.status != "succeeded").count() == 0:
                break
        time.sleep(0.1)
    old.stop()
    new.stop()

    counts = Counter(executed)
    assert set(counts) == set(ids) and max(counts.values()) == 1


def test_a_worker_that_dies_costs_the_job_an_attempt(pg):
    """So a job that kills its own process can't loop forever (a clean shutdown hands jobs back instead)."""
    with pg() as db:
        job = queue.enqueue(db, "demo", max_attempts=3)
    for crash in range(3):
        with pg() as db:
            claimed = queue.claim(db, f"container-{crash}:1:0", 120)
            claimed.locked_until = queue.utcnow() - timedelta(seconds=1)
            db.commit()
        runner.reap_expired_jobs()
        with pg() as db:
            db.get(Job, job.id).run_after = queue.utcnow()
            db.commit()

    stored = _get(pg, job.id)
    assert stored.status == "failed" and stored.attempts == 3


def test_a_stale_failure_does_not_run_the_failure_hook_again(pg):
    hooks = []
    runner.handler("demo", on_failure=lambda session, job, error: hooks.append(error))(lambda ctx: {})
    with pg() as db:
        job_id = queue.enqueue(db, "demo", max_attempts=1).id
    with pg() as db:
        queue.claim(db, "w1", 60)
    with pg() as db:  # the worker records the real, final failure
        runner._fail_job(db, db.get(Job, job_id), "w1", "Error real", {}, {})
    with pg() as db:  # a reaper that loaded the job before that processes it now
        runner._fail_job(db, db.get(Job, job_id), "w1", queue.LOST_WORKER_ERROR, {}, {}, lease_expired=True)

    assert hooks == ["Error real"]


def _asset(db, course_id: int | None, path: str, kind: str = "video", status: str = "ready") -> str:
    asset = MediaAsset(kind=kind, status=status, bucket="course-media", path=path, mime_type="", course_id=course_id)
    db.add(asset)
    db.flush()
    return asset.id


def test_two_uploads_for_one_module_finishing_together_leave_no_orphan(pg, storage, monkeypatch):
    """The module row is locked while attaching: the later one sees, and discards, what the first attached."""
    from app.worker.jobs import media as media_jobs

    with pg() as db:
        course = Course(title="C", status="draft", source="manual", settings={})
        db.add(course)
        db.flush()
        module = Module(course_id=course.id, title="M", order=1, content_text="", source="text")
        db.add(module)
        db.flush()
        folder = f"courses/{course.id}"
        uploads = {name: _asset(db, course.id, f"{folder}/{name}/clase.mp4", status="processing") for name in "ab"}
        posters = {name: _asset(db, course.id, f"{folder}/{name}/derived/poster.jpg", kind="image") for name in "ab"}
        db.commit()
        course_id, module_id = course.id, module.id
    contexts = {}
    for name, asset_id in uploads.items():
        with pg() as db:
            payload = {"asset_id": asset_id, "purpose": "module_video", "module_id": module_id}
            queue.enqueue(db, "media.process", payload)
            job = queue.claim(db, f"worker-{name}", 60)
            contexts[name] = runner.JobContext.from_job(job, f"worker-{name}")

    def finish(name: str) -> None:
        path = f"courses/{course_id}/{name}/clase.mp4"
        snapshot = media_jobs.AssetSnapshot("video", path, "video/mp4", None, course_id, "clase.mp4")
        media_jobs._finish_processing(contexts[name], snapshot, {"meta": {"poster_asset_id": posters[name]}})

    attach, attaching = media_jobs._attach, threading.Event()

    def slow_attach(db, asset, payload):
        replaced = attach(db, asset, payload)
        if asset.id == uploads["a"]:
            attaching.set()
            time.sleep(0.5)  # "b" finishes meanwhile
        return replaced

    monkeypatch.setattr(media_jobs, "_attach", slow_attach)
    first = threading.Thread(target=finish, args=("a",))
    first.start()
    assert attaching.wait(5)
    finish("b")
    first.join()

    with pg() as db:
        assert db.get(Module, module_id).video_asset_id == uploads["b"]
        assert {asset.id for asset in db.query(MediaAsset)} == {uploads["b"], posters["b"]}


def test_a_cover_set_while_its_image_is_discarded_is_never_lost_silently(pg, storage, monkeypatch):
    """The rows are locked before the check: a link made meanwhile fails loudly instead of being SET NULL."""
    with pg() as db:
        image = _asset(db, None, "library/x/derived/image.jpg", kind="image")
        courses = [Course(title=title, status="draft", source="manual", settings={}) for title in ("X", "Y")]
        db.add_all(courses)
        db.commit()
        other = courses[1].id
    referenced, errors = media_service._referenced, []

    def link_it_to_another_course() -> None:
        try:
            with pg() as db:
                db.execute(update(Course).where(Course.id == other).values(cover_asset_id=image))
                db.commit()
        except IntegrityError as exc:
            errors.append(exc)

    def referenced_then_race(db, asset_ids):
        used = referenced(db, asset_ids)
        racer = threading.Thread(target=link_it_to_another_course)
        racer.start()
        racer.join(1)  # it waits for our lock (or, without one, commits right away)
        referenced_then_race.racer = racer
        return used

    monkeypatch.setattr(media_service, "_referenced", referenced_then_race)
    with pg() as db:
        media_service.discard_assets(db, [image])
    referenced_then_race.racer.join()

    with pg() as db:
        cover = db.get(Course, other).cover_asset_id
        exists = db.get(MediaAsset, image) is not None
    assert (cover == image and exists) or (cover is None and errors)
