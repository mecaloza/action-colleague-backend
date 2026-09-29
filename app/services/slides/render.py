"""
Slide renderer: 1920x1080 PNG slides in the brand style (black, white, orange #ff4c01).

Every layout lays text out inside explicit boxes and shrinks the font until it fits, so a slide
can never overflow, overlap the presenter bubble, or be distorted (always rendered at the exact
video resolution).
"""

from dataclasses import dataclass
from functools import lru_cache
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from app.services.slides.spec import MAX_COLUMN_POINTS, MAX_POINTS, Slide, SlideContext, visible_points

WIDTH, HEIGHT = 1920, 1080
MARGIN_X, MARGIN_TOP, MARGIN_BOTTOM = 120, 96, 96
# Presenter bubble (bottom-right). Keep in sync with the video composer.
BUBBLE_SIZE, BUBBLE_MARGIN = 340, 64
BUBBLE_BOX = (WIDTH - BUBBLE_MARGIN - BUBBLE_SIZE, HEIGHT - BUBBLE_MARGIN - BUBBLE_SIZE, WIDTH - BUBBLE_MARGIN, HEIGHT - BUBBLE_MARGIN)

FONTS_DIR = Path(__file__).resolve().parent / "fonts"  # SIL Open Font License (see the OFL files)
DISPLAY_FONT = FONTS_DIR / "InterTight.ttf"
BODY_FONT = FONTS_DIR / "RedHatDisplay.ttf"

Color = tuple[int, int, int]


@dataclass(frozen=True)
class Theme:
    background: Color
    background_end: Color
    text: Color
    muted: Color
    accent: Color
    line: Color


DARK = Theme(
    background=(12, 12, 12), background_end=(24, 24, 24), text=(255, 255, 255), muted=(168, 168, 168),
    accent=(255, 76, 1), line=(56, 56, 56),
)
LIGHT = Theme(
    background=(255, 255, 255), background_end=(244, 244, 244), text=(29, 29, 29), muted=(96, 96, 96),
    accent=(255, 76, 1), line=(222, 222, 222),
)
THEMES = {"dark": DARK, "light": LIGHT}


# ── Text helpers ──────────────────────────────────────────────────────


@lru_cache(maxsize=256)
def font(path: Path, size: int, weight: int) -> ImageFont.FreeTypeFont:
    """The font at `size` px with the variable-font `weight` (cached: loading a font file is slow)."""
    loaded = ImageFont.truetype(str(path), size)
    try:
        loaded.set_variation_by_axes([weight])
    except OSError:  # static font without a weight axis
        pass
    return loaded


def text_width(draw: ImageDraw.ImageDraw, text: str, fnt: ImageFont.FreeTypeFont) -> float:
    return draw.textlength(text, font=fnt)


def _fitting_prefix(draw: ImageDraw.ImageDraw, word: str, fnt: ImageFont.FreeTypeFont, max_width: int) -> int:
    """Length of the longest start of `word` that fits the box (at least one character).

    Doubling then bisecting only ever measures strings about as long as one line, whatever the word's length.
    """
    fits, too_long = 1, 2
    while too_long <= len(word) and text_width(draw, word[:too_long], fnt) <= max_width:
        fits, too_long = too_long, too_long * 2
    too_long = min(too_long, len(word) + 1)
    while too_long - fits > 1:
        middle = (fits + too_long) // 2
        if text_width(draw, word[:middle], fnt) <= max_width:
            fits = middle
        else:
            too_long = middle
    return fits


def _break_word(
    draw: ImageDraw.ImageDraw, word: str, fnt: ImageFont.FreeTypeFont, max_width: int, max_pieces: int
) -> list[str]:
    """A word wider than the box (a URL, a figure without spaces) cut into pieces that fit; at most `max_pieces`."""
    pieces: list[str] = []
    while word and len(pieces) < max_pieces:
        cut = _fitting_prefix(draw, word, fnt, max_width)
        pieces.append(word[:cut])
        word = word[cut:]
    return pieces


