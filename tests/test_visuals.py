"""Scene visuals: stock video, generated images and animated clips behind a scene's text (fake providers)."""

import shutil
from datetime import datetime, timezone
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from app.db.models import Course, Job, Module
from app.services.ai import designer
from app.services.video import visuals as visuals_module
from app.services.video.visuals import FakeVisuals, VisualError
from tests.conftest import auth_headers

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is not installed")

SCENES = [
    {"id": "s1", "slide": {"layout": "cover", "title": "Seguridad en planta", "subtitle": "Lo esencial"},
     "narration": "Bienvenida al módulo sobre el uso correcto del casco en la planta.",
     "visual": {"kind": "stock", "query": "factory worker helmet", "prompt": ""}},
    {"id": "s2", "slide": {"layout": "bullets", "title": "Reglas", "points": ["Siempre puesto", "Revisarlo"]},
     "narration": "Hay dos reglas: llevarlo siempre puesto y revisarlo antes de cada turno.",
     "visual": {"kind": "none", "query": "", "prompt": ""}},
]


@pytest.fixture
def admin_headers(client, admin):
    return auth_headers(client, admin.email)


@pytest.fixture
def module(db, admin):
    course = Course(title="Seguridad", status="draft", source="ai", settings={}, created_by=admin.id)
    db.add(course)
    db.flush()
    module = Module(course_id=course.id, title="Casco", order=1, source="ai", content_text="Resumen",
                    storyboard={"scenes": SCENES})
    db.add(module)
    db.commit()
    return module


def _render(client, headers, module, db, worker) -> None:
    response = client.post(f"/api/v1/courses/{module.course_id}/render", headers=headers,
                           json={"voice_id": "fake-voice-es", "presenter": False})
    assert response.status_code == 202, response.text
    for _ in range(6):
        worker()
        if not db.query(Job).filter(Job.status == "queued").count():
            break
    db.expire_all()


def _frame(video: Path, at: float, dest: Path) -> Image.Image:
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-ss", str(at), "-i", str(video), "-frames:v", "1",
                    str(dest)], check=True, capture_output=True)
    return Image.open(dest).convert("RGB")


def test_a_scene_with_a_visual_shows_it_behind_its_text(client, db, admin_headers, module, storage, worker, tmp_path):
    _render(client, admin_headers, module, db, worker)

    module = db.get(Module, module.id)
    assert module.generation_status == "completed"
    video = next((storage.root / storage.bucket).rglob("video-*/video.mp4"))
    cover, plain = _frame(video, 1.5, tmp_path / "a.png"), _frame(video, 6.0, tmp_path / "b.png")
    def colorful(image: Image.Image) -> int:  # where the scrim is lightest: the stock test pattern shows through
        return max(max(p) - min(p) for p in (image.getpixel((x, 600)) for x in range(1300, 1900, 50)))

    assert colorful(cover) > 40  # colored, not the brand's grey
    assert colorful(plain) < 20  # a scene without a visual keeps it
    assert list((storage.root / storage.bucket).rglob("visuals/*.mp4"))  # cached for the next render


def test_producing_again_reuses_the_visual(client, db, admin_headers, module, storage, worker, monkeypatch):
    _render(client, admin_headers, module, db, worker)
    calls = []
    monkeypatch.setattr(FakeVisuals, "stock", lambda self, query, dest: calls.append(query) or None)
    _render(client, admin_headers, module, db, worker)

    assert db.get(Module, module.id).generation_status == "completed"
    assert calls == []


def test_a_visual_that_fails_falls_back_then_leaves_the_scene_as_it_was(
    client, db, admin_headers, module, storage, worker, monkeypatch
):
    def unavailable(self, *args):
        raise VisualError("sin proveedor")

    monkeypatch.setattr(FakeVisuals, "stock", unavailable)
    _render(client, admin_headers, module, db, worker)
    assert list((storage.root / storage.bucket).rglob("visuals/*.png"))  # no stock: an image instead

    for path in (storage.root / storage.bucket).rglob("visuals/*"):
        path.unlink()
    monkeypatch.setattr(FakeVisuals, "image", unavailable)
    _render(client, admin_headers, module, db, worker)
    module = db.get(Module, module.id)
    assert module.generation_status == "completed" and module.video_asset_id  # neither: the brand background
    assert not list((storage.root / storage.bucket).rglob("visuals/*.*"))


def test_a_clip_becomes_an_image_without_veo(monkeypatch):
    from app.worker.jobs import render as render_job

    monkeypatch.setattr(FakeVisuals, "available", lambda self: {"stock", "image"})
    visuals_module.get_visuals.cache_clear()
    scene = {"slide": {"layout": "statement", "title": "Aire"}, "narration": "n",
             "visual": {"kind": "clip", "query": "tire", "prompt": "air leaking from a tire"}}
    render = render_job.RenderInput(course_id=1, course_title="", language="es", module_label="", scenes=[scene],
                                    voice_id="v", avatar_id="", presenter=False, theme="dark")
    assert render_job._visual_requests(render)[0].kind == "image"


