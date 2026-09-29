"""Recordings and the previous app's media when uploads, admins and deploys don't cooperate."""

import json
import shutil
import subprocess
from datetime import date, timedelta, timezone
from pathlib import Path

import pytest

from app.core.config import get_settings
from app.db.models import Course, Evaluation, Job, MediaAsset, Module
from app.worker import queue
from app.worker.jobs import recording
from tests.conftest import auth_headers
from tests.test_video import _run_until_done

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is not installed")

PUBLIC_URL = "https://proj.supabase.co/storage/v1/object/public/course-videos/modules/9/abc.mp4"


@pytest.fixture
def admin_headers(client, admin):
    return auth_headers(client, admin.email)


@pytest.fixture
def course(db, admin):
    course = Course(title="Grabado", status="draft", source="manual", settings={}, created_by=admin.id)
    db.add(course)
    db.commit()
    return course


@pytest.fixture
def module(db, course):
    module = Module(course_id=course.id, title="Mensaje", order=1, source="recording")
    db.add(module)
    db.commit()
    return module


def _asset(db, course_id: int | None, kind: str = "recording", status: str = "ready", **fields) -> MediaAsset:
    fields.setdefault("path", f"x/{kind}-{status}.webm")
    asset = MediaAsset(kind=kind, status=status, bucket="course-media", mime_type="video/webm", course_id=course_id, **fields)
    db.add(asset)
    db.commit()
    return asset


def _fast_forward(db) -> None:
    for job in db.query(Job).filter(Job.status == "queued").all():
        job.run_after = queue.utcnow()
    db.commit()


