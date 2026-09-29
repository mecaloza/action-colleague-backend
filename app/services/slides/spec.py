"""Slide content and rendering context (shared by the AI designer, the editor preview and the video)."""

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

Layout = Literal["cover", "bullets", "statement", "stat", "steps", "comparison", "closing"]

# What a layout has room for: the AI designer trims to these and the renderer enforces them on edited slides.
MAX_POINTS = 5  # bullets / steps / takeaways
MAX_COLUMN_POINTS = 4  # per side of a comparison


class ComparisonColumn(BaseModel):
    heading: str = ""
    points: list[str] = Field(default_factory=list)


class Slide(BaseModel):
    layout: Layout
    eyebrow: str = ""
    title: str = ""
    subtitle: str = ""
    points: list[str] = Field(default_factory=list)  # bullets / steps / takeaways
    stat_value: str = ""
    stat_label: str = ""
    quote_author: str = ""
    left: ComparisonColumn = Field(default_factory=ComparisonColumn)
    right: ComparisonColumn = Field(default_factory=ComparisonColumn)


def visible_points(points: list[str], limit: int) -> list[str]:
    """The non-blank points a layout shows: the first `limit` of them."""
    return [point for point in points if point.strip()][:limit]


@dataclass(frozen=True)
class SlideContext:
    course_title: str = ""
    module_label: str = ""  # e.g. "Módulo 2"
    index: int = 1
    total: int = 1
    theme: str = "dark"
    presenter: bool = False  # reserve the bottom-right corner for the presenter bubble
