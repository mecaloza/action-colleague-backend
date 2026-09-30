"""AI studio with the offline fake LLM: outline -> modules -> storyboards, reading and quizzes."""

import httpx
import openai
import pytest
from PIL import Image, ImageDraw

from app.db.models import Course, Evaluation, Job, MediaAsset, Module
from app.services.ai import designer
from app.services.ai.fake import FakeLLM
from app.services.ai.llm import LLMError, OpenAILLM
from app.services.slides.render import BODY_FONT, BUBBLE_BOX, WIDTH, font, render, wrap
from app.services.slides.spec import MAX_LABEL_CHARS, MAX_TEXT_CHARS, ComparisonColumn, Slide, SlideContext
from tests.conftest import auth_headers


@pytest.fixture
def admin_headers(client, admin):
    return auth_headers(client, admin.email)


@pytest.fixture
def ai_course(db, admin):
    course = Course(title="Borrador IA", status="draft", source="ai", settings={}, created_by=admin.id)
    db.add(course)
    db.commit()
    return course


def _outline(client, headers, course_id, **payload):
    response = client.post(f"/api/v1/courses/{course_id}/outline/generate", headers=headers, json=payload)
    assert response.status_code == 202, response.text
    return response.json()


def _job(client, headers, job):
    return client.get(f"/api/v1/jobs/{job['id']}", headers=headers).json()


def _manual_module(db, admin, title, content_text):
    """A text module of a manual course, the kind whose quiz is suggested from its content."""
    course = Course(title="Manual", status="draft", source="manual", settings={}, created_by=admin.id)
    db.add(course)
    db.flush()
    module = Module(course_id=course.id, title=title, order=1, source="text", content_text=content_text)
    db.add(module)
    db.commit()
    return module


def test_outline_is_proposed_from_the_brief_and_materials(client, db, admin_headers, ai_course, worker):
    db.add(MediaAsset(kind="document", status="ready", bucket="b", path="p/doc.pdf", course_id=ai_course.id,
                      original_filename="politica.pdf", meta={"text": "Uso obligatorio del casco en planta."}))
    db.commit()

    job = _outline(client, admin_headers, ai_course.id, brief="Seguridad con el casco", modules=3, minutes=15)
    worker()

    assert _job(client, admin_headers, job)["status"] == "succeeded"
    outline = client.get(f"/api/v1/courses/{ai_course.id}/outline", headers=admin_headers).json()
    assert len(outline["modules"]) == 3 and outline["title"]


def test_the_brief_is_kept_to_resume_the_studio(client, admin_headers, ai_course, worker):
    url = f"/api/v1/courses/{ai_course.id}"
    saved = client.patch(url, headers=admin_headers, json={"settings": {"brief": "Seguridad en planta", "minutes": 30}})
    assert saved.json()["settings"]["brief"] == "Seguridad en planta" and saved.json()["settings"]["minutes"] == 30

    client.patch(url, headers=admin_headers, json={"settings": {"modules": 5}})  # e.g. a newer ask the API turned down
    _outline(client, admin_headers, ai_course.id, brief="Seguridad con el casco", minutes=15, tone="Cercano", modules=2)
    worker()

    settings = client.get(url, headers=admin_headers).json()["settings"]
    assert (settings["brief"], settings["minutes"], settings["tone"]) == ("Seguridad con el casco", 15, "Cercano")
    assert settings["modules"] == 2  # the brief shown is the one the proposal was made from


def test_the_module_count_gives_way_to_changes_that_ask_for_another():
    kwargs = {"brief": "Casco", "audience": "", "tone": "", "minutes": 20, "materials": ""}

    assert "Exactamente 4 módulos." in designer.outline_prompt(target_modules=4, **kwargs)
    with_changes = designer.outline_prompt(target_modules=4, feedback="Agrega un módulo de primeros auxilios", **kwargs)
    assert "Exactamente 4 módulos, salvo que los cambios pedidos indiquen otra cantidad." in with_changes
    assert "Entre 3 y 6 módulos" in designer.outline_prompt(target_modules=None, feedback="Más ejemplos", **kwargs)