def wrap(
    draw: ImageDraw.ImageDraw, text: str, fnt: ImageFont.FreeTypeFont, max_width: int, max_lines: int | None = None
) -> list[str]:
    """Greedy word wrap where no line is wider than the box (long words are cut).

    With `max_lines`, it stops as soon as the text needs more lines than that: callers only need to know
    it doesn't fit, and long texts would otherwise be laid out in full for every font size tried.
    """
    lines: list[str] = []
    for paragraph in text.split("\n"):
        current = ""
        for word in paragraph.split():
            candidate = f"{current} {word}" if current else word
            if text_width(draw, candidate, fnt) <= max_width:
                current = candidate
                continue
            if current:
                lines.append(current)
            # Past max_lines + 1 pieces the text can't fit anyway: the rest of the word is not measured.
            max_pieces = len(word) if max_lines is None else max_lines + 1
            *full_pieces, current = _break_word(draw, word, fnt, max_width, max_pieces)
            lines.extend(full_pieces)
            if max_lines is not None and len(lines) > max_lines:
                return lines
        if current:
            lines.append(current)
    return lines or [""]


@dataclass
class FittedText:
    font: ImageFont.FreeTypeFont
    lines: list[str]
    line_height: int

    @property
    def height(self) -> int:
        return self.line_height * len(self.lines)


