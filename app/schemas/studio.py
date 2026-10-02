from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints, model_validator

from app.schemas.courses import MAX_AUDIENCE_CHARS, MAX_MODULES, AvatarEngine, ModuleCount
from app.services.ai.designer import MAX_NARRATION_CHARS, MAX_STORYBOARD_SCENES, CourseOutline, OutlineModule
from app.services.slides.spec import SceneVisual, Slide

__all__ = [
    "MAX_MODULES",
    "AvatarOut",
    "Capabilities",
    "CourseOutline",
    "CourseOutlineIn",
    "DraftRequest",
    "OutlineGenerate",
    "QuizGenerate",
    "RecordingCompose",
    "RenderRequest",
    "SlidePreview",
    "Storyboard",
    "StoryboardRegenerate",
    "StoryboardScene",
    "VoiceOut",
]

MAX_TITLE_CHARS = 300  # size of the course and module title columns
MAX_OUTLINE_ITEMS = 10  # objectives / key points per list

Title = Annotated[str, StringConstraints(max_length=MAX_TITLE_CHARS)]
OutlineItem = Annotated[str, StringConstraints(max_length=300)]


class OutlineGenerate(BaseModel):
    brief: str = Field(default="", max_length=8000)
    audience: str = Field(default="", max_length=MAX_AUDIENCE_CHARS)
    tone: str = Field(default="", max_length=200)
    minutes: int = Field(default=20, ge=5, le=240)
    modules: ModuleCount = None
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
    visual: SceneVisual = Field(default_factory=SceneVisual)


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
    backdrop: bool = False  # the scene has a visual: its text goes over a darkened picture


class SlidePreview(BaseModel):
    slide: Slide
    context: SlidePreviewContext = Field(default_factory=SlidePreviewContext)
    # The scene's visual, to show the infographic or image already made for it (its course keeps them).
    visual: SceneVisual | None = None
    course_id: int | None = Field(default=None, ge=1)


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
    own: bool = False  # the company's own avatar (from a photo or footage), listed first
    engines: list[str] = Field(default_factory=list)  # HeyGen engines it renders on (empty: any)


class RenderRequest(BaseModel):
    """Voice, presenter and look for the course's videos (saved as the course defaults)."""

    module_ids: list[int] | None = Field(default=None, max_length=50)
    voice_id: str | None = Field(default=None, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")  # goes into a URL path
    voice_name: str | None = Field(default=None, max_length=200)
    avatar_id: str | None = Field(default=None, max_length=100)
    avatar_name: str | None = Field(default=None, max_length=200)
    co_avatar_id: str | None = Field(default=None, max_length=100)  # "" removes the second presenter
    co_avatar_name: str | None = Field(default=None, max_length=200)
    co_voice_id: str | None = Field(default=None, max_length=100, pattern=r"^[A-Za-z0-9_-]*$")
    co_voice_name: str | None = Field(default=None, max_length=200)
    avatar_engine: AvatarEngine | None = None
    animation: Literal["", "high"] | None = None
    presenter: bool | None = None
    theme: Literal["dark", "light"] | None = None

    @model_validator(mode="after")
    def _two_different_presenters(self) -> "RenderRequest":
        if self.co_avatar_id and self.co_avatar_id == self.avatar_id:
            raise ValueError("El segundo presentador debe ser otra persona")
        return self


class TimelinePoint(BaseModel):
    at: float = Field(ge=0, le=6 * 3600)  # seconds into the recording (up to 6 hours)
    slide: int = Field(ge=0, le=500)  # deck page index


class RecordingCompose(BaseModel):
    recording_asset_id: str = Field(min_length=1, max_length=36)
    deck_asset_id: str | None = Field(default=None, max_length=36)  # without a deck the recording is the video
    timeline: list[TimelinePoint] = Field(default_factory=list, max_length=2000)  # slide changes


class Capabilities(BaseModel):
    ai: bool
    voice: bool
    avatar: bool
    storage: bool
    # The kinds of scene visual offered.
    visuals: list[Literal["stock", "image", "clip", "infographic"]] = Field(default_factory=list)
    max_clips: int = 0  # animated clips per module (standard animation)