def test_approving_the_outline_creates_the_modules_and_drafts_them(client, db, admin_headers, ai_course, worker):
    _outline(client, admin_headers, ai_course.id, brief="Seguridad con el casco", modules=2)
    worker()
    outline = client.get(f"/api/v1/courses/{ai_course.id}/outline", headers=admin_headers).json()
    outline["title"] = "Seguridad en planta"
    outline["modules"][0]["title"] = "Por qué usamos casco"

    course = client.put(f"/api/v1/courses/{ai_course.id}/outline", headers=admin_headers, json=outline).json()
    assert course["title"] == "Seguridad en planta"
    assert [m["title"] for m in course["modules"]][0] == "Por qué usamos casco"
    assert all(m["source"] == "ai" and m["generation_status"] == "pending" for m in course["modules"])

    jobs = client.post(f"/api/v1/courses/{ai_course.id}/draft", headers=admin_headers, json={}).json()
    assert len(jobs) == 2
    assert client.get(f"/api/v1/courses/{ai_course.id}", headers=admin_headers).json()["modules"][0]["generation_status"] == "queued"
    assert worker() == 2

    detail = client.get(f"/api/v1/courses/{ai_course.id}", headers=admin_headers).json()
    first, second = detail["modules"]
    assert first["scene_count"] >= 3 and first["content_text"]  # storyboard + reading summary
    assert first["evaluation"] is None  # the outline said this module has no quiz
    assert second["evaluation"]["question_count"] >= 1
    storyboard = client.get(f"/api/v1/modules/{first['id']}/storyboard", headers=admin_headers).json()
    assert storyboard["scenes"][0]["slide"]["layout"] == "cover" and storyboard["scenes"][0]["narration"]

    # A course with content can't be replaced by a new outline.
    again = client.put(f"/api/v1/courses/{ai_course.id}/outline", headers=admin_headers, json=outline)
    assert again.status_code == 409


def test_storyboard_can_be_edited_and_regenerated(client, db, admin_headers, ai_course, worker):
    module = Module(course_id=ai_course.id, title="Casco", order=1, source="ai", storyboard={"scenes": []})
    db.add(module)
    db.commit()
    client.post(f"/api/v1/modules/{module.id}/storyboard/regenerate", headers=admin_headers, json={"feedback": "Más corto"})
    worker()

    storyboard = client.get(f"/api/v1/modules/{module.id}/storyboard", headers=admin_headers).json()
    storyboard["scenes"][0]["narration"] = "Narración editada a mano."
    saved = client.put(f"/api/v1/modules/{module.id}/storyboard", headers=admin_headers, json=storyboard)
    assert saved.status_code == 200 and saved.json()["scenes"][0]["narration"] == "Narración editada a mano."

    duplicated = {"scenes": [storyboard["scenes"][0], storyboard["scenes"][0]]}
    assert client.put(f"/api/v1/modules/{module.id}/storyboard", headers=admin_headers, json=duplicated).status_code == 422


def test_quiz_suggestions_come_back_for_review_without_saving(client, db, admin_headers, admin, worker):
    module = _manual_module(db, admin, "Casco", "El casco se usa siempre en la planta. " * 10)

    job = client.post(f"/api/v1/modules/{module.id}/evaluation/generate", headers=admin_headers, json={"count": 4}).json()
    worker()

    result = _job(client, admin_headers, job)
    assert result["status"] == "succeeded" and len(result["result"]["questions"]) >= 1
    assert db.query(Evaluation).count() == 0  # the admin reviews and saves them in the editor


def test_quiz_needs_enough_content(client, db, admin_headers, admin, worker):
    module = _manual_module(db, admin, "Vacío", "")
    job = client.post(f"/api/v1/modules/{module.id}/evaluation/generate", headers=admin_headers, json={}).json()
    worker()
    failed = _job(client, admin_headers, job)
    assert failed["status"] == "failed" and "suficiente contenido" in failed["error"]  # retrying can't help


def test_capabilities_and_slide_preview(client, admin_headers):
    caps = client.get("/api/v1/studio/capabilities", headers=admin_headers).json()
    assert caps["ai"] is True

    png = client.post(
        "/api/v1/slides/preview", headers=admin_headers,
        json={"slide": {"layout": "bullets", "title": "Hola", "points": ["Uno", "Dos"]}, "context": {"presenter": True}},
    )
    assert png.status_code == 200 and png.headers["content-type"] == "image/png"
    assert png.content[:4] == b"\x89PNG"


