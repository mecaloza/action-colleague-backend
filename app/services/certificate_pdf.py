"""
A certificate as a PDF: one A4 landscape page at 300 dpi, drawn on demand from its snapshot with the slides' brand
fonts and helpers (white page, black band with the Rubinstein mark, orange accents). Pillow writes the PDF: an image
of the page, so its text can't be selected.
"""

from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw

from app.db.models import Certificate
from app.schemas.settings import DEFAULT_TIMEZONE, PlatformSettings, is_timezone
from app.services import certificates
from app.services.slides.render import (
    BODY_FONT,
    BRAND_ORANGE,
    DISPLAY_FONT,
    FittedText,
    brand_mark,
    draw_lines,
    draw_tracked,
    fit,
    font,
)

DPI = 300
WIDTH, HEIGHT = 3508, 2480  # A4 landscape (297 x 210 mm) at 300 dpi
BAND = 560  # the black band on the left
LEFT, RIGHT = BAND + 300, WIDTH - 300  # the text's column
INK, MUTED, RULE, PAPER, NIGHT = (24, 24, 24), (104, 104, 104), (214, 214, 214), (255, 255, 255), (12, 12, 12)
SIGNATURE_WIDTH, VERIFICATION_WIDTH = 950, 1300  # side by side under the text, with a gap between them
BOTTOM = HEIGHT - 260  # where the signature and the verification end
MONTHS = (
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
)


def long_date(moment: datetime, zone: str) -> str:
    """"8 de octubre de 2026", the day it is in that time zone."""
    local = certificates.aware(moment).astimezone(ZoneInfo(zone if is_timezone(zone) else DEFAULT_TIMEZONE))
    return f"{local.day} de {MONTHS[local.month - 1]} de {local.year}"


def _number(value: float) -> str:
    """One decimal at most, with a decimal comma: 3,5 · 92 · 86,7."""
    return f"{value:.1f}".rstrip("0").rstrip(".").replace(".", ",")


def course_list(titles: list[str], limit: int = 420) -> str:
    """"Cursos: A · B · C y 4 más": what fits a few lines (a path has up to 30 courses, and measuring a long text at
    every size tried would take seconds)."""
    shown: list[str] = []
    for title in titles:
        if shown and len(" · ".join([*shown, title])) > limit:
            break
        shown.append(title)
    rest = len(titles) - len(shown)
    return f"Cursos: {' · '.join(shown)}" + (f" y {rest} más" if rest else "")


def duration(hours: float) -> str | None:
    if hours <= 0:  # reading and documents: no video to count
        return None
    if hours < 1:
        minutes = max(1, round(hours * 60))
        return f"{minutes} minuto{'s' if minutes != 1 else ''}"
    value = round(hours, 1)
    return f"{_number(value)} hora{'' if value == 1 else 's'}"


@dataclass(frozen=True)
class Texts:
    """Everything the page says, in order, and the PDF's date: what tells one drawing from another."""

    eyebrow: str
    issuer: str
    recipient: str
    lead: str
    title: str
    facts: str  # date of issue, hours, modules, average score
    courses: str | None  # a path's courses
    validity: str | None
    signer_name: str
    signer_title: str
    code: str
    verify_url: str
    issued_at: datetime  # the PDF's creation date


def texts(certificate: Certificate, settings: PlatformSettings) -> Texts:
    """The dates in the time zone of the snapshot: changing the platform's later never changes them (`settings` only
    stands in for a snapshot without one)."""
    details = certificates.details_of(certificate)
    zone = details.timezone or settings.general.timezone
    path = certificate.kind == "path"
    facts = [f"Emitido el {long_date(certificate.issued_at, zone)}", duration(details.hours)]
    if details.modules:
        facts.append(f"{details.modules} módulo{'s' if details.modules != 1 else ''}")
    if details.average_score is not None:
        facts.append(f"Calificación promedio: {_number(details.average_score)} %")
    titles = [course.title for course in details.courses]
    return Texts(
        eyebrow="CERTIFICADO DE RUTA COMPLETADA" if path else "CERTIFICADO DE FINALIZACIÓN",
        issuer=certificates.issuer_name(certificate),
        recipient=certificate.recipient_name,
        lead="completó la ruta de aprendizaje" if path else "completó el curso",
        title=certificate.title,
        facts=" · ".join(fact for fact in facts if fact),
        courses=course_list(titles) if path and titles else None,
        validity=f"Vigente hasta el {long_date(certificate.expires_at, zone)}" if certificate.expires_at else None,
        signer_name=details.signer_name,
        signer_title=details.signer_title,
        code=certificate.code,
        verify_url=certificates.verify_url(certificate.code),
        issued_at=certificates.aware(certificate.issued_at),
    )


def _band(draw: ImageDraw.ImageDraw) -> None:
    """The brand's motif, as on the slides: the faceted mark outlined, over a small orange one."""
    draw.rectangle([0, 0, BAND, HEIGHT], fill=NIGHT)
    cx = BAND // 2
    brand_mark(draw, cx, 880, 560, (64, 64, 64), 10)  # 0.82 as wide as tall: well inside the band
    brand_mark(draw, cx, 1290, 160, BRAND_ORANGE, 12)


