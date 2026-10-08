"""Slides without cut texts or overlaps: the renderer's fixes, and the layout lint that tells the admin."""

import shutil
from dataclasses import replace
from datetime import datetime, timezone
from io import BytesIO

import pytest
from PIL import Image, ImageChops, ImageDraw

from app.db.models import Course, Job, Module
from app.services.slides import lint
from app.services.slides.render import (
    BODY_FONT,
    BRAND_ORANGE,
    BUBBLE_BOX,
    LAYOUTS,
    MIN_BAR_ROOM,
    fit,
    font,
    part,
    render,
    render_png,
)
from app.services.slides.spec import MAX_LABEL_CHARS, MAX_TEXT_CHARS, ComparisonColumn, Slide, SlideContext
from app.services.video.visuals import FakeVisuals
from tests.conftest import auth_headers

ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is not installed")

PRESENTER = SlideContext(index=3, total=8, presenter=True, course_title="Asesoría técnica", module_label="Módulo 2")
NO_PRESENTER = replace(PRESENTER, presenter=False)  # the video's fallback when the presenter can't be made
TWO_LINE_TITLE = "Cómo leer el código DOT de una llanta y calcular su edad real"  # its body starts lower
LONG_POINT = "Revisa el DOT en el costado de la llanta y compara la semana y el año de fabricación con la fecha de hoy"
# The longest texts a slide accepts, with and without spaces.
LONG = ("Usar siempre el equipo de protección personal completo antes de entrar a la zona de producción " * 4)[
    :MAX_TEXT_CHARS
]
URL = ("https://intranet.empresa.com/seguridad/procedimientos/uso-obligatorio-del-casco" * 4)[:MAX_TEXT_CHARS]
ONE_URL = "https://intranet.empresa.com/seguridad/procedimientos/uso-obligatorio-del-casco-en-planta"


@pytest.fixture(autouse=True)
def fresh_caches():
    """The lint keeps what it found per slide, and the preview the pictures it decoded: every test starts clean
    (one swaps a layout, and every test's course has the same id, so the same picture paths)."""
    from app.api.routes import studio as studio_routes

    lint._slide_findings.cache_clear()
    studio_routes._PICTURES.clear()
    yield
    lint._slide_findings.cache_clear()


def _changed(a: Image.Image, b: Image.Image, box) -> bool:
    return ImageChops.difference(a.crop(box), b.crop(box)).getbbox() is not None


def _recorded(slide: Slide, context: SlideContext) -> lint.Recorder:
    recorder = lint.Recorder()
    render(slide, context, draw_with=recorder)
    return recorder


def _boxes(recorder: lint.Recorder, field: str, kind: str = "text") -> list:
    return [element.box for element in recorder.elements if element.part == field and element.kind == kind]


# ── The renderer's fixes ──────────────────────────────────────────────


def test_an_infographic_is_shown_as_drawn_without_darkened_edges():
    white = Image.new("RGB", (1536, 1024), (255, 255, 255))  # its own title at the top, its notes at the bottom
    png = render_png(Slide(layout="visual", title="Presión"), PRESENTER, backdrop=white, fit="contain")
    shown = Image.open(BytesIO(png)).convert("L")
    for band in ((300, 0, 1600, 60), (300, 1020, 1600, 1080)):  # they were dimmed to about half
        assert shown.crop(band).getextrema()[0] >= 250


def test_chart_values_stay_clear_of_the_presenter_and_the_largest_bar_keeps_its_length():
    slide = Slide(layout="chart", title="Costo por km", chart_labels=["Llanta nueva premium", "Reencauche"],
                  chart_values=[1234567.891, 345678.0], chart_unit="MXN por kilómetro recorrido en carretera")
    image = render(slide, PRESENTER)
    drawn = ImageChops.difference(image, render(Slide(layout="chart", title=slide.title), PRESENTER)).getbbox()
    assert drawn[2] <= BUBBLE_BOX[0] - 56  # the values ran past the slide's edge, on the presenter's side

    [largest] = _boxes(_recorded(slide, PRESENTER), "chart_values.0", "shape")
    assert largest[2] - largest[0] >= MIN_BAR_ROOM  # it collapsed to 8 px


