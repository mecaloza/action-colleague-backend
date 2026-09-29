"""Schemas of endpoints kept from the previous app until their replacement ships (manual video upload)."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class VideoUploadResponse(BaseModel):
    video_id: str
    storage_url: str
    status: str
    duration: Optional[int] = None
    file_size: Optional[int] = None
    format: str = "webm"

    model_config = {"from_attributes": True}


class UserVideoOut(BaseModel):
    id: str
    module_id: Optional[int] = None
    user_id: int
    storage_url: str
    duration: Optional[int] = None
    file_size: Optional[int] = None
    format: str
    status: str
    created_at: datetime

    model_config = {"from_attributes": True}