def _mp4(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=25",
                    "-f", "lavfi", "-i", "sine", "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    str(path)], check=True, capture_output=True)
    return path


# ── The recording route ───────────────────────────────────────────────


def test_a_recording_must_be_an_uploaded_file_of_the_course(client, db, admin_headers, module, admin):
    other = Course(title="Otro", status="draft", source="manual", settings={}, created_by=admin.id)
    db.add(other)
    db.commit()
    url = f"/api/v1/modules/{module.id}/recording"
    foreign = _asset(db, other.id)
    assert client.post(url, headers=admin_headers, json={"recording_asset_id": foreign.id}).status_code == 422
    deck = _asset(db, module.course_id, kind="deck")
    assert client.post(url, headers=admin_headers, json={"recording_asset_id": deck.id}).status_code == 422
    for status in ("pending", "failed"):
        asset = _asset(db, module.course_id, status=status)
        response = client.post(url, headers=admin_headers, json={"recording_asset_id": asset.id})
        assert response.status_code == 409 and "no terminó de subirse" in response.json()["detail"]
    assert db.query(Job).count() == 0


def test_another_take_while_one_is_combined_is_a_conflict(client, db, admin_headers, module):
    first, second = _asset(db, module.course_id), _asset(db, module.course_id, status="processing")
    url = f"/api/v1/modules/{module.id}/recording"
    job = client.post(url, headers=admin_headers, json={"recording_asset_id": first.id})
    assert job.status_code == 202
    again = client.post(url, headers=admin_headers, json={"recording_asset_id": first.id})
    assert again.status_code == 202 and again.json()["id"] == job.json()["id"]  # the same request: the same job
    other = client.post(url, headers=admin_headers, json={"recording_asset_id": second.id})
    assert other.status_code == 409  # never silently the previous take


def test_a_recording_waits_only_so_long_for_its_upload(client, db, admin_headers, module, worker, monkeypatch):
    monkeypatch.setattr(recording, "MAX_PROCESSING_WAIT_SECONDS", 15)
    camera = _asset(db, module.course_id, status="processing")  # e.g. a processing job that never finishes
    client.post(f"/api/v1/modules/{module.id}/recording", headers=admin_headers, json={"recording_asset_id": camera.id})
    for _ in range(3):
        worker()
        _fast_forward(db)
    db.expire_all()
    job = db.query(Job).filter(Job.type == "video.compose_recording").one()
    assert job.status == "failed" and "tardó demasiado" in job.error
    assert db.get(Module, module.id).generation_status == "failed"


def test_captions_are_made_for_each_module_showing_a_video(db, course, admin):
    video = _asset(db, course.id, kind="video")
    modules = [Module(course_id=course.id, title=f"M{i}", order=i, source="upload", video_asset_id=video.id) for i in (1, 2)]
    db.add_all(modules)
    db.commit()
    for module in modules:
        recording.enqueue_transcription(db, module, commit=True)
    assert db.query(Job).filter(Job.type == "media.transcribe").count() == 2


def test_silent_videos_get_no_captions_job(db, course):
    video = _asset(db, course.id, kind="video", meta={"has_audio": False})
    module = Module(course_id=course.id, title="Mudo", order=1, source="upload", video_asset_id=video.id)
    db.add(module)
    db.commit()
    recording.enqueue_captions(db, module)
    db.commit()
    assert db.query(Job).filter(Job.type == "media.transcribe").count() == 0


# ── The previous app's media ──────────────────────────────────────────


@pytest.fixture
def legacy(monkeypatch, storage):
    monkeypatch.setattr(get_settings(), "supabase_url", "https://proj.supabase.co")
    _mp4(storage.root / "course-videos" / "modules" / "9" / "abc.mp4")


def _migrate(db, worker, passes: int = 1) -> Job:
    queue.enqueue(db, "legacy.migrate", dedupe_key="legacy-migrate")
    for _ in range(passes):
        worker(max_jobs=1)
        _fast_forward(db)
    return db.query(Job).filter(Job.type == "legacy.migrate").one()


def test_a_migrated_video_never_replaces_a_newer_one(db, course, legacy, storage, worker):
    module = Module(course_id=course.id, title="Viejo", order=1, source="ai", video_url=PUBLIC_URL)
    db.add(module)
    db.commit()
    _migrate(db, worker)  # copies the file and queues its processing (not run yet)

    newer = _asset(db, course.id, kind="video")  # meanwhile the admin gives the module a new video
    stored = db.get(Module, module.id)
    stored.video_asset_id, stored.video_url, stored.source = newer.id, "", "upload"
    db.commit()
    _run_until_done(db, worker)

    db.expire_all()
    assert db.get(Module, module.id).video_asset_id == newer.id
    assert db.query(MediaAsset).filter(MediaAsset.path.like("%/legacy/%")).count() == 0  # the copy was dropped


def test_running_the_migration_again_copies_each_video_once(db, course, legacy, storage, worker):
    module = Module(course_id=course.id, title="Viejo", order=1, source="ai", video_url=PUBLIC_URL)
    db.add(module)
    db.commit()
    queue.enqueue(db, "legacy.migrate", dedupe_key="legacy-migrate")
    worker(max_jobs=1)
    # A deploy runs another pass before the copy was processed.
    queue.enqueue(db, "legacy.migrate", dedupe_key="legacy-migrate")
    _fast_forward(db)
    _run_until_done(db, worker)

    db.expire_all()
    stored = db.get(Module, module.id)
    assert stored.video_asset_id and stored.source == "ai" and stored.video_url == ""
    assert db.query(MediaAsset).filter(MediaAsset.kind == "video").count() == 1


def test_an_unreadable_old_evaluation_does_not_stop_the_videos(db, course, legacy, storage, worker):
    module = Module(course_id=course.id, title="Viejo", order=1, source="ai", video_url=PUBLIC_URL)
    db.add(module)
    db.flush()
    db.add(Evaluation(module_id=module.id, questions_json="{no es json"))
    other = Module(course_id=course.id, title="Otro", order=2, source="ai")
    db.add(other)
    db.flush()
    db.add(Evaluation(module_id=other.id, questions_json=json.dumps([{"type": "true_false", "statement": "A", "correct": 1}])))
    db.commit()
    job = _migrate(db, worker)
    _run_until_done(db, worker)

    db.expire_all()
    assert db.get(Job, job.id).status == "succeeded"
    assert db.get(Module, module.id).video_asset_id  # the video moved anyway
    assert db.get(Module, other.id).evaluation.spec[0]["correct"] is True


def test_lost_videos_are_marked_for_the_admin(db, course, worker, monkeypatch):
    monkeypatch.setattr(recording, "HEYGEN_RETIRED_ON", date(2026, 1, 1))
    lost = [
        Module(course_id=course.id, title="Local", order=1, source="upload", video_url="/uploads/clase.mp4"),
        Module(course_id=course.id, title="HeyGen", order=2, source="ai", video_url="heygen://video/xyz"),
    ]
    db.add_all(lost)
    db.commit()
    job = _migrate(db, worker)
    db.expire_all()
    assert db.get(Job, job.id).result["videos"] == {"lost": 2}
    for module in lost:
        stored = db.get(Module, module.id)
        assert stored.generation_status == "failed" and stored.generation_error == recording.LOST_VIDEO


def test_heygen_videos_are_copied_straight_into_private_storage(db, course, storage, worker, monkeypatch, tmp_path):
    source = _mp4(tmp_path / "heygen.mp4")
    monkeypatch.setattr(recording, "HEYGEN_RETIRED_ON", date.max)
    monkeypatch.setattr(recording.legacy_heygen, "fresh_url", lambda heygen_id: f"https://heygen.test/{heygen_id}.mp4")
    monkeypatch.setattr(recording, "_download", lambda url, dest: shutil.copyfile(source, dest))
    module = Module(course_id=course.id, title="HeyGen", order=1, source="ai", video_url="heygen://video/xyz")
    db.add(module)
    db.commit()

    job = _migrate(db, worker)
    _run_until_done(db, worker)

    db.expire_all()
    stored = db.get(Module, module.id)
    assert stored.video_asset_id and stored.source == "ai" and stored.video_url == ""
    assert db.get(MediaAsset, stored.video_asset_id).bucket == storage.bucket  # the private media bucket
    assert db.get(Job, job.id).status == "succeeded"


def test_only_the_previous_apps_buckets_are_copied(db, course, legacy, storage, worker):
    other_app = "https://proj.supabase.co/storage/v1/object/public/wendy-files/cv.pdf"
    module = Module(course_id=course.id, title="Ajeno", order=1, source="upload", video_url=other_app)
    db.add(module)
    db.commit()
    job = _migrate(db, worker)
    db.expire_all()
    assert db.get(Job, job.id).result["videos"] == {"foreign": 1}
    assert db.get(Module, module.id).video_asset_id is None


def _aware(moment):
    """SQLite gives datetimes back without their zone (they are UTC)."""
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def test_maintenance_is_scheduled_again_on_every_start(session_factory, monkeypatch):
    from app import main

    monkeypatch.setattr(main, "SessionLocal", session_factory)
    main.schedule_maintenance()
    with session_factory() as db:
        job = db.query(Job).one()
        job.run_after = queue.utcnow() + timedelta(hours=6)  # waiting for its next recheck
        db.commit()
    main.schedule_maintenance()  # a deploy: the pass runs now instead of in six hours
    with session_factory() as db:
        [job] = db.query(Job).all()
        assert _aware(job.run_after) <= queue.utcnow() + timedelta(seconds=1)


def test_a_failed_copy_is_processed_again_a_few_times_then_reported(db, course, storage, worker, monkeypatch):
    monkeypatch.setattr(recording, "HEYGEN_RETIRED_ON", date.max)
    module = Module(course_id=course.id, title="HeyGen", order=1, source="ai", video_url="heygen://video/xyz")
    db.add(module)
    db.commit()
    dest = f"courses/{course.id}/modules/{module.id}/legacy/heygen-xyz.mp4"
    _mp4(storage.file_path(dest))  # copied by an earlier pass
    copy = _asset(db, course.id, kind="video", status="failed", path=dest, error="Storage no pudo subir el archivo (503)")

    job = _migrate(db, worker)  # e.g. storage was down while it was processed: this pass tries again
    db.expire_all()
    assert db.get(MediaAsset, copy.id).meta["legacy_retries"] == 1
    assert db.get(Job, job.id).status == "queued"  # rechecks later

    db.query(Job).filter(Job.type == recording.LEGACY_VIDEO_JOB).delete()  # say its processing failed again...
    stored = db.get(MediaAsset, copy.id)
    stored.status, stored.meta = "failed", {"legacy_retries": recording.MAX_LEGACY_RETRIES}  # ...as often as allowed
    db.commit()
    queue.enqueue(db, "legacy.migrate", dedupe_key="legacy-migrate")
    _fast_forward(db)
    worker(max_jobs=1)
    db.expire_all()
    failed = db.get(Module, module.id)
    assert failed.generation_status == "failed" and "No pudimos recuperar" in failed.generation_error


def test_a_heygen_copy_made_before_the_shutdown_is_used_after_it(db, course, storage, worker, monkeypatch):
    monkeypatch.setattr(recording, "HEYGEN_RETIRED_ON", date(2026, 1, 1))
    module = Module(course_id=course.id, title="HeyGen", order=1, source="ai", video_url="heygen://video/xyz")
    db.add(module)
    db.commit()
    _mp4(storage.file_path(f"courses/{course.id}/modules/{module.id}/legacy/heygen-xyz.mp4"))

    _migrate(db, worker)
    _run_until_done(db, worker)
    db.expire_all()
    stored = db.get(Module, module.id)
    assert stored.video_asset_id and stored.generation_error is None  # not "lost": the copy was there


def test_a_module_being_worked_on_is_never_marked_lost(db, course, worker):
    module = Module(course_id=course.id, title="Local", order=1, source="upload", video_url="/uploads/clase.mp4",
                    generation_status="generating")
    db.add(module)
    db.commit()
    _migrate(db, worker)
    db.expire_all()
    stored = db.get(Module, module.id)
    assert stored.generation_status == "generating" and stored.generation_error is None


def test_slide_changes_land_on_their_exact_frame(tmp_path):
    from PIL import Image

    from app.services.video import compose
    from tests.test_recording import BLUE, ORANGE, _camera, _close

    pages = []
    for index, color in enumerate((ORANGE, BLUE)):
        page = tmp_path / f"p{index}.png"
        Image.new("RGB", (1280, 720), color).save(page)
        pages.append(page)
    output = tmp_path / "out.mp4"
    compose.compose_recording(pages, [(0, 0), (0.1, 1)], _camera(tmp_path / "cam.webm"), 1.0, output, tmp_path)

    def frame(number: int):
        dest = tmp_path / f"frame{number}.png"
        subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", str(output), "-vf", f"select=eq(n\\,{number})",
                        "-frames:v", "1", str(dest)], check=True, capture_output=True)
        return Image.open(dest).convert("RGB").getpixel((600, 540))

    # 0.1 s is frame 3 exactly: read at 25 fps, the image would only change on frame 4.
    assert _close(frame(2), ORANGE) and _close(frame(3), BLUE)


