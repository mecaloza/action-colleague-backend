from pydantic import BaseModel, Field, model_validator

from app.schemas.common import UtcDatetime


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    password: str = Field(min_length=1, max_length=200)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1, max_length=300)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user_id: int
    role: str


class CurrentUser(BaseModel):
    id: int
    name: str
    email: str
    role: str
    position: str = ""
    department: str = ""
    is_active: bool = True
    created_at: UtcDatetime | None = None


class MeUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    current_password: str | None = Field(default=None, max_length=200)
    new_password: str | None = Field(default=None, min_length=8, max_length=128)

    @model_validator(mode="after")
    def _password_change_needs_current(self) -> "MeUpdate":
        if self.new_password and not self.current_password:
            raise ValueError("Escribe tu contraseña actual para cambiarla")
        return self
