"""Recordings and the previous app's media when uploads, admins and deploys don't cooperate."""

import json
import shutil
import subprocess
from datetime import date
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
    asset = MediaAsset(kind=kind, status=status, bucket="course-media", path=f"x/{kind}-{status}.webm",
                       mime_type="video/webm", course_id=course_id, **fields)
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
