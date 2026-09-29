"""Recording with slides, automatic captions and the previous app's media (real FFmpeg, fake providers)."""

import json
import shutil
import subprocess
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from PIL import Image

from app.core.config import get_settings
from app.db.models import Course, Enrollment, Evaluation, Job, MediaAsset, Module, ModuleProgress
from app.services.video import compose
from app.worker import queue
from app.worker.jobs import recording
from tests.conftest import auth_headers
from tests.test_video import _frame, _probe, _run_until_done

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is not installed")

ORANGE, BLUE = (255, 76, 1), (20, 60, 200)


def _camera(path: Path, seconds: float = 3.0, size: str = "640x480") -> Path:
    subprocess.run(
        ["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i", f"testsrc=size={size}:rate=25",
         "-f", "lavfi", "-i", "sine=frequency=330", "-t", str(seconds), "-c:v", "libvpx", "-b:v", "300k",
         "-c:a", "libopus", str(path)],
        check=True, capture_output=True,
    )
    return path


def _deck(path: Path) -> Path:
    pages = [Image.new("RGB", (1280, 720), color) for color in (ORANGE, BLUE)]
    pages[0].save(path, save_all=True, append_images=pages[1:])
    return path


def _close(a, b, tolerance=40) -> bool:
    return sum(abs(x - y) for x, y in zip(a, b)) < tolerance


def test_slide_segments_follow_the_recorded_changes():
    assert compose.slide_segments([(0, 0), (2, 1), (5, 0)], 2, 7) == [(0, 2), (1, 3), (0, 2)]
    # A change shorter than a frame is absorbed; the total is always the recording's length.
    segments = compose.slide_segments([(0, 0), (2, 1), (2.01, 0)], 2, 4)
    assert sum(s for _, s in segments) == pytest.approx(4) and len(segments) == 1


def test_recording_is_composed_with_the_slides_at_the_right_times(tmp_path):
    pages = []
    for index, color in enumerate((ORANGE, BLUE)):
        page = tmp_path / f"p{index}.png"
        Image.new("RGB", (1280, 720), color).save(page)
        pages.append(page)
    camera = _camera(tmp_path / "cam.webm")
    output = tmp_path / "out.mp4"

    compose.compose_recording(pages, [(0, 0), (1.5, 1)], camera, 3.0, output, tmp_path)

    info = _probe(output)
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert (video["width"], video["height"]) == (1920, 1080)
    assert float(info["format"]["duration"]) == pytest.approx(3.0, abs=0.15)
    assert _close(_frame(output, 0.5, tmp_path / "a.png").getpixel((600, 540)), ORANGE)
    assert _close(_frame(output, 2.5, tmp_path / "b.png").getpixel((600, 540)), BLUE)


@pytest.fixture
def admin_headers(client, admin):
    return auth_headers(client, admin.email)


@pytest.fixture
def module(db, admin):
    course = Course(title="Grabado", status="draft", source="manual", settings={}, created_by=admin.id)
    db.add(course)
    db.flush()
    module = Module(course_id=course.id, title="Mensaje", order=1, source="recording")
    db.add(module)
    db.commit()
    return module


def _upload(client, headers, file: Path, mime: str, kind: str, course_id: int, **completion) -> str:
    created = client.post("/api/v1/media/uploads", headers=headers, json={
        "filename": file.name, "mime_type": mime, "size_bytes": file.stat().st_size, "kind": kind, "course_id": course_id,
    }).json()
    target = created["upload"]
    client.put(target["url"].replace("http://testserver", ""), content=file.read_bytes(), headers=target["headers"])
    done = client.post(f"/api/v1/media/{created['asset']['id']}/complete", headers=headers, json=completion)
    assert done.status_code == 200, done.text
    return created["asset"]["id"]


def test_recording_with_deck_becomes_the_modules_video_with_captions(
    client, db, admin_headers, module, storage, worker, tmp_path
):
    camera_id = _upload(client, admin_headers, _camera(tmp_path / "grabacion.webm"), "video/webm", "recording", module.course_id)
    deck_id = _upload(client, admin_headers, _deck(tmp_path / "slides.pdf"), "application/pdf", "deck", module.course_id,
                      purpose="deck")

    job = client.post(f"/api/v1/modules/{module.id}/recording", headers=admin_headers, json={
        "recording_asset_id": camera_id, "deck_asset_id": deck_id, "timeline": [{"at": 0, "slide": 0}, {"at": 1.5, "slide": 1}],
    })
    assert job.status_code == 202
    _run_until_done(db, worker)

    [detail] = client.get(f"/api/v1/courses/{module.course_id}", headers=admin_headers).json()["modules"]
    assert detail["source"] == "recording" and detail["generation_status"] == "completed"
    assert detail["video"]["width"] == 1920 and detail["captions_url"]
    assert 2.8 < detail["duration_seconds"] < 3.3
    db.expire_all()
    video = db.get(MediaAsset, db.get(Module, module.id).video_asset_id)
    assert video.meta["transcript"].startswith("Transcripción")  # used by the AI to write quizzes


