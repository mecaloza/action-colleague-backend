"""Scene visuals: stock video, generated images and animated clips behind a scene's text (fake providers)."""

import shutil
from datetime import datetime, timezone
import subprocess
from pathlib import Path

import pytest
from PIL import Image, ImageChops

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


@pytest.fixture(autouse=True)
def no_cached_pictures():
    """The preview keeps the pictures it decoded in memory: every test starts without them."""
    from app.api.routes import studio as studio_routes

    studio_routes._PICTURES.clear()


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


def test_a_visual_that_fails_leaves_the_scene_with_the_brand_background(
    client, db, admin_headers, module, storage, worker, monkeypatch
):
    def unavailable(self, *args):
        raise VisualError("sin proveedor")

    monkeypatch.setattr(FakeVisuals, "stock", unavailable)
    _render(client, admin_headers, module, db, worker)

    module = db.get(Module, module.id)
    assert module.generation_status == "completed" and module.video_asset_id
    assert not list((storage.root / storage.bucket).rglob("visuals/*.*"))  # no dearer image instead of stock


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
            visual_kind=kind, visual_query="", visual_prompt=prompt, chart_labels=[], chart_values=[], chart_unit="",
        )

    scenes = designer.to_scenes([draft("clip"), draft("clip"), draft("clip"), draft("stock")], max_clips=2)
    kinds = [scene["visual"]["kind"] for scene in scenes]
    assert kinds == ["clip", "clip", "none", "none"]  # past the cap no clip (nor a decorative image); stock needs keywords


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



def test_a_visual_scene_always_has_its_infographic():
    def draft(layout: str, kind: str, prompt: str = "") -> designer.SceneDraft:
        return designer.SceneDraft(
            layout=layout, title="Presión y desgaste", subtitle="", points=[], icons=[], icon="", stat_value="",
            stat_label="", quote_author="", left_heading="", left_points=[], right_heading="", right_points=[],
            narration="n", visual_kind=kind, visual_query="", visual_prompt=prompt, chart_labels=[], chart_values=[],
            chart_unit="",
        )

    visual, over_text = designer.to_scenes([draft("visual", "none"), draft("bullets", "infographic", "x")])
    assert visual["visual"] == {"kind": "infographic", "query": "", "prompt": "Presión y desgaste", "variant": 0}
    assert over_text["visual"]["kind"] == "none"  # an infographic under a slide's text would clash


INFOGRAPHIC_SCENES = [
    {"id": "s1", "slide": {"layout": "visual", "title": "Presión y desgaste"},
     "narration": "Mira las tres huellas: baja, correcta y alta presión.",
     "visual": {"kind": "infographic", "query": "", "prompt": "Infografía: presión y desgaste, tres huellas."}},
    {"id": "s2", "slide": {"layout": "chart", "title": "Kilómetros según presión", "chart_labels": ["Correcta", "-20 psi"],
                           "chart_values": [120000, 84000], "chart_unit": "km"},
     "narration": "Con presión correcta la llanta rinde ciento veinte mil kilómetros; con veinte psi menos, ochenta y cuatro mil."},
]


def test_infographics_are_made_with_the_script_and_shown_in_the_preview(
    client, db, admin_headers, module, storage, worker
):
    module = db.get(Module, module.id)
    response = client.put(f"/api/v1/modules/{module.id}/storyboard", headers=admin_headers,
                          json={"scenes": INFOGRAPHIC_SCENES})
    assert response.status_code == 200, response.text
    preview = {"slide": INFOGRAPHIC_SCENES[0]["slide"], "visual": INFOGRAPHIC_SCENES[0]["visual"],
               "course_id": module.course_id, "context": {"index": 1, "total": 2}}
    pending = client.post("/api/v1/slides/preview", headers={**admin_headers, "Origin": "http://localhost:3001"},
                          json=preview)
    assert pending.headers.get("x-visual-pending") == "1"  # over the sample until it is made
    assert "x-visual-pending" in pending.headers["access-control-expose-headers"].lower()  # the editor can read it

    worker()  # the visuals.prepare job the save queued
    shown = client.post("/api/v1/slides/preview", headers=admin_headers, json=preview)
    assert "x-visual-pending" not in shown.headers

    def orange(content: bytes) -> int:  # the fake infographic's panels are outlined in the brand's orange
        image = Image.open(__import__("io").BytesIO(content)).convert("RGB")
        return sum(1 for r, g, b in image.getdata() if r > 200 and 40 < g < 120 and b < 60)

    assert orange(shown.content) > 10 * (orange(pending.content) + 1)
    assert len(list((storage.root / storage.bucket).rglob("visuals/*.png"))) == 1


