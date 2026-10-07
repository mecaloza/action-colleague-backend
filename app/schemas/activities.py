from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.common import UtcDatetime

ActivityType = Literal["decision", "inspection", "simulator", "quick"]
ActivityStatus = Literal["draft", "ready"]


class ActivityAdmin(BaseModel):
    id: int
    module_id: int
    order: int
    type: ActivityType
    title: str
    intro: str
    spec: dict[str, Any]
    version: int
    status: ActivityStatus
    image_asset_id: str | None = None
    image_url: str | None = None
    voice_status: str
    max_score: int
    problems: list[str]  # what keeps it from being published (empty: ready to publish)
    updated_at: UtcDatetime | None = None


class ActivityCreate(BaseModel):
    type: ActivityType
    title: str = Field(min_length=1, max_length=200)
    intro: str = Field(default="", max_length=600)
    spec: dict[str, Any]
    status: ActivityStatus = "draft"


class ActivityUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    intro: str | None = Field(default=None, max_length=600)
    spec: dict[str, Any] | None = None
    status: ActivityStatus | None = None


class ActivityOrder(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=20)


class ActivityImage(BaseModel):
    asset_id: str | None = Field(default=None, max_length=36)  # None takes the picture away