def test_camera_only_recording(client, db, admin_headers, module, storage, worker, tmp_path):
    camera_id = _upload(client, admin_headers, _camera(tmp_path / "grabacion.webm", size="480x640"), "video/webm",
                        "recording", module.course_id)
    client.post(f"/api/v1/modules/{module.id}/recording", headers=admin_headers, json={"recording_asset_id": camera_id})
    _run_until_done(db, worker)

    db.expire_all()
    stored = db.get(Module, module.id)
    assert stored.video_asset_id == camera_id and stored.generation_status == "completed"
    assert db.get(MediaAsset, camera_id).width == 480  # vertical camera, never stretched


def test_uploaded_videos_get_captions_automatically(client, db, admin_headers, module, storage, worker, tmp_path):
    _upload(client, admin_headers, _camera(tmp_path / "clase.webm"), "video/webm", "video", module.course_id,
            module_id=module.id, purpose="module_video")
    _run_until_done(db, worker)
    [detail] = client.get(f"/api/v1/courses/{module.course_id}", headers=admin_headers).json()["modules"]
    assert detail["captions_url"]


def test_transcribe_requires_a_video(client, admin_headers, module):
    assert client.post(f"/api/v1/modules/{module.id}/transcribe", headers=admin_headers).status_code == 422


def test_transcribe_queues_one_captions_job_per_video(client, db, admin, admin_headers, module):
    video = MediaAsset(kind="video", status="ready", bucket="course-media", path="v/video.mp4", mime_type="video/mp4")
    db.add(video)
    db.flush()
    module.video_asset_id = video.id
    db.commit()

    first = client.post(f"/api/v1/modules/{module.id}/transcribe", headers=admin_headers)

    assert first.status_code == 202 and first.json()["type"] == "media.transcribe"
    job = db.query(Job).filter(Job.type == "media.transcribe").one()
    assert (job.dedupe_key, job.created_by, job.max_attempts) == (f"transcribe:{video.id}", admin.id, 2)
    assert client.post(f"/api/v1/modules/{module.id}/transcribe", headers=admin_headers).json()["id"] == job.id


def test_previous_app_media_is_moved_to_private_storage(client, db, admin, admin_headers, storage, worker, tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "supabase_url", "https://proj.supabase.co")
    monkeypatch.setattr(recording, "HEYGEN_RETIRED_ON", date.max)  # HeyGen's API is up: its video keeps the job waiting
    legacy_file = storage.root / "course-videos" / "modules" / "9" / "abc.mp4"
    legacy_file.parent.mkdir(parents=True)
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=25",
                    "-f", "lavfi", "-i", "sine", "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    str(legacy_file)], check=True, capture_output=True)
    course = Course(title="IA vieja", status="published", source="ai", settings={}, created_by=admin.id)
    db.add(course)
    db.flush()
    migrated = Module(course_id=course.id, title="Con video", order=1, source="ai",
                      video_url="https://proj.supabase.co/storage/v1/object/public/course-videos/modules/9/abc.mp4")
    pending = Module(course_id=course.id, title="HeyGen", order=2, source="ai", video_url="heygen://video/xyz")
    db.add_all([migrated, pending])
    db.flush()
    db.add(Evaluation(module_id=migrated.id, questions_json=json.dumps([{"type": "true_false", "statement": "A", "correct": 1}])))
    db.commit()

    queue.enqueue(db, "legacy.migrate", dedupe_key="legacy-migrate")
    worker()  # copies the video and converts the evaluation; the HeyGen one keeps waiting
    worker()  # processes the copied video like any upload

    db.expire_all()
    module = db.get(Module, migrated.id)
    assert module.video_asset_id and module.source == "ai" and module.poster_asset_id
    assert db.get(Evaluation, module.evaluation.id).spec[0]["correct"] is True
    assert db.query(Job).filter(Job.type == "legacy.migrate").one().status == "queued"  # rechecks HeyGen later
    assert legacy_file.exists()  # the public original is left untouched


def test_legacy_migration_stops_waiting_once_heygen_retires_its_api(db, admin, worker, monkeypatch):
    monkeypatch.setattr(recording, "HEYGEN_RETIRED_ON", date(2026, 1, 1))
    course = Course(title="IA vieja", status="published", source="ai", settings={}, created_by=admin.id)
    db.add(course)
    db.flush()
    db.add(Module(course_id=course.id, title="HeyGen", order=1, source="ai", video_url="heygen://video/xyz"))
    db.commit()

    queue.enqueue(db, "legacy.migrate", dedupe_key="legacy-migrate")
    worker()

    job = db.query(Job).filter(Job.type == "legacy.migrate").one()
    db.refresh(job)
    assert job.status == "succeeded" and job.result["unrecoverable"] == 1


def test_access_tokens_are_short_lived():
    assert get_settings().access_token_minutes == 60