def _signature(draw: ImageDraw.ImageDraw, page: Texts, bottom: int, *, measure: bool = False) -> int:
    """The signer's name over a rule, their title under it (bottom left), when the platform has one. Returns the
    top of what it draws (or would draw)."""
    if not page.signer_name:
        return bottom
    width = SIGNATURE_WIDTH
    name = fit(draw, page.signer_name, DISPLAY_FONT, 600, width, 100, 76, 48, max_lines=1)
    rule_y = bottom - 120
    top = rule_y - 24 - name.height
    if measure:
        return top
    draw_lines(draw, name, LEFT, top, INK)
    draw.line([(LEFT, rule_y), (LEFT + width, rule_y)], fill=INK, width=4)
    if page.signer_title:
        title = fit(draw, page.signer_title, BODY_FONT, 500, width, 80, 50, 34, max_lines=1)
        draw_lines(draw, title, LEFT, rule_y + 28, MUTED)
    return top


def _verification(draw: ImageDraw.ImageDraw, page: Texts, bottom: int, *, measure: bool = False) -> int:
    """The code and where to check it (bottom right, aligned to the right edge). Returns its top."""
    label_font, code_font = font(BODY_FONT, 38, 700), font(DISPLAY_FONT, 88, 700)
    url = fit(draw, f"Verifícalo en {page.verify_url}", BODY_FONT, 500, VERIFICATION_WIDTH, 110, 40, 28, max_lines=2)
    url_top = bottom - url.height
    code_top = url_top - 130
    label_top = code_top - 66
    if measure:
        return label_top
    for index, line in enumerate(url.lines):
        line_width = draw.textlength(line, font=url.font)
        draw.text((RIGHT - line_width, url_top + index * url.line_height), line, font=url.font, fill=MUTED)
    draw.text((RIGHT - draw.textlength(page.code, font=code_font), code_top), page.code, font=code_font, fill=INK)
    label, tracking = "CÓDIGO DE VERIFICACIÓN", 4
    label_width = sum(draw.textlength(char, font=label_font) + tracking for char in label) - tracking
    draw_tracked(draw, label, (round(RIGHT - label_width), label_top), label_font, BRAND_ORANGE, tracking)
    return label_top


@dataclass
class _Block:
    text: FittedText | None  # None: the rule under the title
    color: tuple[int, int, int]
    gap: int  # below it


def _body(draw: ImageDraw.ImageDraw, page: Texts, scale: float) -> list[_Block]:
    """From the recipient's name to the validity, every size times `scale`."""
    width = RIGHT - LEFT

    def block(text, path, weight, size, min_size, lines, color, gap, leading=1.15):
        size = round(size * scale)
        fitted = fit(draw, text, path, weight, width, round(size * leading) * lines, size,
                     min(min_size, size), max_lines=lines, leading=leading)
        return _Block(fitted, color, round(gap * scale))

    blocks = [
        block(page.recipient, DISPLAY_FONT, 600, 220, 110, 2, INK, 40, leading=1.0),
        block(page.lead, BODY_FONT, 500, 76, 44, 1, MUTED, 24),
        block(page.title, DISPLAY_FONT, 600, 130, 64, 3, INK, 70, leading=1.05),
        _Block(None, RULE, round(56 * scale)),
        block(page.facts, BODY_FONT, 500, 56, 34, 2, INK, 20),
    ]
    if page.courses:
        blocks.append(block(page.courses, BODY_FONT, 500, 50, 30, 4, MUTED, 20))
    if page.validity:
        blocks.append(block(page.validity, BODY_FONT, 600, 56, 34, 1, INK, 0))
    return blocks


def page_image(page: Texts) -> Image.Image:
    image = Image.new("RGB", (WIDTH, HEIGHT), PAPER)
    draw = ImageDraw.Draw(image)
    _band(draw)

    y = 330
    draw.rectangle([LEFT, y, LEFT + 240, y + 16], fill=BRAND_ORANGE)
    draw_tracked(draw, page.eyebrow, (LEFT, y + 74), font(BODY_FONT, 56, 700), BRAND_ORANGE, 6)
    issuer = fit(draw, page.issuer, BODY_FONT, 600, RIGHT - LEFT, 100, 72, 48, max_lines=1)
    y = draw_lines(draw, issuer, LEFT, y + 170, MUTED)

    # The body takes the room left above the signature and the verification: a long name, title or list of courses
    # shrinks it (up to about half its size) instead of running into them.
    room = min(_signature(draw, page, BOTTOM, measure=True), _verification(draw, page, BOTTOM, measure=True)) - 90 - y
    for scale in (1.0, 0.92, 0.85, 0.78, 0.72, 0.66, 0.6, 0.55):
        lead_in = round(170 * scale)
        blocks = _body(draw, page, scale)
        if lead_in + sum((b.text.height if b.text else 4) + b.gap for b in blocks) <= room:
            break
    y += lead_in
    for item in blocks:
        if item.text is None:
            draw.line([(LEFT, y), (RIGHT, y)], fill=item.color, width=4)
            y += 4 + item.gap
        else:
            y = draw_lines(draw, item.text, LEFT, y, item.color) + item.gap

    _signature(draw, page, BOTTOM)
    _verification(draw, page, BOTTOM)
    return image


def draw(page: Texts) -> bytes:
    """The page as a PDF: tens of milliseconds of CPU."""
    buffer = BytesIO()
    page_image(page).save(
        buffer, "PDF", resolution=float(DPI), quality=92,
        title=f"{page.eyebrow.capitalize()} {page.code}", author=page.issuer, subject=page.title,
        creator=certificates.DEFAULT_ISSUER, creationDate=page.issued_at.utctimetuple(),
    )
    return buffer.getvalue()


def render(certificate: Certificate, settings: PlatformSettings) -> bytes:
    return draw(texts(certificate, settings))
