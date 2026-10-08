"""
Layout lint: what doesn't fit or overlaps on a slide, told to the admin before a video is paid for.

The slide is drawn as the video draws it (with the widest header and page counter: `LINT_FRAME`), with a drawing
handle (`Recorder`) that notes the box of every text and shape and the part of the slide it belongs to
(`render.part`); `fit` says when a text had to be cut. What is reported, per field of the slide:

  text_truncated          error    a text doesn't fit even at its smallest size: its end is cut
  word_broken             warning  a word wider than its box is split across lines
  presenter_overlap       error    under the presenter: its bubble, or large on the cover and the closing
  motif_overlap           error    over the brand motif in the corner
  frame_overlap           error    over the header or the page counter
  outside_safe_area       error    past the slide's margins
  text_overlap            error    two texts on top of each other
  visual_under_presenter  warning  the bubble covers drawings of the scene's infographic (another version helps)
"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from functools import lru_cache
from math import ceil, floor, pi
from typing import Literal

from PIL import Image, ImageChops, ImageDraw, ImageStat

from app.services.slides.render import (
    BUBBLE_BOX,
    BUBBLE_SIZE,
    FOOTER,
    FRAME_PARTS,
    HEADER,
    HEIGHT,
    HERO_BOX,
    MARGIN_BOTTOM,
    MARGIN_TOP,
    MARGIN_X,
    MOTIF,
    WIDTH,
    FittedText,
    render,
)
from app.services.slides.spec import Slide, SlideContext

Box = tuple[float, float, float, float]  # left, top, right, bottom (slide pixels)
LintCode = Literal[
    "text_truncated", "word_broken", "presenter_overlap", "motif_overlap", "frame_overlap", "outside_safe_area",
    "text_overlap", "visual_under_presenter",
]
Severity = Literal["error", "warning"]

SEVERITY: dict[str, Severity] = {
    "text_truncated": "error",
    "word_broken": "warning",
    "presenter_overlap": "error",
    "motif_overlap": "error",
    "frame_overlap": "error",
    "outside_safe_area": "error",
    "text_overlap": "error",
    "visual_under_presenter": "warning",
}
SAFE_AREA = (MARGIN_X, MARGIN_TOP, WIDTH - MARGIN_X, HEIGHT - MARGIN_BOTTOM)
SAFE_TOLERANCE = 12  # glyphs' side bearings, and the large stat's nudge 6 px left of the margin
PRESENTER_CLEARANCE = 8  # around the presenter's circle (its ring is inside it): nothing may touch it
MIN_TEXT_OVERLAP = 3  # px both ways: ink boxes that only touch are not two texts on top of each other
MAX_PRESENTER_SHARE = 0.02  # of the bubble over an infographic's drawings: more hides part of what it explains
CONTENT_CONTRAST = 48  # how far (0-255, in any channel) a pixel is from the background to be part of a drawing
# The header and the page counter only change the frame, and the content never goes where they are: every slide is
# checked with the widest of them, so a scene moved, added or removed doesn't draw the rest of the script again.
LINT_FRAME = {"course_title": "W" * 70, "module_label": "", "index": 99, "total": 99}


@dataclass(frozen=True)
class Element:
    kind: Literal["text", "shape"]
    box: Box
    part: str | None  # the slide's field or the piece of the frame it belongs to; None: a rule, a divider...


@dataclass(frozen=True)
class Finding:
    code: LintCode
    severity: Severity
    field: str | None  # the slide's field the admin edits ("title", "points.2"...); "visual" for the picture
    message: str


# ── Recording what is drawn ───────────────────────────────────────────


def _points(xy) -> list[tuple[float, float]]:
    """Pillow's coordinates, flat ([x0, y0, x1, y1...]) or in pairs ([(x0, y0), ...]), as pairs."""
    values = list(xy)
    if values and isinstance(values[0], (int, float)):
        return list(zip(values[::2], values[1::2]))
    return [(x, y) for x, y in values]


def _bounds(points: list[tuple[float, float]], pad: float = 0) -> Box:
    xs, ys = [x for x, _ in points], [y for _, y in points]
    return min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad


class Recorder:
    """A drawing handle that draws like Pillow's and notes the box of everything it draws, with the part of the
    slide it belongs to: `render(slide, ctx, draw_with=recorder)`."""

    def __init__(self) -> None:
        self.elements: list[Element] = []
        self.fits: dict[str, FittedText] = {}  # how each field's text was fitted
        self._draw: ImageDraw.ImageDraw | None = None
        self._part: str | None = None

    def __call__(self, image: Image.Image) -> "Recorder":
        self._draw = ImageDraw.Draw(image)
        return self

    def __getattr__(self, name: str):  # measuring (textlength...): the real handle
        if name.startswith("_"):  # never this proxy's own state (a copy would recurse looking for `_draw`)
            raise AttributeError(name)
        return getattr(self._draw, name)

    @contextmanager
    def tag(self, name: str, fitted: FittedText | None) -> Iterator[None]:
        """See `render.part`."""
        outer, self._part = self._part, name
        if fitted is not None:
            self.fits[name] = fitted
        try:
            yield
        finally:
            self._part = outer

    def _note(self, kind: Literal["text", "shape"], box: Box) -> None:
        self.elements.append(Element(kind, box, self._part))

    def text(self, xy, text: str, **options):
        if text.strip():
            self._note("text", self._draw.textbbox(xy, text, font=options.get("font"), anchor=options.get("anchor")))
        return self._draw.text(xy, text, **options)

    def line(self, xy, **options):
        points, half = _points(xy), options.get("width", 0) / 2
        for start, end in zip(points, points[1:]):  # segment by segment: a diagonal keeps a tight box
            self._note("shape", _bounds([start, end], half))
        return self._draw.line(xy, **options)

    def _shape(self, method: str, xy, options: dict):
        self._note("shape", _bounds(_points(xy)))
        return getattr(self._draw, method)(xy, **options)

    def polygon(self, xy, **options):
        return self._shape("polygon", xy, options)

    def rectangle(self, xy, **options):
        return self._shape("rectangle", xy, options)

    def rounded_rectangle(self, xy, **options):
        return self._shape("rounded_rectangle", xy, options)

    def ellipse(self, xy, **options):
        return self._shape("ellipse", xy, options)


# ── Checks ────────────────────────────────────────────────────────────


def _presenter_circle(ctx: SlideContext) -> tuple[float, float, float] | None:
    """Center and radius (with its clearance) of the presenter on this slide, if it is on it."""
    if ctx.hero:
        box = HERO_BOX
    elif ctx.presenter:
        box = BUBBLE_BOX
    else:
        return None
    return (box[0] + box[2]) / 2, (box[1] + box[3]) / 2, (box[2] - box[0]) / 2 + PRESENTER_CLEARANCE


def _hits_circle(box: Box, circle: tuple[float, float, float]) -> bool:
    cx, cy, radius = circle
    nearest_x, nearest_y = min(max(cx, box[0]), box[2]), min(max(cy, box[1]), box[3])
    return (nearest_x - cx) ** 2 + (nearest_y - cy) ** 2 < radius**2


def _overlap(a: Box, b: Box) -> tuple[float, float]:
    """How far two boxes overlap, across and down (negative: they don't)."""
    return min(a[2], b[2]) - max(a[0], b[0]), min(a[3], b[3]) - max(a[1], b[1])


def _touches(box: Box, others: list[Box]) -> bool:
    return any(min(_overlap(box, other)) > 0 for other in others)


def _inside(box: Box, area: Box, tolerance: float) -> bool:
    return (box[0] >= area[0] - tolerance and box[1] >= area[1] - tolerance
            and box[2] <= area[2] + tolerance and box[3] <= area[3] + tolerance)