def test_a_long_calculation_result_keeps_its_label_clear_of_the_presenter():
    slide = Slide(layout="calculation", title=TWO_LINE_TITLE,
                  points=["Opción A: 9 500 MXN ÷ 180 000 km = 0,053 MXN/km"] * 5,
                  stat_value="3 600 MXN ahorrados por llanta y año",
                  stat_label="de ahorro por posición al año en toda la flota")
    image = render(slide, PRESENTER)
    empty = render(Slide(layout="calculation", title=TWO_LINE_TITLE), PRESENTER)
    assert not _changed(image, empty, BUBBLE_BOX)  # the label went inside the bubble
    assert ImageChops.difference(image, empty).getbbox()[2] <= BUBBLE_BOX[0] - 56
    assert not [finding for finding in lint.check_slide(slide, PRESENTER)]  # the whole result shows, on two lines


@pytest.mark.parametrize("slide", [
    Slide(layout="bullets", title=TWO_LINE_TITLE, points=[LONG_POINT, "Dos", "Tres"]),
    Slide(layout="steps", title=TWO_LINE_TITLE, points=[LONG_POINT, "Dos", "Tres"]),
    Slide(layout="comparison", title=TWO_LINE_TITLE, left=ComparisonColumn(heading="Correcto", points=[LONG_POINT]),
          right=ComparisonColumn(heading="Lo que nunca debes hacer en la ruta", points=[LONG_POINT])),
    Slide(layout="case", title=TWO_LINE_TITLE, points=[LONG_POINT] * 4),
    Slide(layout="chart", title=TWO_LINE_TITLE, chart_labels=["Correcta", "Baja"], chart_values=[10, 5]),
    Slide(layout="calculation", title=TWO_LINE_TITLE, points=[LONG_POINT] * 3, stat_value="$1 650",
          stat_label="de ahorro"),
], ids=lambda slide: slide.layout)
def test_without_the_presenter_nothing_runs_into_the_corner_motif(slide):
    # A two-line title starts the body beside the motif's small orange mark: the body may not use the full width.
    image = render(slide, NO_PRESENTER)
    frame = _recorded(Slide(layout=slide.layout, title=slide.title), NO_PRESENTER)
    motif = [element.box for element in frame.elements if element.part == "motif"]
    corner = (min(b[0] for b in motif), min(b[1] for b in motif), max(b[2] for b in motif), max(b[3] for b in motif))
    assert not _changed(image, render(Slide(layout=slide.layout, title=slide.title), NO_PRESENTER), corner)


def test_the_last_case_card_uses_the_size_of_the_others():
    text = "Una flota de 40 tractocamiones con desgaste irregular en los hombros por baja presión sostenida"
    recorder = _recorded(Slide(layout="case", title="Caso", points=[text] * 4), PRESENTER)
    assert len({recorder.fits[f"points.{index}"].font.size for index in range(4)}) == 1  # the 4th was smaller


def test_a_stat_label_starts_below_the_comma_of_its_value():
    slide = Slide(layout="stat", stat_value="87,5 %", stat_label="de las fallas se deben a una presión baja")
    recorder = _recorded(slide, PRESENTER)
    value_bottom = max(box[3] for box in _boxes(recorder, "stat_value"))
    assert value_bottom < min(box[1] for box in _boxes(recorder, "stat_label"))  # the comma ran into the label


def test_fit_says_how_it_made_the_text_fit():
    draw = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    roomy = fit(draw, "Casco", BODY_FONT, 500, 800, 200, 52, 30, max_lines=2)
    crammed = fit(draw, LONG, BODY_FONT, 500, 800, 200, 52, 30, max_lines=2)
    broken = fit(draw, ONE_URL, BODY_FONT, 500, 800, 200, 52, 30, max_lines=3)
    assert (roomy.at_min_size, roomy.broken_word, roomy.truncated) == (False, False, False)
    assert crammed.truncated and crammed.at_min_size and crammed.lines[-1].endswith("…")
    assert broken.broken_word and not broken.truncated


# ── The lint ──────────────────────────────────────────────────────────

