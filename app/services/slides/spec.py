"""Slide content and rendering context (shared by the AI designer, the editor preview and the video)."""

from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, Field, StringConstraints, model_validator

from app.services.slides.icons import ICONS, IconName, NoIcon

Layout = Literal["cover", "bullets", "statement", "stat", "steps", "comparison", "closing"]

# What a layout has room for: the AI designer trims to these and the renderer enforces them on edited slides.
MAX_POINTS = 5  # bullets / steps / takeaways
MAX_COLUMN_POINTS = 4  # per side of a comparison

# Input limits, well above what a slide can show (the renderer shrinks and ellipsizes long text). They
# bound the renderer's work: every request to /slides/preview and every stored scene is laid out.
MAX_TEXT_CHARS = 300  # title, subtitle, a point, the stat label
MAX_LABEL_CHARS = 100  # stat value, author, column heading, eyebrow
MAX_LIST_ITEMS = 10  # points kept per list (only the first MAX_POINTS / MAX_COLUMN_POINTS are shown)

SlideText = Annotated[str, StringConstraints(max_length=MAX_TEXT_CHARS)]
SlideLabel = Annotated[str, StringConstraints(max_length=MAX_LABEL_CHARS)]
SlideIcon = IconName | NoIcon  # "" draws the layout's plain marker (the AI's schema: one of the catalog)


def _known_icon(name: object) -> object:
    """An icon no longer in the catalog (or a typo) is drawn as none, instead of breaking a saved script."""
    return name if not isinstance(name, str) or name in ICONS else ""


StoredIcon = Annotated[SlideIcon, BeforeValidator(_known_icon)]


class ComparisonColumn(BaseModel):
    heading: SlideLabel = ""
    points: list[SlideText] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)


class Slide(BaseModel):
    layout: Layout
    eyebrow: SlideLabel = ""
    title: SlideText = ""
    subtitle: SlideText = ""
    points: list[SlideText] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)  # bullets / steps / takeaways
    icons: list[StoredIcon] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)  # one per point, same order
    icon: StoredIcon = ""  # the statement's or the stat's icon
    stat_value: SlideLabel = ""
    stat_label: SlideText = ""
    quote_author: SlideLabel = ""
    left: ComparisonColumn = Field(default_factory=ComparisonColumn)
    right: ComparisonColumn = Field(default_factory=ComparisonColumn)

    @model_validator(mode="after")
    def _icons_follow_points(self) -> "Slide":
        del self.icons[len(self.points):]  # an icon without its point is never drawn
        return self


def visible_points(points: list[str], limit: int) -> list[str]:
    """The non-blank points a layout shows: the first `limit` of them."""
    return [point for point in points if point.strip()][:limit]


def visible_items(slide: "Slide", limit: int) -> list[tuple[str, str]]:
    """The points a layout shows, each with its own icon ("" if none): paired before blank points are dropped."""
    icons = slide.icons
    return [
        (point, icons[index] if index < len(icons) else "") for index, point in enumerate(slide.points) if point.strip()
    ][:limit]


@dataclass(frozen=True)
class SlideContext:
    course_title: str = ""
    module_label: str = ""  # e.g. "Módulo 2"
    index: int = 1
    total: int = 1
    theme: str = "dark"
    presenter: bool = False  # reserve the bottom-right corner for the presenter bubble
    hero: bool = False  # the presenter is shown large on the right (cover and closing): text keeps to the left
    reveal: int | None = None  # beats shown (see `render.beat_count`); None shows the whole slide
