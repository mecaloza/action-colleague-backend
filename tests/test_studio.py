"""AI studio with the offline fake LLM: outline -> modules -> storyboards, reading and quizzes."""

import pytest

from app.db.models import Course, Evaluation, MediaAsset, Module
from app.services.slides.render import BUBBLE_BOX, WIDTH, render
from app.services.slides.spec import ComparisonColumn, Slide, SlideContext
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

    _outline(client, admin_headers, ai_course.id, brief="Seguridad con el casco", minutes=15, tone="Cercano")
    worker()

    settings = client.get(url, headers=admin_headers).json()["settings"]
    assert (settings["brief"], settings["minutes"], settings["tone"]) == ("Seguridad con el casco", 15, "Cercano")


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
    assert failed["status"] in ("failed", "queued") and "suficiente contenido" in failed["error"]


def test_capabilities_and_slide_preview(client, admin_headers):
    caps = client.get("/api/v1/studio/capabilities", headers=admin_headers).json()
    assert caps["ai"] is True

    png = client.post(
        "/api/v1/slides/preview", headers=admin_headers,
        json={"slide": {"layout": "bullets", "title": "Hola", "points": ["Uno", "Dos"]}, "context": {"presenter": True}},
    )
    assert png.status_code == 200 and png.headers["content-type"] == "image/png"
    assert png.content[:4] == b"\x89PNG"


LONG = "Usar siempre el equipo de protección personal completo antes de entrar a la zona de producción " * 3
LAYOUTS = [
    Slide(layout="cover", title=LONG, subtitle=LONG),
    Slide(layout="bullets", title=LONG, points=[LONG] * 5),
    Slide(layout="statement", title=LONG, quote_author=LONG),
    Slide(layout="stat", stat_value="1.234.567,89 %", stat_label=LONG, subtitle=LONG),
    Slide(layout="steps", title=LONG, points=[LONG] * 5),
    Slide(layout="comparison", title=LONG, left=ComparisonColumn(heading=LONG, points=[LONG] * 4),
          right=ComparisonColumn(heading=LONG, points=[LONG] * 4)),
    Slide(layout="closing", title=LONG, points=[LONG] * 5),
]


GRADIENT_SPREAD = 25  # the background gradient alone varies less than this (0-255 gray levels)


def _brightness_spread(image, box):
    """Darkest-to-brightest distance inside the box: small only where nothing but the background is drawn."""
    darkest, brightest = image.crop(box).convert("L").getextrema()
    return brightest - darkest


@pytest.mark.parametrize("slide", LAYOUTS, ids=[s.layout for s in LAYOUTS])
@pytest.mark.parametrize("theme", ["dark", "light"])
def test_every_layout_keeps_long_text_inside_and_away_from_the_presenter(slide, theme):
    image = render(slide, SlideContext(course_title=LONG, module_label="Módulo 1", presenter=True, theme=theme))
    assert image.size == (1920, 1080)
    # The presenter bubble's corner stays empty: only the background gradient is there.
    corner = _brightness_spread(image, BUBBLE_BOX)
    assert corner < GRADIENT_SPREAD, f"something was drawn where the presenter goes (spread {corner})"
    # Nothing touches the right edge (text never overflows the slide).
    assert _brightness_spread(image, (WIDTH - 40, 0, WIDTH, image.height)) < GRADIENT_SPREAD