EDGE_SLIDES = [
    Slide(layout="cover", title=LONG, subtitle=LONG),
    Slide(layout="cover", title=URL, subtitle=URL),
    Slide(layout="bullets", title=LONG, points=[LONG] * 5),
    Slide(layout="bullets", title=URL, points=[URL] * 5),
    Slide(layout="bullets", title=TWO_LINE_TITLE, points=[LONG_POINT, "Dos", "Tres"], icons=["hard-hat", "", "hand"]),
    Slide(layout="statement", title=LONG, quote_author=LONG[:60], icon="hard-hat"),
    Slide(layout="stat", stat_value="1.234.567,89 %", stat_label=LONG, subtitle=LONG, icon="hard-hat"),
    Slide(layout="stat", stat_value="Treinta y cinco por ciento menos", stat_label="de costo por km", subtitle="Fuente"),
    Slide(layout="steps", title=LONG, points=[LONG] * 5),
    Slide(layout="steps", title=TWO_LINE_TITLE, points=[LONG_POINT] * 5),
    Slide(layout="comparison", title=LONG, left=ComparisonColumn(heading=LONG[:MAX_LABEL_CHARS], points=[LONG] * 4),
          right=ComparisonColumn(heading=URL[:MAX_LABEL_CHARS], points=[URL] * 4)),
    Slide(layout="closing", title=LONG, points=[LONG] * 5),
    Slide(layout="chart", title=TWO_LINE_TITLE, chart_labels=[LONG[:MAX_LABEL_CHARS]] * 6,
          chart_values=[1e12, -5, 3.14159, 0, 7, 8], chart_unit=LONG[:MAX_LABEL_CHARS]),
    Slide(layout="calculation", title=LONG, points=[LONG] * 5, stat_value=LONG[:MAX_LABEL_CHARS], stat_label=LONG),
    Slide(layout="case", title=LONG, points=[LONG] * 4),
    Slide(layout="visual", title=LONG),
]


@pytest.mark.parametrize("slide", EDGE_SLIDES, ids=[f"{s.layout}-{i}" for i, s in enumerate(EDGE_SLIDES)])
def test_even_the_longest_texts_never_overlap_anything(slide):
    contexts = [PRESENTER, NO_PRESENTER, replace(PRESENTER, theme="light"), replace(PRESENTER, backdrop=True)]
    if slide.layout in ("cover", "closing"):
        contexts.append(replace(NO_PRESENTER, hero=True))
    for context in contexts:
        findings = lint.check_slide(slide, context)
        assert all(finding.code in ("text_truncated", "word_broken") for finding in findings), (context, findings)


def test_a_text_that_does_not_fit_is_reported_on_the_field_the_admin_edits():
    slide = Slide(layout="bullets", title="Equipo", points=["Casco", " ", LONG])  # the blank point isn't shown
    assert lint.check_slide(slide, PRESENTER) == [
        lint.Finding("text_truncated", "error", "points.2", "La viñeta 3 no cabe y se corta. Usa un texto más corto.")
    ]
    chart = Slide(layout="chart", title="Costo", chart_labels=["A"], chart_values=[5], chart_unit=LONG[:MAX_LABEL_CHARS])
    assert [(f.field, f.message) for f in lint.check_slide(chart, PRESENTER)] == [
        ("chart_values.0", "El valor de la barra 1 no cabe y se corta. Usa una unidad más corta.")
    ]
    case = Slide(layout="case", title="Caso", points=["Flota de reparto", "Hombros gastados", LONG, "Más km"])
    assert [(f.field, f.message) for f in lint.check_slide(case, PRESENTER)] == [
        ("points.2", "La solución no cabe y se corta. Usa un texto más corto.")
    ]


def test_a_word_too_long_for_its_box_is_only_a_warning():
    slide = Slide(layout="comparison", title="Dónde consultarlo",
                  left=ComparisonColumn(heading="Antes", points=["En papel"]),
                  right=ComparisonColumn(heading="Ahora", points=[" ", ONE_URL]))
    assert lint.check_slide(slide, PRESENTER) == [lint.Finding(
        "word_broken", "warning", "right.points.1",
        "El punto 2 de la columna derecha tiene una palabra que no cabe en una línea y se parte.",
    )]


def _misplaced(*texts):
    """A layout that draws its texts where it says: the kind of bug the lint is there to catch."""
    def layout(slide, ctx, theme, draw):
        for field, xy, text in texts:
            with part(draw, field):
                draw.text(xy, text, font=font(BODY_FONT, 40, 500), fill=theme.text)
    return layout