def test_producing_reuses_the_reviewed_infographic(client, db, admin_headers, module, storage, worker, monkeypatch):
    client.put(f"/api/v1/modules/{module.id}/storyboard", headers=admin_headers, json={"scenes": INFOGRAPHIC_SCENES})
    worker()
    calls = []
    monkeypatch.setattr(FakeVisuals, "infographic", lambda self, prompt, dest: calls.append(prompt))
    _render(client, admin_headers, module, db, worker)
    assert db.get(Module, module.id).generation_status == "completed" and calls == []


def test_new_layouts_reveal_their_parts_one_by_one():
    from app.services.slides.render import beat_count
    from app.services.slides.render import render as render_slide
    from app.services.slides.spec import Slide, SlideContext

    chart = Slide(layout="chart", title="T", chart_labels=["A", "B", "C"], chart_values=[3, 2, 1], chart_unit="km")
    calculation = Slide(layout="calculation", title="T", points=["Paso 1", "Paso 2"], stat_value="$1 650",
                        stat_label="de ahorro")
    case = Slide(layout="case", title="Caso", points=["Situación", "Diagnóstico", "Solución", "Resultado"])
    assert [beat_count(chart), beat_count(calculation), beat_count(case)] == [4, 4, 5]
    for slide in (chart, calculation, case):
        frames = [render_slide(slide, SlideContext(reveal=shown)) for shown in range(1, beat_count(slide) + 1)]
        assert all(ImageChops.difference(a, b).getbbox() for a, b in zip(frames, frames[1:]))  # each beat adds


def test_a_visual_scene_without_its_picture_still_shows_its_title():
    from app.services.slides.render import render as render_slide
    from app.services.slides.spec import Slide, SlideContext

    empty = render_slide(Slide(layout="visual", title=""), SlideContext())
    titled = render_slide(Slide(layout="visual", title="Presión y desgaste"), SlideContext())
    assert ImageChops.difference(empty, titled).getbbox() is not None


def _drain(db, worker, rounds: int = 10) -> None:
    """Run the queue until nothing is left, skipping the waits."""
    for _ in range(rounds):
        db.query(Job).filter(Job.status == "queued").update({Job.run_after: datetime(2000, 1, 1, tzinfo=timezone.utc)},
                                                            synchronize_session=False)
        db.commit()
        worker()
        if not db.query(Job).filter(Job.status == "queued").count():
            break
    db.expire_all()


@pytest.mark.parametrize("values", ["[1e308, 1]", "[NaN, 1]", "[Infinity, 1]"])
def test_a_chart_value_the_slide_cannot_draw_is_refused(client, admin_headers, module, values):
    slide = ('{"layout": "chart", "title": "T", "chart_labels": ["A", "B"], "chart_values": %s}' % values)
    preview = client.post("/api/v1/slides/preview", headers={**admin_headers, "Content-Type": "application/json"},
                          content='{"slide": %s, "context": {}}' % slide)
    scenes = '{"scenes": [{"id": "s1", "slide": %s, "narration": "n"}]}' % slide
    saved = client.put(f"/api/v1/modules/{module.id}/storyboard",
                       headers={**admin_headers, "Content-Type": "application/json"}, content=scenes)
    assert (preview.status_code, saved.status_code) == (422, 422)