# The longest texts a slide accepts, with and without spaces (URLs, figures).
LONG = ("Usar siempre el equipo de protección personal completo antes de entrar a la zona de producción " * 4)[
    :MAX_TEXT_CHARS
]
LABEL = LONG[:MAX_LABEL_CHARS]
URL = ("https://intranet.empresa.com/seguridad/procedimientos/uso-obligatorio-del-casco" * 4)[:MAX_TEXT_CHARS]
LAYOUTS = [
    Slide(layout="cover", title=LONG, subtitle=LONG),
    Slide(layout="cover", title=URL, subtitle=URL),
    Slide(layout="bullets", title=LONG, points=[LONG] * 5),
    Slide(layout="bullets", title=URL, points=[URL] * 5),
    Slide(layout="statement", title=LONG, quote_author=LABEL),
    Slide(layout="stat", stat_value="1.234.567,89 %", stat_label=LONG, subtitle=LONG),
    Slide(layout="stat", stat_value="US$1.500.000.000", stat_label="en multas evitadas el año pasado en todas las plantas"),
    Slide(layout="stat", stat_value="87%",
          stat_label="de los accidentes graves en planta se evitan usando correctamente el casco de seguridad"),
    Slide(layout="steps", title=LONG, points=[LONG] * 5),
    Slide(layout="comparison", title=LONG, left=ComparisonColumn(heading=LABEL, points=[LONG] * 4),
          right=ComparisonColumn(heading=URL[:MAX_LABEL_CHARS], points=[URL] * 4)),
    Slide(layout="closing", title=LONG, points=[LONG] * 5),
]


GRADIENT_SPREAD = 25  # the background gradient alone varies less than this (0-255 gray levels)


def _brightness_spread(image, box):
    """Darkest-to-brightest distance inside the box: small only where nothing but the background is drawn."""
    darkest, brightest = image.crop(box).convert("L").getextrema()
    return brightest - darkest


@pytest.mark.parametrize("slide", LAYOUTS, ids=[f"{s.layout}-{i}" for i, s in enumerate(LAYOUTS)])
@pytest.mark.parametrize("theme", ["dark", "light"])
def test_every_layout_keeps_long_text_inside_and_away_from_the_presenter(slide, theme):
    image = render(slide, SlideContext(course_title=LONG, module_label="Módulo 1", presenter=True, theme=theme))
    assert image.size == (1920, 1080)
    # The presenter bubble's corner stays empty: only the background gradient is there.
    corner = _brightness_spread(image, BUBBLE_BOX)
    assert corner < GRADIENT_SPREAD, f"something was drawn where the presenter goes (spread {corner})"
    # Nothing touches the right edge (text never overflows the slide).
    assert _brightness_spread(image, (WIDTH - 40, 0, WIDTH, image.height)) < GRADIENT_SPREAD


def test_wrapping_stops_once_the_text_cannot_fit():
    """Long texts are not laid out in full for every font size tried (it took minutes per slide)."""
    draw = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    fnt = font(BODY_FONT, 40, 500)
    assert len(wrap(draw, "palabra " * 50_000, fnt, 600, max_lines=3)) == 4
    assert all(draw.textlength(line, font=fnt) <= 600 for line in wrap(draw, "x" * 200, fnt, 600))


def test_slide_preview_rejects_oversized_text(client, admin_headers):
    huge = {"layout": "cover", "title": "palabra " * 10_000}
    assert client.post("/api/v1/slides/preview", headers=admin_headers, json={"slide": huge}).status_code == 422
    many = {"layout": "bullets", "title": "T", "points": ["p"] * 1000}
    assert client.post("/api/v1/slides/preview", headers=admin_headers, json={"slide": many}).status_code == 422


def _approved_course(client, headers, course_id, worker, modules=1):
    _outline(client, headers, course_id, brief="Seguridad con el casco", modules=modules)
    worker()
    outline = client.get(f"/api/v1/courses/{course_id}/outline", headers=headers).json()
    return client.put(f"/api/v1/courses/{course_id}/outline", headers=headers, json=outline).json()