def test_combining_again_replaces_the_previous_video_but_keeps_the_recording(
    client, db, admin_headers, module, storage, worker, tmp_path
):
    camera_id = _upload(client, admin_headers, _camera(tmp_path / "grabacion.webm"), "video/webm", "recording", module.course_id)
    deck_id = _upload(client, admin_headers, _deck(tmp_path / "slides.pdf"), "application/pdf", "deck", module.course_id,
                      purpose="deck")
    body = {"recording_asset_id": camera_id, "deck_asset_id": deck_id, "timeline": [{"at": 0, "slide": 0}]}
    client.post(f"/api/v1/modules/{module.id}/recording", headers=admin_headers, json=body)
    _run_until_done(db, worker)
    db.expire_all()
    first = db.get(Module, module.id)
    first_video, first_captions = first.video_asset_id, first.captions_asset_id
    first_file = storage.file_path(db.get(MediaAsset, first_video).path)

    body["timeline"] = [{"at": 0, "slide": 1}]
    client.post(f"/api/v1/modules/{module.id}/recording", headers=admin_headers, json=body)
    _run_until_done(db, worker)

    db.expire_all()
    stored = db.get(Module, module.id)
    assert stored.video_asset_id not in (first_video, camera_id)
    assert db.get(MediaAsset, first_video) is None and not first_file.exists()
    assert db.get(MediaAsset, first_captions) is None  # the new video got its own captions
    camera = db.get(MediaAsset, camera_id)
    assert camera is not None and db.get(MediaAsset, camera.meta["poster_asset_id"]) is not None


def test_legacy_migration_dates_courses_completed_before_the_date_existed(db, admin, collaborator, worker, monkeypatch):
    course = Course(title="Viejo", status="published", source="manual", settings={}, created_by=admin.id)
    db.add(course)
    db.flush()
    lesson = Module(course_id=course.id, title="Único", order=1, source="text")
    db.add(lesson)
    db.flush()
    enrollment = Enrollment(user_id=collaborator.id, course_id=course.id, status="completed", progress_pct=100)
    db.add(enrollment)
    db.flush()
    finished = datetime(2025, 3, 4, 10, 0, tzinfo=timezone.utc)
    db.add(ModuleProgress(enrollment_id=enrollment.id, module_id=lesson.id, completed=True, passed=True, attempts=1,
                          completed_at=finished))
    db.commit()

    queue.enqueue(db, "legacy.migrate", dedupe_key="legacy-migrate")
    worker()

    db.expire_all()
    assert db.get(Enrollment, enrollment.id).completed_at.replace(tzinfo=timezone.utc) == finished


def test_an_unsorted_timeline_with_a_missing_page_still_composes(
    client, db, admin_headers, module, storage, worker, tmp_path
):
    camera_id = _upload(client, admin_headers, _camera(tmp_path / "grabacion.webm"), "video/webm", "recording", module.course_id)
    deck_id = _upload(client, admin_headers, _deck(tmp_path / "slides.pdf"), "application/pdf", "deck", module.course_id,
                      purpose="deck")
    body = {
        "recording_asset_id": camera_id,
        "deck_asset_id": deck_id,
        "timeline": [{"at": 1.5, "slide": 0}, {"at": 0, "slide": 9}],  # out of order; page 9 doesn't exist
    }

    client.post(f"/api/v1/modules/{module.id}/recording", headers=admin_headers, json=body)
    _run_until_done(db, worker)

    db.expire_all()
    stored = db.get(Module, module.id)
    assert stored.generation_status == "completed" and stored.video_asset_id != camera_id
    assert db.get(MediaAsset, stored.poster_asset_id) is not None


def test_a_composition_for_a_deleted_module_leaves_nothing_behind(
    client, db, admin_headers, module, storage, worker, tmp_path, monkeypatch
):
    camera_id = _upload(client, admin_headers, _camera(tmp_path / "grabacion.webm"), "video/webm", "recording", module.course_id)
    deck_id = _upload(client, admin_headers, _deck(tmp_path / "slides.pdf"), "application/pdf", "deck", module.course_id,
                      purpose="deck")
    compose_with_deck = recording._compose_with_deck

    def compose_then_delete_module(ctx, inputs):
        made = compose_with_deck(ctx, inputs)
        with recording.open_session() as session:  # the admin deletes the module meanwhile
            session.delete(session.get(Module, ctx.module_id))
            session.commit()
        return made

    monkeypatch.setattr(recording, "_compose_with_deck", compose_then_delete_module)
    body = {"recording_asset_id": camera_id, "deck_asset_id": deck_id, "timeline": [{"at": 0, "slide": 0}]}
    client.post(f"/api/v1/modules/{module.id}/recording", headers=admin_headers, json=body)
    _run_until_done(db, worker)

    db.expire_all()
    assert db.query(MediaAsset).filter(MediaAsset.path.like("%/recording-%")).count() == 0
