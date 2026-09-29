"""Authentication: login, single-use refresh tokens, logout and the signed-in person's own profile."""

import secrets
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, update
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.core.config import get_settings
from app.core.security import (
    create_access_token,
    hash_password,
    hash_refresh_token,
    new_refresh_token,
    verify_password,
)
from app.db.models import RefreshToken, User
from app.schemas.auth import CurrentUser, LoginRequest, MeUpdate, RefreshRequest, TokenResponse

router = APIRouter(prefix="/auth", tags=["auth"])


def _issue_tokens(user: User, db: Session) -> TokenResponse:
    refresh = new_refresh_token()
    db.add(
        RefreshToken(
            user_id=user.id,
            token=hash_refresh_token(refresh),
            expires_at=datetime.now(timezone.utc) + timedelta(days=get_settings().refresh_token_days),
        )
    )
    db.commit()
    return TokenResponse(
        access_token=create_access_token(user.id, user.role),
        refresh_token=refresh,
        user_id=user.id,
        role=user.role,
    )


def _find_user_by_email(db: Session, email: str) -> User | None:
    return db.query(User).filter(func.lower(User.email) == email.strip().lower()).first()


def _find_refresh_token(db: Session, token: str) -> RefreshToken | None:
    return db.query(RefreshToken).filter(RefreshToken.token == hash_refresh_token(token)).first()


@lru_cache
def _dummy_password_hash() -> str:
    return hash_password(secrets.token_urlsafe(16))


def _is_expired(expires_at: datetime) -> bool:
    if expires_at.tzinfo is None:  # SQLite drops the timezone
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at < datetime.now(timezone.utc)


def _session_expired() -> HTTPException:
    return HTTPException(status.HTTP_401_UNAUTHORIZED, "Tu sesión expiró. Vuelve a iniciar sesión.")


def _current_user(user: User) -> CurrentUser:
    return CurrentUser(
        id=user.id,
        name=user.name,
        email=user.email,
        role=user.role,
        position=user.position or "",
        department=user.department or "",
        is_active=bool(user.is_active),
        created_at=user.created_at,
    )


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    user = _find_user_by_email(db, payload.email)
    # Hash anyway for unknown emails so response times don't reveal which accounts exist.
    password_hash = user.password_hash if user else _dummy_password_hash()
    if not verify_password(payload.password, password_hash) or not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Correo o contraseña incorrectos")
    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Tu cuenta está desactivada. Habla con tu administrador.")
    return _issue_tokens(user, db)


@router.post("/refresh", response_model=TokenResponse)
def refresh(payload: RefreshRequest, db: Session = Depends(get_db)):
    db_token = _find_refresh_token(db, payload.refresh_token)
    if not db_token or db_token.revoked or _is_expired(db_token.expires_at):
        raise _session_expired()

    user = db.get(User, db_token.user_id)
    if not user or not user.is_active:
        raise _session_expired()

    # Rotation: every refresh token is single-use. Compare-and-set so two concurrent
    # refreshes with the same token can't both succeed.
    consumed = db.execute(
        update(RefreshToken)
        .where(RefreshToken.id == db_token.id, RefreshToken.revoked.is_not(True))
        .values(revoked=True)
    ).rowcount
    if consumed != 1:
        db.rollback()
        raise _session_expired()
    return _issue_tokens(user, db)


@router.post("/logout")
def logout(payload: RefreshRequest, db: Session = Depends(get_db)):
    db_token = _find_refresh_token(db, payload.refresh_token)
    if db_token and not db_token.revoked:
        db_token.revoked = True
        db.commit()
    return {"detail": "Sesión cerrada"}


@router.get("/me", response_model=CurrentUser)
def me(current_user: User = Depends(get_current_user)):
    return _current_user(current_user)


@router.patch("/me", response_model=CurrentUser)
def update_me(payload: MeUpdate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Change your own name or password (the current password is required to set a new one)."""
    if payload.name is not None:
        current_user.name = payload.name.strip()
    if payload.new_password:
        if not verify_password(payload.current_password or "", current_user.password_hash):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Tu contraseña actual no es correcta")
        current_user.password_hash = hash_password(payload.new_password)
    db.commit()
    return _current_user(current_user)
