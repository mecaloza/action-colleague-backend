"""Hybrid video engine with real FFmpeg and the offline fake voice and presenter."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from app.db.models import Course, Job, MediaAsset, Module
from app.services.ai.fake import FakeAvatar, FakeVoice
from app.services.slides.render import BUBBLE_BOX
from app.services.slides.render import render as render_slide
from app.services.slides.spec import Slide, SlideContext
from app.services.video import compose
from app.services.video.avatar import RenderStatus
from app.worker import queue
from tests.conftest import auth_headers

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is not installed")

SCENES = [
    {"id": "s1", "slide": {"layout": "cover", "title": "Seguridad en planta", "subtitle": "Lo esencial"},
     "narration": "Bienvenida al módulo sobre el uso correcto del casco en la planta."},
    {"id": "s2", "slide": {"layout": "bullets", "title": "Reglas", "points": ["Siempre puesto", "Revisarlo"]},
     "narration": "Hay dos reglas: llevarlo siempre puesto y revisarlo antes de cada turno."},
]
RENDER_CHOICES = {"voice_id": "fake-voice-es", "avatar_id": "fake-avatar", "presenter": True}


def _probe(path: Path) -> dict:
    return json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        check=True, capture_output=True,
    ).stdout)


def _stream(info: dict, codec_type: str) -> dict:
    return next(stream for stream in info["streams"] if stream["codec_type"] == codec_type)


def _frame(video: Path, at: float, dest: Path) -> Image.Image:
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-ss", str(at), "-i", str(video), "-frames:v", "1", str(dest)],
                   check=True, capture_output=True)
    return Image.open(dest).convert("RGB")


@pytest.fixture
def narration(tmp_path):
    voice = FakeVoice()
    audio = []
    for index, scene in enumerate(SCENES):
        path = tmp_path / f"s{index}.mp3"
        path.write_bytes(voice.speak(scene["narration"], "v").audio)
        audio.append(path)
    return compose.build_narration(audio, tmp_path)


@pytest.fixture
def slides(tmp_path):
    paths = []
    for index, scene in enumerate(SCENES):
        path = tmp_path / f"slide{index}.png"
        render_slide(Slide.model_validate(scene["slide"]), SlideContext(index=index + 1, total=2, presenter=True)).save(path)
        paths.append(path)
    return paths


def test_narration_timings_come_from_the_audio_itself(narration):
    assert narration.starts[0] == pytest.approx(compose.LEAD_IN)
    assert narration.starts[1] == pytest.approx(compose.LEAD_IN + narration.lengths[0] + compose.GAP, abs=1e-3)
    assert narration.total == pytest.approx(narration.starts[1] + narration.lengths[1] + compose.TAIL, abs=1e-3)


def test_video_without_presenter(tmp_path, narration, slides):
    output = tmp_path / "video.mp4"
    duration = compose.compose_video(slides, narration, None, output, tmp_path)

    info = _probe(output)
    video, audio = _stream(info, "video"), _stream(info, "audio")
    assert (video["codec_name"], video["width"], video["height"]) == ("h264", 1920, 1080)
    assert (audio["codec_name"], audio["sample_rate"]) == ("aac", "48000")
    assert float(info["format"]["duration"]) == pytest.approx(duration, abs=0.15)


def test_presenter_bubble_is_round_and_not_stretched(tmp_path, narration, slides):
    avatar_source = tmp_path / "narration.mp3"
    compose.to_mp3(narration.audio, avatar_source)
    presenter = FakeAvatar()
    audio_id = presenter.upload_audio(avatar_source)
    video_id = presenter.start("fake-avatar", "#1D1D1D", idempotency_key="test", audio_asset_id=audio_id)
    avatar = tmp_path / "presenter.mp4"
    presenter.download(f"fake://{video_id}", avatar)

    plain, with_bubble = tmp_path / "plain.mp4", tmp_path / "bubble.mp4"
    compose.compose_video(slides, narration, None, plain, tmp_path)
    compose.compose_video(slides, narration, avatar, with_bubble, tmp_path)

    a, b = _frame(plain, 1.0, tmp_path / "a.png"), _frame(with_bubble, 1.0, tmp_path / "b.png")
    left, top, right, bottom = BUBBLE_BOX  # the corner the slides keep free for the presenter
    center = ((left + right) // 2, (top + bottom) // 2)
    corner = (left + 8, top + 8)  # inside the square, outside the circle
    assert sum(abs(p - q) for p, q in zip(a.getpixel(center), b.getpixel(center))) > 60  # the presenter is there
    assert sum(abs(p - q) for p, q in zip(a.getpixel(corner), b.getpixel(corner))) < 30  # masked: slide shows through


@pytest.fixture
def admin_headers(client, admin):
    return auth_headers(client, admin.email)


@pytest.fixture
def drafted_module(db, admin):
    course = Course(title="Seguridad", status="draft", source="ai", settings={}, created_by=admin.id)
    db.add(course)
    db.flush()
    module = Module(course_id=course.id, title="Casco", order=1, source="ai", content_text="Resumen",
                    storyboard={"scenes": SCENES})
    db.add(module)
    db.commit()
    return module


def _run_until_done(db, worker, rounds: int = 5) -> None:
    """Run jobs, fast-forwarding the presenter waits (rescheduled jobs)."""
    for _ in range(rounds):
        worker()
        pending = db.query(Job).filter(Job.status == "queued").all()
        if not pending:
            return
        for job in pending:
            job.run_after = queue.utcnow()
        db.commit()


def test_rendering_produces_the_modules_video_with_presenter_and_captions(
    client, db, admin_headers, drafted_module, storage, worker
):
    jobs = client.post(
        f"/api/v1/courses/{drafted_module.course_id}/render", headers=admin_headers,
        json={**RENDER_CHOICES, "theme": "dark"},
    )
    assert jobs.status_code == 202 and len(jobs.json()) == 1

    worker()  # narration, then the presenter render starts and the job waits for it
    db.expire_all()
    job = db.query(Job).one()
    assert job.status == "queued" and job.state["phase"] == "presenter" and job.state["heygen"]["video_id"]
    _run_until_done(db, worker)

    [module] = client.get(f"/api/v1/courses/{drafted_module.course_id}", headers=admin_headers).json()["modules"]
    assert module["generation_status"] == "completed"
    assert module["video"]["mime_type"] == "video/mp4" and module["poster_url"] and module["captions_url"]
    vtt = client.get(module["captions_url"].replace("http://testserver", "")).text
    assert vtt.startswith("WEBVTT") and "casco" in vtt.lower()
    db.expire_all()
    stored = db.get(Module, drafted_module.id)
    assert stored.storyboard["render"]["avatar_id"] == "fake-avatar" and stored.storyboard["render"]["warning"] is None
    # Intermediate narration files were cleaned up.
    assert not list((storage.root / storage.bucket).rglob("render/*/scene_*.mp3"))


def test_a_failed_presenter_still_produces_the_video(client, db, admin_headers, drafted_module, storage, worker, monkeypatch):
    monkeypatch.setattr(FakeAvatar, "status", lambda self, video_id: RenderStatus(status="failed", error="sin créditos"))
    client.post(f"/api/v1/courses/{drafted_module.course_id}/render", headers=admin_headers, json=RENDER_CHOICES)
    _run_until_done(db, worker)

    db.expire_all()
    module = db.get(Module, drafted_module.id)
    assert module.generation_status == "completed" and module.video_asset_id
    assert "sin créditos" in module.storyboard["render"]["warning"]


def test_rerendering_replaces_the_previous_video(client, db, admin_headers, drafted_module, storage, worker):
    for _ in range(2):
        client.post(f"/api/v1/modules/{drafted_module.id}/render", headers=admin_headers)
        _run_until_done(db, worker)
    db.expire_all()
    assert db.query(MediaAsset).filter(MediaAsset.kind == "video").count() == 1


def test_modules_without_storyboard_cannot_be_rendered(client, db, admin_headers, admin):
    course = Course(title="Vacío", status="draft", source="ai", settings={}, created_by=admin.id)
    db.add(course)
    db.flush()
    db.add(Module(course_id=course.id, title="Sin guion", order=1, source="ai"))
    db.commit()
    assert client.post(f"/api/v1/courses/{course.id}/render", headers=admin_headers, json={}).status_code == 422


def test_voices_avatars_and_capabilities(client, admin_headers):
    caps = client.get("/api/v1/studio/capabilities", headers=admin_headers).json()
    assert caps["voice"] and caps["avatar"]
    assert client.get("/api/v1/studio/voices", headers=admin_headers).json()[0]["id"] == "fake-voice-es"
    assert client.get("/api/v1/studio/avatars", headers=admin_headers).json()[0]["id"] == "fake-avatar"
    cloned = client.post(
        "/api/v1/studio/voices/clone", headers=admin_headers,
        data={"name": "Mi voz"}, files={"file": ("muestra.mp3", b"ID3fake", "audio/mpeg")},
    )
    assert cloned.status_code == 201 and cloned.json()["id"].startswith("fake-clone-")