def test_outline_limits_protect_the_course_settings(client, admin_headers, ai_course, worker, monkeypatch):
    real_outline = FakeLLM._outline
    monkeypatch.setattr(FakeLLM, "_outline", lambda self, p: real_outline(self, p).model_copy(update={"audience": "y" * 900}))
    _outline(client, admin_headers, ai_course.id, brief="Casco", modules=1)
    worker()
    # The model's long audience is cut to fit the settings: the course still opens.
    detail = client.get(f"/api/v1/courses/{ai_course.id}", headers=admin_headers)
    assert detail.status_code == 200 and len(detail.json()["settings"]["audience"]) == 500

    outline = client.get(f"/api/v1/courses/{ai_course.id}/outline", headers=admin_headers).json()
    put = client.put(f"/api/v1/courses/{ai_course.id}/outline", headers=admin_headers, json=outline)
    assert put.status_code == 422  # what the admin approves is validated like any other input
    assert client.get(f"/api/v1/courses/{ai_course.id}", headers=admin_headers).status_code == 200


def test_the_ai_never_rewrites_a_module_with_its_own_content(client, db, admin_headers, admin, worker):
    module = _manual_module(db, admin, "Política", "Texto escrito a mano. " * 20)

    regenerate = client.post(f"/api/v1/modules/{module.id}/storyboard/regenerate", headers=admin_headers, json={})
    draft = client.post(f"/api/v1/courses/{module.course_id}/draft", headers=admin_headers, json={"module_ids": [module.id]})

    assert regenerate.status_code == 409 and draft.status_code == 409
    assert worker() == 0
    db.refresh(module)
    assert module.content_text.startswith("Texto escrito a mano.")


def test_a_video_attached_while_drafting_is_kept(client, db, admin_headers, ai_course, worker):
    module = Module(course_id=ai_course.id, title="Casco", order=1, source="ai", storyboard={"scenes": []})
    db.add(module)
    db.commit()
    client.post(f"/api/v1/modules/{module.id}/storyboard/regenerate", headers=admin_headers, json={})
    module.source, module.generation_status, module.content_text = "upload", "completed", "Transcripción"
    db.commit()

    worker()

    db.refresh(module)
    assert (module.source, module.generation_status, module.content_text) == ("upload", "completed", "Transcripción")


def test_new_instructions_are_not_lost_while_a_generation_runs(client, db, admin_headers, ai_course):
    module = Module(course_id=ai_course.id, title="Casco", order=1, source="ai", storyboard={"scenes": []})
    db.add(module)
    db.commit()
    url = f"/api/v1/modules/{module.id}/storyboard/regenerate"
    first = client.post(url, headers=admin_headers, json={})
    assert client.post(url, headers=admin_headers, json={}).json()["id"] == first.json()["id"]  # a double click
    assert client.post(url, headers=admin_headers, json={"feedback": "Más corto"}).status_code == 409

    outline_url = f"/api/v1/courses/{ai_course.id}/outline/generate"
    client.post(outline_url, headers=admin_headers, json={"brief": "A"})
    assert client.post(outline_url, headers=admin_headers, json={"brief": "Otro brief"}).status_code == 409


def test_modules_being_drafted_are_not_replaced_or_edited(client, db, admin_headers, ai_course, worker):
    course = _approved_course(client, admin_headers, ai_course.id, worker)
    outline = client.get(f"/api/v1/courses/{ai_course.id}/outline", headers=admin_headers).json()
    client.post(f"/api/v1/courses/{ai_course.id}/draft", headers=admin_headers, json={})

    assert client.put(f"/api/v1/courses/{ai_course.id}/outline", headers=admin_headers, json=outline).status_code == 409
    scene = {"id": "s1", "slide": {"layout": "cover", "title": "A mano"}, "narration": "Hola"}
    module_id = course["modules"][0]["id"]
    edit = client.put(f"/api/v1/modules/{module_id}/storyboard", headers=admin_headers, json={"scenes": [scene]})
    assert edit.status_code == 409