@pytest.mark.parametrize("context, texts, code, message", [
    (PRESENTER, [("points.0", (1560, 800), "Casco")], "presenter_overlap", "La viñeta 1 queda debajo del presentador."),
    (replace(NO_PRESENTER, hero=True), [("points.0", (1300, 500), "Casco")], "presenter_overlap",
     "La viñeta 1 queda debajo del presentador."),
    (NO_PRESENTER, [("points.0", (1690, 360), "Casco")], "motif_overlap",
     "La viñeta 1 se encima con el logo de la esquina."),
    (NO_PRESENTER, [("points.0", (130, 50), "Casco")], "frame_overlap",
     "La viñeta 1 se encima con el encabezado de la diapositiva."),
    (NO_PRESENTER, [("points.0", (130, 1000), "Casco")], "frame_overlap",
     "La viñeta 1 se encima con el número de la diapositiva."),
    (NO_PRESENTER, [("points.0", (40, 500), "Casco")], "outside_safe_area",
     "La viñeta 1 se sale de los márgenes de la diapositiva."),
    (NO_PRESENTER, [("points.0", (300, 500), "Casco"), ("points.1", (330, 510), "Guantes")], "text_overlap",
     "La viñeta 1 se encima con la viñeta 2."),
], ids=["bubble", "hero", "motif", "header", "footer", "margins", "texts"])
def test_the_lint_catches_what_a_layout_draws_where_it_must_not(monkeypatch, context, texts, code, message):
    monkeypatch.setitem(LAYOUTS, "bullets", _misplaced(*texts))
    findings = lint.check_slide(Slide(layout="bullets", title="Equipo"), context)
    assert [(finding.code, finding.severity, finding.field, finding.message) for finding in findings] == [
        (code, "error", "points.0", message)
    ]


def _infographic(covered: bool) -> Image.Image:
    picture = Image.new("RGB", (1536, 1024), (17, 17, 17))
    draw = ImageDraw.Draw(picture)
    draw.rectangle([90, 200, 1000, 580], outline=BRAND_ORANGE, width=10)
    if covered:  # the image model ignored the corner the prompt keeps for the presenter
        draw.rectangle([1250, 700, 1500, 960], outline=(255, 255, 255), width=12)
    return picture


def test_an_infographic_the_presenter_would_cover_gets_a_warning():
    slide = Slide(layout="visual", title="Presión y desgaste")
    context = replace(PRESENTER, backdrop=True)
    assert lint.scene_warnings("s2", slide, context, _infographic(covered=False)) == []
    assert lint.scene_warnings("s2", slide, context, _infographic(covered=True)) == [{
        "scene_id": "s2", "code": "visual_under_presenter", "severity": "warning", "field": "visual",
        "message": "El presentador tapa parte de la infografía. Usa «Otra versión» de la infografía.",
    }]
    assert lint.scene_warnings("s2", slide, replace(context, presenter=False), _infographic(covered=True)) == []
    preview_copy = _infographic(covered=True)
    preview_copy.thumbnail((960, 960))  # the editor's preview keeps a smaller copy
    assert lint.presenter_share(preview_copy) > lint.MAX_PRESENTER_SHARE
    assert lint.presenter_share(Image.new("RGB", (1536, 1024), (240, 240, 240))) == 0  # a flat color hides nothing


# ── API ───────────────────────────────────────────────────────────────

SCENES = [
    {"id": "s1", "slide": {"layout": "cover", "title": "Seguridad en planta", "subtitle": "Lo esencial"},
     "narration": "Bienvenida al módulo sobre el uso correcto del casco en la planta."},
    {"id": "s2", "slide": {"layout": "bullets", "title": "Reglas", "points": ["Siempre puesto", "Revisarlo"]},
     "narration": "Hay dos reglas: llevarlo siempre puesto y revisarlo antes de cada turno."},
]
TOO_LONG = {**SCENES[1], "slide": {**SCENES[1]["slide"], "points": ["Siempre puesto", LONG]}}
TOO_LONG_WARNING = {"scene_id": "s2", "code": "text_truncated", "severity": "error", "field": "points.1",
                    "message": "La viñeta 2 no cabe y se corta. Usa un texto más corto."}
INFOGRAPHIC_SCENE = {"id": "s2", "slide": {"layout": "visual", "title": "Presión y desgaste"},
                     "narration": "Mira las tres huellas: baja, correcta y alta presión.",
                     "visual": {"kind": "infographic", "query": "", "prompt": "Infografía: presión y desgaste."}}


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