def test_deleting_a_module_drops_its_queued_legacy_copy(client, db, admin_headers, course, legacy, storage, worker):
    module = Module(course_id=course.id, title="Viejo", order=1, source="ai", video_url=PUBLIC_URL)
    db.add(module)
    db.commit()
    _migrate(db, worker)  # the copy is queued, not processed yet
    [(copy_id, copy_path)] = db.query(MediaAsset.id, MediaAsset.path).filter(MediaAsset.path.like("%/legacy/%")).all()
    assert client.delete(f"/api/v1/modules/{module.id}", headers=admin_headers).status_code == 204
    db.expire_all()
    assert db.get(MediaAsset, copy_id) is None and not storage.file_path(copy_path).exists()


def test_a_deck_another_module_recorded_with_stays(db, course, storage):
    deck, camera = _asset(db, course.id, kind="deck"), _asset(db, course.id)
    take = {"recording": {"recording_asset_id": camera.id, "deck_asset_id": deck.id, "timeline": []}}
    first = Module(course_id=course.id, title="Uno", order=1, source="recording", storyboard=take)
    second = Module(course_id=course.id, title="Dos", order=2, source="recording", storyboard=dict(take))
    db.add_all([first, second])
    db.commit()
    assert recording.drop_take(db, second, keep=set()) == []  # the first module can still combine them again


def test_a_deck_says_how_many_pages_it_has(db, course):
    from app.services.media_views import asset_out

    deck = _asset(db, course.id, kind="deck", meta={"pages": ["d/p0.png", "d/p1.png"]})
    assert asset_out(db, deck, signed={"d/p0.png": "https://signed/p0"}).page_count == 2