def _ellipsize(draw: ImageDraw.ImageDraw, lines: list[str], fnt: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    """Close the last line with an ellipsis, dropping words (then letters) until it fits."""
    while lines and lines[-1] and text_width(draw, lines[-1] + "…", fnt) > max_width:
        lines[-1] = lines[-1].rsplit(" ", 1)[0] if " " in lines[-1] else lines[-1][:-1]
    if lines:
        lines[-1] = lines[-1].rstrip(" ,.;:") + "…"
    return lines


def fit(
    draw: ImageDraw.ImageDraw,
    text: str,
    path: Path,
    weight: int,
    max_width: int,
    max_height: int,
    max_size: int,
    min_size: int,
    max_lines: int,
    leading: float = 1.15,
) -> FittedText:
    """Largest font size (stepping down) whose wrapped text fits the box; truncates as a last resort."""
    for size in range(max_size, min_size - 1, -2):
        fnt = font(path, size, weight)
        line_height = round(size * leading)
        lines_that_fit = min(max_lines, max_height // line_height)
        lines = wrap(draw, text, fnt, max_width, max_lines=lines_that_fit)
        if len(lines) <= lines_that_fit:
            return FittedText(fnt, lines, line_height)

    fnt = font(path, min_size, weight)
    line_height = round(min_size * leading)
    limit = max(1, min(max_lines, max_height // line_height))
    lines = wrap(draw, text, fnt, max_width, max_lines=limit)
    if len(lines) > limit:
        lines = _ellipsize(draw, lines[:limit], fnt, max_width)
    return FittedText(fnt, lines, line_height)


def fit_all(
    draw: ImageDraw.ImageDraw,
    texts: list[str],
    path: Path,
    weight: int,
    max_width: int,
    max_height: int,
    max_size: int,
    min_size: int,
    max_lines: int,
) -> list[FittedText]:
    """Fit several items (bullets, steps) at one shared size, so no item looks smaller than the rest."""
    best_sizes = [
        fit(draw, text, path, weight, max_width, max_height, max_size, min_size, max_lines=max_lines).font.size
        for text in texts
    ]
    size = min(best_sizes, default=max_size)
    return [fit(draw, text, path, weight, max_width, max_height, size, min_size, max_lines=max_lines) for text in texts]


def draw_lines(draw: ImageDraw.ImageDraw, fitted: FittedText, x: int, y: int, fill: Color) -> int:
    """Draw the lines top-down; returns the y just below the last one."""
    for line in fitted.lines:
        draw.text((x, y), line, font=fitted.font, fill=fill)
        y += fitted.line_height
    return y


def draw_tracked(
    draw: ImageDraw.ImageDraw, text: str, xy: tuple[int, int], fnt: ImageFont.FreeTypeFont, fill: Color, tracking: float
) -> None:
    """Text with letter spacing (Pillow has no native tracking)."""
    x, y = xy
    for char in text:
        draw.text((x, y), char, font=fnt, fill=fill)
        x += text_width(draw, char, fnt) + tracking


def diamond(
    draw: ImageDraw.ImageDraw, cx: float, cy: float, radius: float,
    fill: Color | None = None, outline: Color | None = None, width: int = 1,
) -> None:
    """Rhombus centered on (cx, cy): filled, outlined, or both."""
    draw.polygon(
        [(cx, cy - radius), (cx + radius, cy), (cx, cy + radius), (cx - radius, cy)], fill=fill, outline=outline, width=width
    )


# ── Frame (background, header, footer) ────────────────────────────────


def _background(theme: Theme) -> Image.Image:
    base = Image.new("RGB", (WIDTH, HEIGHT), theme.background)
    gradient = Image.linear_gradient("L").resize((WIDTH, HEIGHT))
    end = Image.new("RGB", (WIDTH, HEIGHT), theme.background_end)
    return Image.composite(end, base, gradient)


def _diamonds(draw: ImageDraw.ImageDraw, theme: Theme) -> None:
    """Brand motif in the top-right corner: two outlined diamonds and a small orange one."""
    cx = WIDTH - 190
    diamond(draw, cx, 150, 95, outline=theme.line, width=3)
    diamond(draw, cx, 270, 60, outline=theme.line, width=3)
    diamond(draw, cx, 360, 26, fill=theme.accent)


def _frame(ctx: SlideContext, theme: Theme) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    """Background, corner motif, header label and page counter; returns the image and its drawing handle."""
    image = _background(theme)
    draw = ImageDraw.Draw(image)
    _diamonds(draw, theme)

    label_font = font(BODY_FONT, 24, 700)
    header_y = MARGIN_TOP - 44
    label = " · ".join(part for part in (ctx.module_label.upper(), ctx.course_title.upper()) if part)
    if label:
        diamond(draw, MARGIN_X + 9, header_y + 14, 9, theme.accent)
        draw_tracked(draw, label[:70], (MARGIN_X + 32, header_y), label_font, theme.muted, 3)

    counter = f"{ctx.index:02d} / {ctx.total:02d}"
    counter_font = font(BODY_FONT, 24, 600)
    footer_y = HEIGHT - MARGIN_BOTTOM + 28
    draw.line([(MARGIN_X, footer_y - 18), (MARGIN_X + 80, footer_y - 18)], fill=theme.accent, width=4)
    draw.text((MARGIN_X, footer_y - 4), counter, font=counter_font, fill=theme.muted)
    return image, draw


def _content_box(top: int) -> tuple[int, int, int, int]:
    """(left, top, right, bottom) of the area text may use; avoids the corner motif."""
    right = WIDTH - MARGIN_X - (180 if top < 360 else 0)
    bottom = HEIGHT - MARGIN_BOTTOM - 40
    return MARGIN_X, top, right, bottom


def _text_right_limit(ctx: SlideContext, y_bottom: int, default_right: int) -> int:
    """Right edge for a block whose bottom is y_bottom: stay clear of the presenter bubble."""
    if ctx.presenter and y_bottom > BUBBLE_BOX[1] - 40:
        return min(default_right, BUBBLE_BOX[0] - 60)
    return default_right


# ── Layouts ───────────────────────────────────────────────────────────


def _cover(slide: Slide, ctx: SlideContext, theme: Theme, draw: ImageDraw.ImageDraw) -> None:
    left, top, right, bottom = _content_box(300)
    right = min(right, WIDTH - 420)
    draw.rectangle([left, top - 60, left + 120, top - 50], fill=theme.accent)
    title = fit(draw, slide.title, DISPLAY_FONT, 600, right - left, 420, 150, 84, leading=1.0, max_lines=3)
    y = draw_lines(draw, title, left, top, theme.text) + 36
    if slide.subtitle:
        sub_right = _text_right_limit(ctx, bottom, right)
        subtitle = fit(draw, slide.subtitle, BODY_FONT, 400, sub_right - left, bottom - y, 46, 30, max_lines=3)
        draw_lines(draw, subtitle, left, y, theme.muted)


def _numbered_list(slide: Slide, ctx: SlideContext, theme: Theme, draw: ImageDraw.ImageDraw, top: int) -> None:
    left, _, right, bottom = _content_box(top)
    points = visible_points(slide.points, MAX_POINTS)
    if not points:
        return
    gap = 28
    per_item = (bottom - top - gap * (len(points) - 1)) // len(points)
    prefix_font = font(DISPLAY_FONT, 44, 600)
    prefix_w = int(text_width(draw, f"{MAX_POINTS}.", prefix_font)) + 18  # room for the last number
    text_left = left + 44 + prefix_w
    # The presenter bubble narrows the lowest items; size every item for the narrowest width.
    width = _text_right_limit(ctx, bottom, right) - text_left
    fitted_items = fit_all(draw, points, BODY_FONT, 500, width, per_item, 46, 30, max_lines=3)
    y = top
    for number, fitted in enumerate(fitted_items, start=1):
        diamond(draw, left + 12, y + fitted.line_height / 2, 12, theme.accent)
        draw.text((left + 44, y + (fitted.line_height - 44) / 2 - 4), f"{number}.", font=prefix_font, fill=theme.accent)
        draw_lines(draw, fitted, text_left, y, theme.text)
        y += max(fitted.height, per_item // 2) + gap


def _title_block(slide: Slide, ctx: SlideContext, theme: Theme, draw: ImageDraw.ImageDraw, max_lines: int = 2) -> int:
    """Slide title with a rule under it; returns the y where the body starts."""
    left, top, right, _ = _content_box(MARGIN_TOP + 40)
    title = fit(draw, slide.title, DISPLAY_FONT, 600, right - left, 230, 88, 56, leading=1.02, max_lines=max_lines)
    y = draw_lines(draw, title, left, top, theme.text)
    draw.line([(left, y + 26), (right, y + 26)], fill=theme.line, width=2)
    return y + 64


def _bullets(slide: Slide, ctx: SlideContext, theme: Theme, draw: ImageDraw.ImageDraw) -> None:
    _numbered_list(slide, ctx, theme, draw, _title_block(slide, ctx, theme, draw))


def _steps(slide: Slide, ctx: SlideContext, theme: Theme, draw: ImageDraw.ImageDraw) -> None:
    top = _title_block(slide, ctx, theme, draw)
    left, _, right, bottom = _content_box(top)
    steps = visible_points(slide.points, MAX_POINTS)
    if not steps:
        return
    per_item = (bottom - top) // len(steps)
    number_font = font(DISPLAY_FONT, 40, 700)
    width = _text_right_limit(ctx, bottom, right) - (left + 100)
    fitted_steps = fit_all(draw, steps, BODY_FONT, 500, width, per_item - 12, 44, 28, max_lines=2)
    for index, fitted in enumerate(fitted_steps):
        y = top + index * per_item
        cx, cy = left + 34, y + 30
        draw.ellipse([cx - 30, cy - 30, cx + 30, cy + 30], outline=theme.accent, width=3)
        label = f"{index + 1:02d}"
        draw.text((cx - text_width(draw, label, number_font) / 2, cy - 26), label, font=number_font, fill=theme.accent)
        if index < len(steps) - 1:
            draw.line([(cx, cy + 34), (cx, y + per_item - 4)], fill=theme.line, width=3)
        draw_lines(draw, fitted, left + 100, y + 30 - fitted.line_height // 2, theme.text)


def _statement(slide: Slide, ctx: SlideContext, theme: Theme, draw: ImageDraw.ImageDraw) -> None:
    left, top, right, bottom = _content_box(250)
    right = _text_right_limit(ctx, bottom, min(right, WIDTH - 360))
    text_left = left + 48  # room for the pull-quote bar
    statement = fit(draw, slide.title, DISPLAY_FONT, 500, right - text_left, 470, 96, 52, leading=1.08, max_lines=5)
    y = draw_lines(draw, statement, text_left, top + 20, theme.text)
    draw.rectangle([left, top + 28, left + 10, y - 12], fill=theme.accent)
    if slide.quote_author:
        draw_tracked(draw, slide.quote_author.upper()[:60], (text_left, y + 30), font(BODY_FONT, 26, 700), theme.muted, 3)


def _stat(slide: Slide, ctx: SlideContext, theme: Theme, draw: ImageDraw.ImageDraw) -> None:
    left, top, right, bottom = _content_box(230)
    value = fit(draw, slide.stat_value, DISPLAY_FONT, 700, min(right, 1500) - left, 330, 300, 120, leading=1.0, max_lines=1)
    y = draw_lines(draw, value, left - 6, top, theme.accent) + 10
    label_right = _text_right_limit(ctx, y + 170, right)  # a two-line label can reach the presenter bubble
    label = fit(draw, slide.stat_label or slide.title, DISPLAY_FONT, 600, label_right - left, 170, 72, 44, max_lines=2)
    y = draw_lines(draw, label, left, y, theme.text) + 20
    if slide.subtitle:
        sub_right = _text_right_limit(ctx, bottom, right)
        context = fit(draw, slide.subtitle, BODY_FONT, 400, sub_right - left, bottom - y, 40, 28, max_lines=3)
        draw_lines(draw, context, left, y, theme.muted)


def _comparison(slide: Slide, ctx: SlideContext, theme: Theme, draw: ImageDraw.ImageDraw) -> None:
    top = _title_block(slide, ctx, theme, draw)
    left, _, right, bottom = _content_box(top)
    right = _text_right_limit(ctx, bottom, right)
    gutter = 80
    column_width = (right - left - gutter) // 2
    item_width = column_width - 40  # the bullet takes the rest of the column
    list_top = top + 44 + 44  # below the column heading and its rule
    left_points = visible_points(slide.left.points, MAX_COLUMN_POINTS)
    right_points = visible_points(slide.right.points, MAX_COLUMN_POINTS)
    rows = max(len(left_points), len(right_points))
    per_item = (bottom - list_top) // rows if rows else 0
    # Both columns share one text size, so neither side looks more important.
    fitted = fit_all(draw, left_points + right_points, BODY_FONT, 500, item_width, per_item - 10, 38, 26, max_lines=3)
    columns = [
        (slide.left, theme.accent, fitted[: len(left_points)]),
        (slide.right, theme.text, fitted[len(left_points):]),
    ]
    for column_index, (column, color, items) in enumerate(columns):
        x = left + column_index * (column_width + gutter)
        heading = fit(draw, column.heading.upper(), BODY_FONT, 800, column_width, 60, 34, 24, max_lines=1)
        y = draw_lines(draw, heading, x, top, color)
        draw.rectangle([x, y + 8, x + 60, y + 14], fill=color)
        y = list_top
        for item in items:
            diamond(draw, x + 10, y + item.line_height / 2, 9, color)
            draw_lines(draw, item, x + 40, y, theme.text)
            y += max(item.height + 22, per_item // 2)
    mid_x = left + column_width + gutter // 2
    draw.line([(mid_x, top), (mid_x, bottom)], fill=theme.line, width=2)


def _closing(slide: Slide, ctx: SlideContext, theme: Theme, draw: ImageDraw.ImageDraw) -> None:
    top = _title_block(slide, ctx, theme, draw, max_lines=1)
    _numbered_list(slide, ctx, theme, draw, top)


LAYOUTS = {
    "cover": _cover,
    "bullets": _bullets,
    "statement": _statement,
    "stat": _stat,
    "steps": _steps,
    "comparison": _comparison,
    "closing": _closing,
}


def render(slide: Slide, ctx: SlideContext) -> Image.Image:
    theme = THEMES.get(ctx.theme, DARK)
    image, draw = _frame(ctx, theme)
    LAYOUTS[slide.layout](slide, ctx, theme, draw)
    return image


def render_png(slide: Slide, ctx: SlideContext, scale: float = 1.0) -> bytes:
    image = render(slide, ctx)
    if scale != 1.0:
        image = image.resize((round(WIDTH * scale), round(HEIGHT * scale)), Image.LANCZOS)
    buffer = BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()