def test_the_ai_storyboard_always_fits_the_editor(client, db, admin_headers, ai_course, worker, monkeypatch):
    real_module = FakeLLM._module

    def oversized(self, prompt):
        draft = real_module(self, prompt)
        scene = draft.scenes[1].model_copy(update={"title": "t" * 900, "narration": "palabra " * 900})
        return draft.model_copy(update={"scenes": [scene] * 45, "reading_summary": "Lee ![x](https://e.example/?q=1)"})

    monkeypatch.setattr(FakeLLM, "_module", oversized)
    module = Module(course_id=ai_course.id, title="Casco", order=1, source="ai", storyboard={"scenes": []})
    db.add(module)
    db.commit()
    client.post(f"/api/v1/modules/{module.id}/storyboard/regenerate", headers=admin_headers, json={})
    worker()

    storyboard = client.get(f"/api/v1/modules/{module.id}/storyboard", headers=admin_headers)
    assert storyboard.status_code == 200 and len(storyboard.json()["scenes"]) == designer.MAX_STORYBOARD_SCENES
    assert client.put(f"/api/v1/modules/{module.id}/storyboard", headers=admin_headers, json=storyboard.json()).status_code == 200
    db.refresh(module)
    assert module.content_text == "Lee x"  # no URL a document chose ends up loaded by learners' browsers


def test_reading_markdown_keeps_text_and_drops_what_loads_urls():
    markdown = "- Presión < 2 bar o > 5 bar\n- Ver [la guía](https://x.example) <img src=x onerror=alert(1)> <https://x.example>"
    assert designer.plain_markdown(markdown) == "- Presión < 2 bar o > 5 bar\n- Ver la guía"


def test_feedback_is_sent_with_the_version_it_refers_to(client, db, admin_headers, ai_course, worker, monkeypatch):
    course = _approved_course(client, admin_headers, ai_course.id, worker)
    module_id = course["modules"][0]["id"]
    client.patch(f"/api/v1/modules/{module_id}", headers=admin_headers, json={"title": "Título nuevo"})
    client.post(f"/api/v1/courses/{ai_course.id}/draft", headers=admin_headers, json={})
    worker()
    prompts = []
    real_structured = FakeLLM.structured
    monkeypatch.setattr(FakeLLM, "structured", lambda self, system, user, *a, **k: prompts.append(user) or real_structured(self, system, user, *a, **k))

    client.post(f"/api/v1/modules/{module_id}/storyboard/regenerate", headers=admin_headers, json={"feedback": "Más corto"})
    worker()

    assert "VERSIÓN ANTERIOR" in prompts[0] and "Empecemos por las ideas clave." in prompts[0]  # the previous narration
    assert "MÓDULO 1: Título nuevo" in prompts[0]  # the module's current title, not the outline's


class _FailingCompletions:
    def __init__(self, error):
        self.error = error

    def parse(self, **kwargs):
        raise self.error


def _http_error(error_class, status_code):
    response = httpx.Response(status_code, request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions"))
    return error_class("error", response=response, body=None)


@pytest.mark.parametrize(
    ("error", "permanent"),
    [
        (openai.APIConnectionError(request=httpx.Request("POST", "https://api.openai.com")), False),
        (_http_error(openai.RateLimitError, 429), False),
        (_http_error(openai.InternalServerError, 500), False),
        (_http_error(openai.ConflictError, 409), False),
        (_http_error(openai.BadRequestError, 400), True),
        (_http_error(openai.AuthenticationError, 401), True),
    ],
)
def test_provider_errors_say_whether_a_retry_can_help(error, permanent):
    llm = OpenAILLM("sk-test", "gpt-4o")
    llm.client = type("Client", (), {"chat": type("Chat", (), {"completions": _FailingCompletions(error)})()})()
    with pytest.raises(LLMError) as raised:
        llm.structured("s", "u", designer.QuizDraft)
    assert raised.value.permanent is permanent


def test_ai_jobs_do_not_retry_permanent_llm_errors(client, db, admin_headers, admin, worker, monkeypatch):
    module = _manual_module(db, admin, "Casco", "El casco se usa siempre en la planta. " * 10)

    def refuse(self, *args, **kwargs):
        raise LLMError("El modelo no pudo generar este contenido.", permanent=True)

    monkeypatch.setattr(FakeLLM, "structured", refuse)
    job = client.post(f"/api/v1/modules/{module.id}/evaluation/generate", headers=admin_headers, json={}).json()
    worker()
    assert db.get(Job, job["id"]).status == "failed"