def test_the_editor_lints_its_draft_without_saving_it(client, db, admin_headers, module):
    url = f"/api/v1/modules/{module.id}/storyboard"
    stored = client.get(url, headers=admin_headers).json()
    assert stored["warnings"] == []

    draft = [SCENES[0], {**TOO_LONG, "narration": ""}]  # being edited: a narration may still be empty
    response = client.post(f"{url}/lint", headers=admin_headers, json={"scenes": draft})
    assert response.status_code == 200, response.text
    assert response.json() == {"warnings": [TOO_LONG_WARNING]}

    assert client.get(url, headers=admin_headers).json() == stored  # nothing was saved
    assert db.query(Job).count() == 0  # no visual nor anything else asked for
    huge = {"scenes": [{**SCENES[1], "slide": {"layout": "bullets", "points": ["p"] * 1000}}]}
    assert client.post(f"{url}/lint", headers=admin_headers, json=huge).status_code == 422


def test_a_saved_script_comes_back_with_its_warnings(client, admin_headers, module):
    url = f"/api/v1/modules/{module.id}/storyboard"
    saved = client.put(url, headers=admin_headers, json={"scenes": [SCENES[0], TOO_LONG]})
    assert saved.status_code == 200, saved.text
    assert saved.json()["warnings"] == [TOO_LONG_WARNING]
    assert client.get(url, headers=admin_headers).json() == saved.json()


def test_a_lint_that_fails_never_keeps_the_admin_from_the_script(client, admin_headers, module, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("lint bug")

    monkeypatch.setattr(lint, "scene_warnings", broken)
    url = f"/api/v1/modules/{module.id}/storyboard"
    saved = client.put(url, headers=admin_headers, json={"scenes": [SCENES[0], TOO_LONG]})
    assert saved.status_code == 200 and saved.json()["warnings"] == []
    opened = client.get(url, headers=admin_headers)
    assert opened.status_code == 200 and opened.json()["scenes"][1]["slide"]["points"][1] == LONG


def test_an_infographic_already_made_is_checked_under_the_course_s_presenter(
    client, db, admin_headers, module, storage, worker, monkeypatch
):
    made = []

    def covered(self, prompt, dest):
        made.append(prompt)
        dest = dest.with_suffix(".png")
        _infographic(covered=True).save(dest)
        return dest

    monkeypatch.setattr(FakeVisuals, "infographic", covered)
    url = f"/api/v1/modules/{module.id}/storyboard"
    saved = client.put(url, headers=admin_headers, json={"scenes": [SCENES[0], INFOGRAPHIC_SCENE]}).json()
    assert saved["warnings"] == [] and made == []  # not made yet: nothing to check, and the lint never makes it

    worker()  # the visuals.prepare job the save queued
    [warning] = client.get(url, headers=admin_headers).json()["warnings"]
    assert (warning["scene_id"], warning["code"], warning["field"]) == ("s2", "visual_under_presenter", "visual")
    lint_response = client.post(f"{url}/lint", headers=admin_headers, json={"scenes": [SCENES[0], INFOGRAPHIC_SCENE]})
    assert lint_response.json()["warnings"] == [warning] and len(made) == 1

    course = db.get(Course, module.course_id)
    course.settings = {"presenter": False}  # the course's videos have no bubble to cover it
    db.commit()
    assert client.get(url, headers=admin_headers).json()["warnings"] == []


def _render(client, db, headers, module, worker) -> Job:
    response = client.post(f"/api/v1/courses/{module.course_id}/render", headers=headers,
                           json={"voice_id": "fake-voice-es", "presenter": False})
    assert response.status_code == 202, response.text
    for _ in range(6):
        db.query(Job).filter(Job.status == "queued").update(
            {Job.run_after: datetime(2000, 1, 1, tzinfo=timezone.utc)}, synchronize_session=False
        )
        db.commit()
        worker()
        if not db.query(Job).filter(Job.status == "queued").count():
            break
    db.expire_all()
    return db.query(Job).filter(Job.type == "video.render").one()


@ffmpeg
def test_the_render_keeps_its_slides_lint_for_the_video_review(client, db, admin_headers, module, storage, worker):
    module.storyboard = {"scenes": [SCENES[0], TOO_LONG]}
    db.commit()
    job = _render(client, db, admin_headers, module, worker)
    assert db.get(Module, module.id).generation_status == "completed"
    assert job.state["lint"] == {"warnings": [TOO_LONG_WARNING]}


@ffmpeg
def test_a_lint_that_fails_never_costs_the_video(client, db, admin_headers, module, storage, worker, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("lint bug")

    monkeypatch.setattr(lint, "scene_warnings", broken)
    job = _render(client, db, admin_headers, module, worker)
    assert db.get(Module, module.id).generation_status == "completed"
    assert job.state["lint"] == {"warnings": []}
