import secrets
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db, require_admin
from app.core.config import get_settings
from app.core.security import (
    create_access_token,
    hash_password,
    hash_refresh_token,
    new_refresh_token,
    verify_password,
)
from app.db.models import RefreshToken, User
from app.schemas import LoginRequest, RefreshRequest, RegisterRequest, TokenResponse, UserOut

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
        preferred_language=user.preferred_language or "es",
    )


def _find_user_by_email(db: Session, email: str) -> User | None:
    return db.query(User).filter(User.email == email).first()


def _find_refresh_token(db: Session, token: str) -> RefreshToken | None:
    return db.query(RefreshToken).filter(RefreshToken.token == hash_refresh_token(token)).first()


@lru_cache
def _dummy_password_hash() -> str:
    return hash_password(secrets.token_urlsafe(16))


def _is_expired(expires_at: datetime) -> bool:
    if expires_at.tzinfo is None:  # SQLite drops the timezone
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at < datetime.now(timezone.utc)


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def register(payload: RegisterRequest, db: Session = Depends(get_db), _admin: User = Depends(require_admin)):
    if _find_user_by_email(db, payload.email):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Email already registered")
    user = User(
        name=payload.name,
        email=payload.email,
        password_hash=hash_password(payload.password),
        role=payload.role,
        position=payload.position,
        department=payload.department,
        preferred_language=payload.preferred_language,
        reports_to=payload.reports_to,
        permissions_json="[]",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    user = _find_user_by_email(db, payload.email)
    # Hash anyway for unknown emails so response times don't reveal which accounts exist.
    password_hash = user.password_hash if user else _dummy_password_hash()
    if not verify_password(payload.password, password_hash) or not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")
    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Account deactivated")
    return _issue_tokens(user, db)


@router.post("/refresh", response_model=TokenResponse)
def refresh(payload: RefreshRequest, db: Session = Depends(get_db)):
    db_token = _find_refresh_token(db, payload.refresh_token)
    if not db_token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid refresh token")
    if db_token.revoked:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Refresh token revoked")
    if _is_expired(db_token.expires_at):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Refresh token expired")

    user = db.get(User, db_token.user_id)
    if not user or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found or inactive")

    # Rotation: every refresh token is single-use. Compare-and-set so two concurrent
    # refreshes with the same token can't both succeed.
    consumed = db.execute(
        update(RefreshToken)
        .where(RefreshToken.id == db_token.id, RefreshToken.revoked.is_not(True))
        .values(revoked=True)
    ).rowcount
    if consumed != 1:
        db.rollback()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Refresh token revoked")
    return _issue_tokens(user, db)


@router.post("/logout")
def logout(payload: RefreshRequest, db: Session = Depends(get_db)):
    db_token = _find_refresh_token(db, payload.refresh_token)
    if db_token and not db_token.revoked:
        db_token.revoked = True
        db.commit()
    return {"detail": "Logged out"}


@router.get("/me", response_model=UserOut)
def me(current_user: User = Depends(get_current_user)):
    return current_user
