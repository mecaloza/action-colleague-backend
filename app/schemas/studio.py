from typing import Literal

from pydantic import BaseModel, Field

from app.services.ai.designer import CourseOutline
from app.services.slides.spec import Slide

__all__ = [
    "MAX_MODULES",
    "Capabilities",
    "CourseOutline",
    "DraftRequest",
    "OutlineGenerate",
    "QuizGenerate",
    "SlidePreview",
    "Storyboard",
    "StoryboardRegenerate",
    "StoryboardScene",
]

MAX_MODULES = 12  # per course, both when asking the AI for a structure and when approving one


class OutlineGenerate(BaseModel):
    brief: str = Field(default="", max_length=8000)
    audience: str = Field(default="", max_length=500)
    tone: str = Field(default="", max_length=200)
    minutes: int = Field(default=20, ge=5, le=240)
    modules: int | None = Field(default=None, ge=1, le=MAX_MODULES)
    feedback: str = Field(default="", max_length=4000)


class DraftRequest(BaseModel):
    module_ids: list[int] | None = Field(default=None, max_length=50)


class StoryboardScene(BaseModel):
    id: str = Field(min_length=1, max_length=40)
    slide: Slide
    narration: str = Field(min_length=1, max_length=4000)


class Storyboard(BaseModel):
    scenes: list[StoryboardScene] = Field(max_length=40)


class StoryboardRegenerate(BaseModel):
    feedback: str = Field(default="", max_length=4000)


class QuizGenerate(BaseModel):
    count: int = Field(default=5, ge=1, le=15)


class SlidePreviewContext(BaseModel):
    course_title: str = Field(default="", max_length=300)
    module_label: str = Field(default="", max_length=100)
    index: int = Field(default=1, ge=1, le=99)
    total: int = Field(default=1, ge=1, le=99)
    theme: Literal["dark", "light"] = "dark"
    presenter: bool = False


class SlidePreview(BaseModel):
    slide: Slide
    context: SlidePreviewContext = Field(default_factory=SlidePreviewContext)


class Capabilities(BaseModel):
    ai: bool
    voice: bool
    avatar: bool
    storage: bool