def test_the_ai_s_unusable_figures_drop_their_bars():
    scene = designer.SceneDraft(
        layout="chart", title="T", subtitle="", points=[], icons=[], icon="", stat_value="", stat_label="",
        quote_author="", left_heading="", left_points=[], right_heading="", right_points=[], narration="n",
        visual_kind="none", visual_query="", visual_prompt="", chart_labels=["A", "B", "C"],
        chart_values=[float("nan"), 1e300, 5], chart_unit="",
    )
    slide = designer.to_slide(scene)
    assert (slide.chart_labels, slide.chart_values) == (["C"], [5])


def test_a_busy_image_provider_makes_the_infographic_later(client, db, admin_headers, module, storage, worker,
                                                           monkeypatch):
    from app.worker.jobs import render as render_job

    monkeypatch.setattr(render_job, "VISUAL_POLL_SECONDS", 0)
    made = FakeVisuals.infographic
    tries = []

    def busy_once(self, prompt, dest):
        tries.append(prompt)
        if len(tries) == 1:
            raise VisualError("rate limit", retryable=True)
        return made(self, prompt, dest)

    monkeypatch.setattr(FakeVisuals, "infographic", busy_once)
    client.put(f"/api/v1/modules/{module.id}/storyboard", headers=admin_headers, json={"scenes": INFOGRAPHIC_SCENES})
    _drain(db, worker)
    assert len(tries) == 2 and list((storage.root / storage.bucket).rglob("visuals/*.png"))


def test_only_what_the_video_will_use_is_made_ahead(client, db, admin_headers, module, storage, worker, monkeypatch):
    images = []
    monkeypatch.setattr(FakeVisuals, "image", lambda self, prompt, dest: images.append(prompt))
    clip = {"id": "s3", "slide": {"layout": "statement", "title": "Aire"}, "narration": "n",
            "visual": {"kind": "clip", "query": "", "prompt": "air leaking from a tire"}}
    client.put(f"/api/v1/modules/{module.id}/storyboard", headers=admin_headers,
               json={"scenes": [*INFOGRAPHIC_SCENES, clip]})
    _drain(db, worker)
    assert images == []  # the clip's fallback image is made only if the clip can't be
    assert len(list((storage.root / storage.bucket).rglob("visuals/*.png"))) == 1  # the infographic


def test_a_picture_nobody_is_making_is_not_pending(client, db, admin_headers, module):
    preview = {"slide": INFOGRAPHIC_SCENES[0]["slide"], "visual": INFOGRAPHIC_SCENES[0]["visual"],
               "course_id": module.course_id, "context": {}}
    response = client.post("/api/v1/slides/preview", headers=admin_headers, json=preview)  # the script isn't saved
    assert response.status_code == 200 and "x-visual-pending" not in response.headers


def test_a_save_while_the_infographics_are_being_made_is_made_too(client, db, admin_headers, module, storage, worker,
                                                                  monkeypatch):
    made = FakeVisuals.infographic
    changed = [{**INFOGRAPHIC_SCENES[0], "visual": {**INFOGRAPHIC_SCENES[0]["visual"], "prompt": "Otra infografía"}}]

    def save_meanwhile(self, prompt, dest):
        if not getattr(save_meanwhile, "saved", False):
            save_meanwhile.saved = True  # the admin saves again while the first one is drawn
            assert client.put(f"/api/v1/modules/{module.id}/storyboard", headers=admin_headers,
                              json={"scenes": changed}).status_code == 200
        return made(self, prompt, dest)

    monkeypatch.setattr(FakeVisuals, "infographic", save_meanwhile)
    client.put(f"/api/v1/modules/{module.id}/storyboard", headers=admin_headers, json={"scenes": INFOGRAPHIC_SCENES})
    _drain(db, worker)
    assert len(list((storage.root / storage.bucket).rglob("visuals/*.png"))) == 2


def test_production_waits_for_the_infographics_being_made(client, db, admin_headers, module, storage, worker,
                                                          monkeypatch):
    calls = []
    made = FakeVisuals.infographic
    monkeypatch.setattr(FakeVisuals, "infographic", lambda self, prompt, dest: calls.append(prompt) or made(self, prompt, dest))
    client.put(f"/api/v1/modules/{module.id}/storyboard", headers=admin_headers, json={"scenes": INFOGRAPHIC_SCENES})
    response = client.post(f"/api/v1/courses/{module.course_id}/render", headers=admin_headers,
                           json={"voice_id": "fake-voice-es", "presenter": False})
    assert response.status_code == 202
    _drain(db, worker, rounds=20)
    assert db.get(Module, module.id).generation_status == "completed"
    assert len(calls) == 1  # made once, for the editor, and reused by the video


