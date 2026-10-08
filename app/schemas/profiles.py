from pydantic import BaseModel, Field, field_validator

from app.schemas.common import required_text


class ProfileRef(BaseModel):
    id: int
    name: str


class ProfileCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)

    _name = field_validator("name")(classmethod(lambda cls, v: required_text(v)))


class ProfileUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)

    _name = field_validator("name")(classmethod(lambda cls, v: required_text(v) if v is not None else v))


class ProfileItem(BaseModel):
    id: int
    name: str
    description: str
    user_count: int
    path_count: int
