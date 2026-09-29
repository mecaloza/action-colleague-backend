from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints

from app.schemas.courses import MAX_AUDIENCE_CHARS
from app.services.ai.designer import MAX_NARRATION_CHARS, MAX_STORYBOARD_SCENES, CourseOutline, OutlineModule
from app.services.slides.spec import Slide

__all__ = [
    "MAX_MODULES",
    "AvatarOut",
    "Capabilities",
    "CourseOutline",
    "CourseOutlineIn",
    "DraftRequest",
    "OutlineGenerate",
    "QuizGenerate",
    "RenderRequest",
    "SlidePreview",
    "Storyboard",
    "StoryboardRegenerate",
    "StoryboardScene",
    "VoiceOut",
]

MAX_MODULES = 12  # per course, both when asking the AI for a structure and when approving one
MAX_TITLE_CHARS = 300  # size of the course and module title columns
MAX_OUTLINE_ITEMS = 10  # objectives / key points per list

Title = Annotated[str, StringConstraints(max_length=MAX_TITLE_CHARS)]
OutlineItem = Annotated[str, StringConstraints(max_length=300)]


class OutlineGenerate(BaseModel):
    brief: str = Field(default="", max_length=8000)
    audience: str = Field(default="", max_length=MAX_AUDIENCE_CHARS)
    tone: str = Field(default="", max_length=200)
    minutes: int = Field(default=20, ge=5, le=240)
    modules: int | None = Field(default=None, ge=1, le=MAX_MODULES)
    feedback: str = Field(default="", max_length=4000)


class OutlineModuleIn(OutlineModule):
    """An outline module as the admin approves it (the model's schema can't carry limits: Structured Outputs)."""

    title: Title
    summary: str = Field(max_length=3000)  # becomes the module description
    objectives: list[OutlineItem] = Field(max_length=MAX_OUTLINE_ITEMS)
    key_points: list[OutlineItem] = Field(max_length=MAX_OUTLINE_ITEMS)
    estimated_minutes: int = Field(ge=1, le=240)


class CourseOutlineIn(CourseOutline):
    title: Title
    description: str = Field(max_length=5000)
    audience: str = Field(max_length=MAX_AUDIENCE_CHARS)  # stored in the course settings
    objectives: list[OutlineItem] = Field(max_length=MAX_OUTLINE_ITEMS)
    modules: list[OutlineModuleIn] = Field(max_length=50)  # 1..MAX_MODULES is checked with a clearer message


class DraftRequest(BaseModel):
    module_ids: list[int] | None = Field(default=None, max_length=50)


class StoryboardScene(BaseModel):
    id: str = Field(min_length=1, max_length=40)
    slide: Slide
    narration: str = Field(min_length=1, max_length=MAX_NARRATION_CHARS)


class Storyboard(BaseModel):
    scenes: list[StoryboardScene] = Field(max_length=MAX_STORYBOARD_SCENES)


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


class VoiceOut(BaseModel):
    id: str
    name: str
    gender: str = ""
    accent: str = ""
    language: str = ""
    preview_url: str = ""
    category: str = ""


class AvatarOut(BaseModel):
    id: str
    name: str
    preview_image_url: str = ""
    preview_video_url: str = ""
    gender: str = ""


class RenderRequest(BaseModel):
    """Voice, presenter and look for the course's videos (saved as the course defaults)."""

    module_ids: list[int] | None = Field(default=None, max_length=50)
    voice_id: str | None = Field(default=None, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")  # goes into a URL path
    voice_name: str | None = Field(default=None, max_length=200)
    avatar_id: str | None = Field(default=None, max_length=100)
    avatar_name: str | None = Field(default=None, max_length=200)
    presenter: bool | None = None
    theme: Literal["dark", "light"] | None = None


class Capabilities(BaseModel):
    ai: bool
    voice: bool
    avatar: bool
    storage: bool