def test_a_description_not_saved_yet_is_not_pending_while_others_are_drawn(client, db, admin_headers, module):
    client.put(f"/api/v1/modules/{module.id}/storyboard", headers=admin_headers, json={"scenes": INFOGRAPHIC_SCENES})
    job = db.query(Job).filter(Job.type == "visuals.prepare").one()
    saved = {"slide": INFOGRAPHIC_SCENES[0]["slide"], "visual": INFOGRAPHIC_SCENES[0]["visual"],
             "course_id": module.course_id, "context": {}}
    edited = {**saved, "visual": {**saved["visual"], "prompt": "Otra descripción, aún sin guardar"}}

    def pending(preview) -> bool:
        return client.post("/api/v1/slides/preview", headers=admin_headers, json=preview).headers.get(
            "x-visual-pending") == "1"

    assert pending(saved) and pending(edited)  # not started yet: it will read whatever is saved
    from app.services.video.visuals import VisualRequest
    from app.worker.jobs.render import scene_prompt

    making = VisualRequest("infographic", "", scene_prompt(INFOGRAPHIC_SCENES[0])).key()
    job.status, job.state = "running", {"signature": [f"0:{making}"]}  # started: it reads the saved script
    db.commit()
    assert pending(saved) and not pending(edited)


def test_every_beat_of_the_new_layouts_is_timed_with_the_narration():
    from app.services.slides.render import beat_count
    from app.services.slides.spec import Slide
    from app.services.video.captions import TimedWord
    from app.services.video.motion import beat_texts, beat_times

    case = Slide(layout="case", title="Caso", points=["Flota de reparto", "Hombros gastados", "Calibrar", "Más km"])
    slides = [
        Slide(layout="chart", title="T", chart_labels=["A", "B", "C"], chart_values=[3, 2, 1]),
        Slide(layout="calculation", title="T", points=["Paso uno", "Paso dos", "Paso tres"], stat_value="$0.05"),
        case, Slide(layout="visual", title="Infografía"),
    ]
    for slide in slides:
        assert len(beat_texts(slide)) == beat_count(slide) - 1, slide.layout
    said = "la situación era esta el diagnóstico fue claro la solución llegó y el resultado se notó".split()
    words = [TimedWord(text, 1.0 + 2 * index, 1.5 + 2 * index) for index, text in enumerate(said)]
    times = beat_times(case, words, 0.0, 40.0)
    assert len(times) == 4 and times == sorted(times) and times[2] > 10  # "la solución" comes late in the scene


def test_case_parts_do_not_repeat_their_label():
    scene = designer.SceneDraft(
        layout="case", title="Caso", subtitle="", icons=[], icon="", stat_value="", stat_label="", quote_author="",
        left_heading="", left_points=[], right_heading="", right_points=[], narration="n", visual_kind="none",
        visual_query="", visual_prompt="", chart_labels=[], chart_values=[], chart_unit="",
        points=["Situación: flota de reparto", "diagnostico: hombros gastados", "Se calibró: 110 psi", "Resultado: +12% km"],
    )
    assert designer.to_slide(scene).points == ["flota de reparto", "hombros gastados", "Se calibró: 110 psi", "+12% km"]


def test_a_long_word_gets_a_smaller_size_before_it_is_cut():
    from PIL import Image as PILImage, ImageDraw

    from app.services.slides.render import DISPLAY_FONT, fit, text_width

    draw = ImageDraw.Draw(PILImage.new("RGB", (10, 10)))
    fitted = fit(draw, "Selección y comercialización de llantas", DISPLAY_FONT, 700, 520, 400, 96, 40, max_lines=4)
    assert "comercialización" in " ".join(fitted.lines).split()  # whole, at a size where it fits
    assert all(text_width(draw, line, fitted.font) <= 520 for line in fitted.lines)
