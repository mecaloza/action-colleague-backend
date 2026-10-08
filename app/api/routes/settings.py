"""Admin: the platform's parameters ("parámetros"): general, evaluation defaults and certificates."""

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_admin
from app.db.models import User
from app.schemas.settings import PlatformSettings
from app.services import settings as platform

router = APIRouter(prefix="/settings", tags=["settings"], dependencies=[Depends(require_admin)])


@router.get("", response_model=PlatformSettings)
def get_platform_settings(db: Session = Depends(get_db)):
    """Every parameter, with its default when it was never changed."""
    return platform.get_settings(db)


@router.patch("", response_model=PlatformSettings)
def update_platform_settings(
    changes: dict[str, Any] = Body(...), db: Session = Depends(get_db), admin: User = Depends(require_admin)
):
    """Only what is sent changes, section by section and key by key (`{"certificates": {"signer_name": "Ana"}}`); a
    null clears an optional value. A wrong value answers 422 saying which one, in Spanish."""
    try:
        settings = platform.update_settings(db, changes, admin)
    except platform.InvalidSettings as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    db.commit()
    return settings