def _check(slide: Slide, ctx: SlideContext, recorder: Recorder) -> list[Finding]:
    found: dict[tuple[str, str | None], Finding] = {}

    def report(code: LintCode, field: str | None, **words: str) -> None:
        if (code, field) not in found:  # once per field, however many of its lines
            found[code, field] = Finding(code, SEVERITY[code], field, _message(code, slide.layout, field, **words))

    for field, fitted in recorder.fits.items():
        if fitted.truncated:
            report("text_truncated", field)
        elif fitted.broken_word:
            report("word_broken", field)

    frame = {name: [element.box for element in recorder.elements if element.part == name] for name in FRAME_PARTS}
    content = [element for element in recorder.elements if element.part not in FRAME_PARTS]
    presenter = _presenter_circle(ctx)
    for element in content:
        if presenter and _hits_circle(element.box, presenter):
            report("presenter_overlap", element.part)
        elif _touches(element.box, frame[MOTIF]):
            report("motif_overlap", element.part)
        elif _touches(element.box, frame[HEADER]):
            report("frame_overlap", element.part, where="el encabezado de la diapositiva")
        elif _touches(element.box, frame[FOOTER]):
            report("frame_overlap", element.part, where="el número de la diapositiva")
        elif not _inside(element.box, SAFE_AREA, SAFE_TOLERANCE):
            report("outside_safe_area", element.part)

    # Lines of one text (and a point's marker) may touch: only texts of two different fields are compared.
    texts = [element for element in content if element.kind == "text" and element.part is not None]
    for index, first in enumerate(texts):
        for second in texts[index + 1:]:
            if first.part != second.part and min(_overlap(first.box, second.box)) >= MIN_TEXT_OVERLAP:
                report("text_overlap", first.part, other=_noun(slide.layout, second.part))
    return sorted(found.values(), key=lambda finding: finding.severity != "error")  # errors first, else in order


@lru_cache(maxsize=1024)
def _slide_findings(slide_json: str, ctx: SlideContext) -> tuple[Finding, ...]:
    """(Kept: the editor checks its whole draft after every change, and most of its slides didn't change, wherever
    they are in it now.)"""
    slide = Slide.model_validate_json(slide_json)
    recorder = Recorder()
    render(slide, ctx, draw_with=recorder)
    return tuple(_check(slide, ctx, recorder))


def check_slide(slide: Slide, ctx: SlideContext) -> list[Finding]:
    """What doesn't fit or overlaps on the slide, drawn whole (every beat on screen) as the video draws it."""
    return list(_slide_findings(slide.model_dump_json(), replace(ctx, reveal=None, **LINT_FRAME)))


def presenter_share(picture: Image.Image) -> float:
    """How much of the presenter's bubble (0-1) lies over drawings or text of an infographic placed whole on the
    slide, as the video shows it ("contain"). Works on a picture of any size (e.g. the preview's smaller copy)."""
    scale = min(WIDTH / picture.width, HEIGHT / picture.height)
    left, top = (WIDTH - picture.width * scale) / 2, (HEIGHT - picture.height * scale) / 2
    radius = BUBBLE_SIZE / 2 / scale  # in the picture's pixels
    cx = ((BUBBLE_BOX[0] + BUBBLE_BOX[2]) / 2 - left) / scale
    cy = ((BUBBLE_BOX[1] + BUBBLE_BOX[3]) / 2 - top) / scale
    box = (max(0, floor(cx - radius)), max(0, floor(cy - radius)),
           min(picture.width, ceil(cx + radius)), min(picture.height, ceil(cy + radius)))
    if box[0] >= box[2] or box[1] >= box[3]:
        return 0.0
    area = picture.convert("RGB").crop(box)
    circle = Image.new("L", area.size, 0)
    ImageDraw.Draw(circle).ellipse(
        [cx - radius - box[0], cy - radius - box[1], cx + radius - box[0], cy + radius - box[1]], fill=255
    )
    # The background is what most of the circle shows (dark, or not: any flat color covers nothing): a pixel far
    # from it in any channel belongs to a drawing, a label or an arrow.
    background = Image.new("RGB", area.size, tuple(round(value) for value in ImageStat.Stat(area, circle).median))
    red, green, blue = ImageChops.difference(area, background).split()
    distance = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    drawn = ImageChops.multiply(distance.point(lambda value: 255 if value > CONTENT_CONTRAST else 0), circle)
    return drawn.histogram()[255] / (pi * radius**2)


