"""Admin: team members (collaborators and admins) and each person's courses."""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_admin
from app.api.lookups import user_or_404
from app.core.security import hash_password
from app.db.models import Enrollment, RefreshToken, User
from app.schemas.learn import LearnerCourse
from app.schemas.users import Role, UserCreate, UserRow, UserUpdate
from app.services import progress
from app.services.learner_views import learner_course

router = APIRouter(prefix="/users", tags=["users"], dependencies=[Depends(require_admin)])


def _email_taken(db: Session, email: str, exclude_id: int | None = None) -> bool:
    query = db.query(User.id).filter(func.lower(User.email) == email.lower())
    if exclude_id is not None:
        query = query.filter(User.id != exclude_id)
    return query.first() is not None


def _demotes_self(changes: dict) -> bool:
    """True when the update would deactivate the account or take away its admin role."""
    return changes.get("is_active") is False or changes.get("role", "admin") != "admin"


def _enrollment_counts(db: Session, user_ids: list[int]) -> dict[int, tuple[int, int]]:
    """(enrolled, completed) course counts by user id; people without enrollments are absent."""
    if not user_ids:
        return {}
    rows = (
        db.query(Enrollment.user_id, func.count(Enrollment.id), progress.completed_enrollments())
        .filter(Enrollment.user_id.in_(user_ids))
        .group_by(Enrollment.user_id)
    )
    return {user_id: (enrolled, int(completed or 0)) for user_id, enrolled, completed in rows}


def _rows(db: Session, users: list[User]) -> list[UserRow]:
    counts = _enrollment_counts(db, [user.id for user in users])
    rows = []
    for user in users:
        enrolled, completed = counts.get(user.id, (0, 0))
        rows.append(
            UserRow(
                id=user.id,
                name=user.name,
                email=user.email,
                role=user.role,
                position=user.position or "",
                department=user.department or "",
                is_active=bool(user.is_active),
                created_at=user.created_at,
                enrolled_count=enrolled,
                completed_count=completed,
            )
        )
    return rows


@router.get("", response_model=list[UserRow])
def list_users(
    q: str | None = Query(None, max_length=200),
    role: Role | None = None,
    include_inactive: bool = True,
    db: Session = Depends(get_db),
):
    query = db.query(User)
    if q and q.strip():
        pattern = f"%{q.strip()}%"
        query = query.filter(or_(User.name.ilike(pattern), User.email.ilike(pattern), User.department.ilike(pattern)))
    if role:
        query = query.filter(User.role == role)
    if not include_inactive:
        query = query.filter(User.is_active.is_(True))
    return _rows(db, query.order_by(func.lower(User.name), User.id).all())


@router.post("", response_model=UserRow, status_code=status.HTTP_201_CREATED)
def create_user(payload: UserCreate, db: Session = Depends(get_db)):
    if _email_taken(db, payload.email):
        raise HTTPException(status.HTTP_409_CONFLICT, "Ya existe una persona con ese correo")
    user = User(
        name=payload.name.strip(),
        email=payload.email,
        role=payload.role,
        position=payload.position.strip(),
        department=payload.department.strip(),
        password_hash=hash_password(payload.password),
        is_active=True,
    )
    db.add(user)
    db.commit()
    return _rows(db, [user])[0]


@router.patch("/{user_id}", response_model=UserRow)
def update_user(user_id: int, payload: UserUpdate, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    user = user_or_404(db, user_id)
    # Explicit nulls mean "no change": name, email, role and is_active can't be empty.
    data = payload.model_dump(exclude_unset=True, exclude_none=True)
    if user.id == admin.id and _demotes_self(data):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No puedes desactivarte ni quitarte el rol de administrador")
    if "email" in data and _email_taken(db, data["email"], exclude_id=user.id):
        raise HTTPException(status.HTTP_409_CONFLICT, "Ya existe una persona con ese correo")
    password = data.pop("password", None)
    if password:
        user.password_hash = hash_password(password)
    for field, value in data.items():
        setattr(user, field, value.strip() if isinstance(value, str) else value)
    if password or data.get("is_active") is False:
        # A reset password or a deactivated account ends every open session.
        db.query(RefreshToken).filter(RefreshToken.user_id == user.id).update(
            {RefreshToken.revoked: True}, synchronize_session=False
        )
    db.commit()
    return _rows(db, [user])[0]


@router.get("/{user_id}/courses", response_model=list[LearnerCourse])
def user_courses(user_id: int, db: Session = Depends(get_db)):
    """A person's assigned courses and progress (drafts included, for the admin's view)."""
    user_or_404(db, user_id)
    enrollments = progress.user_enrollments(db, user_id)
    evaluations = progress.evaluations_by_module(db, [m for e in enrollments for m in e.course.modules])
    return [learner_course(e, progress.module_states(db, e, evaluations)) for e in enrollments]
