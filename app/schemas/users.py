import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.schemas.common import UtcDatetime, required_text

Role = Literal["admin", "collaborator"]
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalize_email(value: str) -> str:
    value = value.strip().lower()
    if not _EMAIL_RE.match(value) or len(value) > 200:
        raise ValueError("Correo inválido")
    return value


class UserCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    email: str
    password: str = Field(min_length=8, max_length=128)
    role: Role = "collaborator"
    position: str = Field(default="", max_length=200)
    department: str = Field(default="", max_length=200)

    _email = field_validator("email")(classmethod(lambda cls, v: normalize_email(v)))
    _name = field_validator("name")(classmethod(lambda cls, v: required_text(v)))


class UserUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    email: str | None = None
    password: str | None = Field(default=None, min_length=8, max_length=128)
    role: Role | None = None
    position: str | None = Field(default=None, max_length=200)
    department: str | None = Field(default=None, max_length=200)
    is_active: bool | None = None

    _email = field_validator("email")(classmethod(lambda cls, v: normalize_email(v) if v is not None else v))
    _name = field_validator("name")(classmethod(lambda cls, v: required_text(v) if v is not None else v))


class UserRow(BaseModel):
    id: int
    name: str
    email: str
    role: str
    position: str
    department: str
    is_active: bool
    created_at: UtcDatetime | None = None
    enrolled_count: int = 0
    completed_count: int = 0
