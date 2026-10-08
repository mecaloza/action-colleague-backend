"""Admin: profiles (roles in the company). The people of a profile get the learning paths linked to it."""

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_admin
from app.api.lookups import profile_or_404
from app.db.models import JobProfile, LearningPathProfile, User
from app.schemas.profiles import ProfileCreate, ProfileItem, ProfileUpdate
from app.services import paths

router = APIRouter(prefix="/profiles", tags=["profiles"], dependencies=[Depends(require_admin)])

DUPLICATE = "Ya existe un perfil con ese nombre."


def _name_taken(db: Session, name: str, exclude_id: int | None = None) -> bool:
    query = db.query(JobProfile.id).filter(func.lower(JobProfile.name) == name.lower())
    if exclude_id is not None:
        query = query.filter(JobProfile.id != exclude_id)
    return query.first() is not None


def _commit(db: Session) -> None:
    """The same name saved at the same moment by another request: the unique constraint answers like the check."""
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, DUPLICATE) from exc


def _items(db: Session, profiles: list[JobProfile]) -> list[ProfileItem]:
    ids = [profile.id for profile in profiles]
    if not ids:
        return []
    people = dict(
        db.query(User.profile_id, func.count(User.id)).filter(User.profile_id.in_(ids)).group_by(User.profile_id).all()
    )
    linked = dict(
        db.query(LearningPathProfile.profile_id, func.count(LearningPathProfile.path_id))
        .filter(LearningPathProfile.profile_id.in_(ids))
        .group_by(LearningPathProfile.profile_id)
        .all()
    )
    return [
        ProfileItem(
            id=profile.id,
            name=profile.name,
            description=profile.description or "",
            user_count=people.get(profile.id, 0),
            path_count=linked.get(profile.id, 0),
        )
        for profile in profiles
    ]


@router.get("", response_model=list[ProfileItem])
def list_profiles(db: Session = Depends(get_db)):
    return _items(db, db.query(JobProfile).order_by(func.lower(JobProfile.name), JobProfile.id).all())


@router.post("", response_model=ProfileItem, status_code=status.HTTP_201_CREATED)
def create_profile(payload: ProfileCreate, db: Session = Depends(get_db)):
    if _name_taken(db, payload.name):
        raise HTTPException(status.HTTP_409_CONFLICT, DUPLICATE)
    profile = JobProfile(name=payload.name, description=payload.description.strip())
    db.add(profile)
    _commit(db)
    return _items(db, [profile])[0]


@router.patch("/{profile_id}", response_model=ProfileItem)
def update_profile(profile_id: int, payload: ProfileUpdate, db: Session = Depends(get_db)):
    profile = profile_or_404(db, profile_id)
    data = payload.model_dump(exclude_unset=True, exclude_none=True)
    if "name" in data and _name_taken(db, data["name"], exclude_id=profile.id):
        raise HTTPException(status.HTTP_409_CONFLICT, DUPLICATE)
    for field, value in data.items():
        setattr(profile, field, value.strip())
    _commit(db)
    return _items(db, [profile])[0]


@router.delete("/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_profile(profile_id: int, db: Session = Depends(get_db)):
    """Its people stay without a profile: they keep their courses, progress and completed paths, and lose the paths
    the profile gave them that they had not completed."""
    profile = profile_or_404(db, profile_id)
    paths.drop_profile(db, profile.id)
    db.delete(profile)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