def check_infographic(covered: float) -> list[Finding]:
    """Whether the presenter's bubble hides part of the scene's infographic (`covered`: its `presenter_share`)."""
    if covered <= MAX_PRESENTER_SHARE:
        return []
    code: LintCode = "visual_under_presenter"
    return [Finding(code, SEVERITY[code], "visual", _message(code, "visual", "visual"))]


def scene_warnings(scene_id: str, slide: Slide, ctx: SlideContext, covered: float | None = None) -> list[dict]:
    """A scene's findings as the API reports them. `covered`: the `presenter_share` of the scene's infographic,
    already made, when it is shown with the presenter's bubble over it."""
    findings = check_slide(slide, ctx)
    if covered is not None and ctx.presenter and not ctx.hero:
        findings += check_infographic(covered)
    return [{"scene_id": scene_id, **asdict(finding)} for finding in findings]


# ── Messages ──────────────────────────────────────────────────────────

POINT_NOUNS = {"bullets": "la viñeta {n}", "steps": "el paso {n}", "calculation": "el paso {n}"}
CASE_NOUNS = ("la situación", "el diagnóstico", "la solución", "el resultado")  # its CASE_PARTS
FIELD_NOUNS = {  # the editor's names for each field, per layout ("*": any other)
    "title": {"statement": "la frase", "case": "el título del caso", "chart": "el título de la gráfica",
              "visual": "el tema de la infografía", "*": "el título"},
    "subtitle": {"stat": "el contexto", "*": "el subtítulo"},
    "stat_value": {"calculation": "el resultado", "*": "el dato"},
    "stat_label": {"calculation": "lo que significa el resultado", "*": "lo que significa el dato"},
    "quote_author": {"*": "el autor o la fuente"},
    "icon": {"*": "el ícono"},
    "visual": {"*": "la infografía"},
    "left.heading": {"*": "el encabezado izquierdo"},
    "right.heading": {"*": "el encabezado derecho"},
}
MESSAGES: dict[str, str] = {
    "text_truncated": "{noun} no cabe y se corta. {fix}",
    "word_broken": "{noun} tiene una palabra que no cabe en una línea y se parte.",
    "presenter_overlap": "{noun} queda debajo del presentador.",
    "motif_overlap": "{noun} se encima con el logo de la esquina.",
    "frame_overlap": "{noun} se encima con {where}.",
    "outside_safe_area": "{noun} se sale de los márgenes de la diapositiva.",
    "text_overlap": "{noun} se encima con {other}.",
    "visual_under_presenter": "El presentador tapa parte de la infografía. Usa «Otra versión» de la infografía.",
}


def _noun(layout: str, field: str | None) -> str:
    """How the editor calls the slide's field, with its article: "la viñeta 3"."""
    if field is None:
        return "un elemento de la diapositiva"
    if field in FIELD_NOUNS:
        names = FIELD_NOUNS[field]
        return names.get(layout, names["*"])
    name, _, index = field.rpartition(".")
    number = int(index) + 1 if index.isdigit() else 0
    if name == "points":
        if layout == "case" and 1 <= number <= len(CASE_NOUNS):
            return CASE_NOUNS[number - 1]
        return POINT_NOUNS.get(layout, "el punto {n}").format(n=number)
    if name in ("left.points", "right.points"):
        return f"el punto {number} de la columna {'izquierda' if name == 'left.points' else 'derecha'}"
    if name == "chart_labels":
        return f"el nombre de la barra {number}"
    if name == "chart_values":
        return f"el valor de la barra {number}"
    return "un texto de la diapositiva"


def _message(code: LintCode, layout: str, field: str | None, **words: str) -> str:
    noun = _noun(layout, field)
    fix = "Usa una unidad más corta." if field and field.startswith("chart_values") else "Usa un texto más corto."
    text = MESSAGES[code].format(noun=noun, fix=fix, **words)
    return text[0].upper() + text[1:]