def test_the_ai_gets_at_most_the_clips_a_module_may_have():
    def draft(kind: str, prompt: str = "a moving scene") -> designer.SceneDraft:
        return designer.SceneDraft(
            layout="statement", title="T", subtitle="", points=[], icons=[], icon="", stat_value="", stat_label="",
            quote_author="", left_heading="", left_points=[], right_heading="", right_points=[], narration="n",
            visual_kind=kind, visual_query="", visual_prompt=prompt,
        )

    scenes = designer.to_scenes([draft("clip"), draft("clip"), draft("clip"), draft("stock")], max_clips=2)
    kinds = [scene["visual"]["kind"] for scene in scenes]
    assert kinds == ["clip", "clip", "image", "none"]  # the third clip is an image; stock without keywords is dropped


def test_high_animation_turns_every_visual_into_a_clip():
    from app.worker.jobs import render as render_job

    visuals_module.get_visuals.cache_clear()
    scenes = [{"slide": {"layout": "statement", "title": "A"}, "narration": "n",
               "visual": {"kind": kind, "query": "truck highway", "prompt": ""}} for kind in ("stock", "none")]
    render = render_job.RenderInput(course_id=1, course_title="", language="es", module_label="", scenes=scenes,
                                    voice_id="v", avatar_id="", presenter=False, theme="dark", animation="high")
    requests = render_job._visual_requests(render)
    assert list(requests) == [0] and requests[0].kind == "clip" and requests[0].prompt == "A. truck highway"  # keywords plus what the slide says


def test_a_busy_provider_is_retried_then_a_cheaper_kind_is_used(client, db, admin_headers, module, storage, worker,
                                                                 monkeypatch):
    from app.worker.jobs import render as render_job

    monkeypatch.setattr(render_job, "VISUAL_POLL_SECONDS", 0)
    tries = []

    def busy(self, prompt):
        tries.append(prompt)
        raise VisualError("rate limit", retryable=True)

    monkeypatch.setattr(FakeVisuals, "start_clip", busy)
    m = db.get(Module, module.id)
    m.storyboard = {"scenes": [{**SCENES[0], "visual": {"kind": "clip", "query": "tire", "prompt": "air leaking"}},
                               SCENES[1]]}
    db.commit()
    response = client.post(f"/api/v1/courses/{module.course_id}/render", headers=admin_headers,
                           json={"voice_id": "fake-voice-es", "presenter": False})
    assert response.status_code == 202
    for _ in range(render_job.MAX_VISUAL_RETRIES + 6):
        past = datetime(2000, 1, 1, tzinfo=timezone.utc)  # fast-forward the waits
        db.query(Job).filter(Job.status == "queued").update({Job.run_after: past}, synchronize_session=False)
        db.commit()
        worker()
        if not db.query(Job).filter(Job.status == "queued").count():
            break
    db.expire_all()
    assert db.get(Module, module.id).generation_status == "completed"
    assert len(tries) == render_job.MAX_VISUAL_RETRIES + 1  # asked again while busy, then gave up on the clip
    assert list((storage.root / storage.bucket).rglob("visuals/*.png"))  # the scene got an image instead


def test_deleting_the_course_deletes_its_visuals(client, db, admin_headers, module, storage, worker):
    _render(client, admin_headers, module, db, worker)
    assert list((storage.root / storage.bucket).rglob("visuals/*"))

    assert client.delete(f"/api/v1/courses/{module.course_id}", headers=admin_headers).status_code == 204
    assert not list((storage.root / storage.bucket).rglob("visuals/*.*"))


def test_clips_are_capped_where_they_are_paid_for(monkeypatch):
    from app.core.config import get_settings
    from app.worker.jobs import render as render_job

    visuals_module.get_visuals.cache_clear()
    monkeypatch.setattr(get_settings(), "max_clips_per_module", 2)
    scenes = [{"slide": {"layout": "statement", "title": f"T{n}"}, "narration": "n",
               "visual": {"kind": "clip", "query": "", "prompt": f"scene {n}"}} for n in range(5)]
    render = render_job.RenderInput(course_id=1, course_title="", language="es", module_label="", scenes=scenes,
                                    voice_id="v", avatar_id="", presenter=False, theme="dark")
    kinds = [request.kind for request in render_job._visual_requests(render).values()]
    assert kinds == ["clip", "clip", "image", "image", "image"]


def test_the_gemini_key_never_leaves_gemini_and_downloads_stay_on_known_hosts(tmp_path):
    import httpx

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.host, request.headers.get("x-goog-api-key")))
        if request.url.host == "generativelanguage.googleapis.com":
            return httpx.Response(302, headers={"location": "https://video-downloads.googleusercontent.com/clip.mp4"})
        if request.url.host == "video-downloads.googleusercontent.com":
            return httpx.Response(200, content=b"video-bytes")
        return httpx.Response(200, content=b"no")

    provider = visuals_module.Visuals("", "", "gpt-image-2", "secret-key", "veo", client=httpx.Client(
        transport=httpx.MockTransport(handler), follow_redirects=False))
    provider.download_clip("https://generativelanguage.googleapis.com/v1beta/files/x:download?alt=media", tmp_path / "c")
    assert seen == [("generativelanguage.googleapis.com", "secret-key"), ("video-downloads.googleusercontent.com", None)]
    assert (tmp_path / "c.mp4").read_bytes() == b"video-bytes"

    with pytest.raises(VisualError):
        provider.download_clip("https://evil.example.com/clip.mp4", tmp_path / "d")
    with pytest.raises(VisualError):
        provider._download("http://169.254.169.254/latest/meta-data", tmp_path / "e", "descargar")
