from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.common import UtcDatetime

MediaKind = Literal["video", "recording", "document", "deck", "image", "audio"]
MediaPurpose = Literal["module_video", "recording", "module_document", "course_cover", "course_material", "deck"]

# Purposes that become a module's video, and the `Module.source` each one sets.
VIDEO_PURPOSES = {"module_video": "upload", "recording": "recording"}
# Purposes that only make sense for one module: completing the upload must say which.
PURPOSES_NEEDING_MODULE = (*VIDEO_PURPOSES, "module_document")


class UploadCreate(BaseModel):
    filename: str = Field(min_length=1, max_length=300)
    mime_type: str = Field(min_length=1, max_length=120)
    size_bytes: int = Field(gt=0)
    kind: MediaKind
    course_id: int | None = None


class UploadTargetOut(BaseModel):
    method: Literal["PUT", "TUS"]
    url: str
    headers: dict[str, str]
    metadata: dict[str, str]
    chunk_size: int | None = None


class MediaAssetOut(BaseModel):
    id: str
    kind: str
    status: str
    mime_type: str
    size_bytes: int | None = None
    duration_seconds: float | None = None
    width: int | None = None
    height: int | None = None
    original_filename: str | None = None
    course_id: int | None = None
    url: str | None = None
    error: str | None = None
    pages: list[str] = []
    page_count: int | None = None  # of a deck: more than `pages` when some links could not be signed
    text_chars: int | None = None
    created_at: UtcDatetime | None = None


class UploadCreated(BaseModel):
    asset: MediaAssetOut
    upload: UploadTargetOut


class UploadComplete(BaseModel):
    module_id: int | None = None
    purpose: MediaPurpose | None = None


class CoverUpdate(BaseModel):
    asset_id: str = Field(min_length=1, max_length=36)


class JobOut(BaseModel):
    id: str
    type: str
    status: str
    progress: int
    step: str
    error: str | None = None
    course_id: int | None = None
    module_id: int | None = None
    result: dict[str, Any] | None = None
    created_at: UtcDatetime | None = None
    updated_at: UtcDatetime | None = None
