"""Admin: team members (collaborators and admins), their profile, each person's courses and overview ("ficha"), and
giving someone their quiz attempts back."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_admin
from app.api.lookups import module_or_404, user_or_404
from app.core.security import hash_password
from app.db.models import Enrollment, JobProfile, ModuleProgress, RefreshToken, User
from app.schemas.learn import LearnerCourse
from app.schemas.people import PersonOverview, QuizReset
from app.schemas.profiles import ProfileRef
from app.schemas.users import Role, UserCreate, UserRow, UserUpdate
from app.services import certificates, paths, people, progress, quiz
from app.services.learner_views import learner_course

logger = logging.getLogger(__name__)
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


def _profiles(db: Session, users: list[User]) -> dict[int, ProfileRef]:
    ids = {user.profile_id for user in users if user.profile_id}
    if not ids:
        return {}
    return {profile.id: ProfileRef(id=profile.id, name=profile.name)
            for profile in db.query(JobProfile).filter(JobProfile.id.in_(ids))}


def _rows(db: Session, users: list[User]) -> list[UserRow]:
    counts = _enrollment_counts(db, [user.id for user in users])
    profiles = _profiles(db, users)
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
                profile=profiles.get(user.profile_id),
                is_active=bool(user.is_active),
                created_at=user.created_at,
                enrolled_count=enrolled,
                completed_count=completed,
            )
        )
    return rows


def _check_profile(db: Session, profile_id: int | None) -> None:
    if profile_id is not None and db.get(JobProfile, profile_id) is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "El perfil no existe")


@router.get("", response_model=list[UserRow])
def list_users(
    q: str | None = Query(None, max_length=200),
    role: Role | None = None,
    profile_id: int | None = None,
    include_inactive: bool = True,
    db: Session = Depends(get_db),
):
    query = db.query(User)
    if q and q.strip():
        pattern = f"%{q.strip()}%"
        query = query.filter(or_(User.name.ilike(pattern), User.email.ilike(pattern), User.department.ilike(pattern)))
    if role:
        query = query.filter(User.role == role)
    if profile_id is not None:
        query = query.filter(User.profile_id == profile_id)
    if not include_inactive:
        query = query.filter(User.is_active.is_(True))
    return _rows(db, query.order_by(func.lower(User.name), User.id).all())


@router.post("", response_model=UserRow, status_code=status.HTTP_201_CREATED)
def create_user(payload: UserCreate, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    if _email_taken(db, payload.email):
        raise HTTPException(status.HTTP_409_CONFLICT, "Ya existe una persona con ese correo")
    _check_profile(db, payload.profile_id)
    user = User(
        name=payload.name.strip(),
        email=payload.email,
        role=payload.role,
        position=payload.position.strip(),
        department=payload.department.strip(),
        profile_id=payload.profile_id,
        password_hash=hash_password(payload.password),
        is_active=True,
    )
    db.add(user)
    db.flush()
    if user.profile_id:
        paths.sync_user(db, user, assigned_by=admin.id)  # the learning paths of their profile
    db.commit()
    return _rows(db, [user])[0]


@router.get("/{user_id}", response_model=UserRow)
def get_user(user_id: int, db: Session = Depends(get_db)):
    return _rows(db, [user_or_404(db, user_id)])[0]


@router.patch("/{user_id}", response_model=UserRow)
def update_user(user_id: int, payload: UserUpdate, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    user = user_or_404(db, user_id)
    # Explicit nulls mean "no change": name, email, role and is_active can't be empty. A null profile removes it.
    data = payload.model_dump(exclude_unset=True, exclude_none=True, exclude={"profile_id"})
    if user.id == admin.id and _demotes_self(data):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No puedes desactivarte ni quitarte el rol de administrador")
    if "email" in data and _email_taken(db, data["email"], exclude_id=user.id):
        raise HTTPException(status.HTTP_409_CONFLICT, "Ya existe una persona con ese correo")
    if "profile_id" in payload.model_fields_set:
        _check_profile(db, payload.profile_id)
        data["profile_id"] = payload.profile_id
    # What decides the paths a person gets by profile: compared before and after the changes.
    membership = (user.profile_id, user.role, bool(user.is_active))
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
    if (user.profile_id, user.role, bool(user.is_active)) != membership:
        db.flush()
        paths.sync_user(db, user, assigned_by=admin.id)
    db.commit()
    return _rows(db, [user])[0]


@router.get("/{user_id}/courses", response_model=list[LearnerCourse])
def user_courses(user_id: int, db: Session = Depends(get_db)):
    """A person's assigned courses and progress (drafts included, for the admin's view), locks as they see them, and
    the certificates they earned."""
    user_or_404(db, user_id)
    enrollments = progress.user_enrollments(db, user_id)
    evaluations = progress.evaluations_by_module(db, [m for e in enrollments for m in e.course.modules])
    journey = paths.journey(db, user_id)
    earned = certificates.learner_refs(db, user_id, course_ids=[e.course_id for e in enrollments]).courses
    return [
        learner_course(
            e, progress.module_states(db, e, evaluations, course_unlocked=e.course_id not in journey.locked_by),
            journey=journey, certificate=earned.get(e.course_id),
        )
        for e in enrollments
    ]


@router.get("/{user_id}/overview", response_model=PersonOverview)
def user_overview(user_id: int, db: Session = Depends(get_db)):
    """Everything about a person's learning on one page: their numbers, paths, courses module by module (quiz and
    activities), certificates and the latest things they did. A fixed number of reads, whatever their size."""
    return people.overview(db, user_or_404(db, user_id))


@router.post("/{user_id}/modules/{module_id}/quiz-reset", response_model=QuizReset)
def reset_quiz_attempts(
    user_id: int, module_id: int, db: Session = Depends(get_db), admin: User = Depends(require_admin)
):
    """Give a person their attempts at a module's quiz back (out of them, a sequential path would keep them stuck),
    unless they passed it. What they answered stays in their history, and their next attempt is numbered after it."""
    user = user_or_404(db, user_id)
    module = module_or_404(db, module_id)
    evaluation = module.evaluation
    if not quiz.evaluation_questions(evaluation):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Este módulo no tiene evaluación")
    enrollment = (
        db.query(Enrollment).filter(Enrollment.user_id == user.id, Enrollment.course_id == module.course_id).first()
    )
    if enrollment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Esta persona no está inscrita en el curso")
    # Locked as a submission locks it: one in flight is counted first, then given back too.
    record = (
        db.query(ModuleProgress)
        .filter(ModuleProgress.enrollment_id == enrollment.id, ModuleProgress.module_id == module.id)
        .with_for_update()
        .populate_existing()
        .first()
    )
    if record is not None and record.passed:
        raise HTTPException(status.HTTP_409_CONFLICT, "La persona ya aprobó esta evaluación.")
    given_back = (record.attempts or 0) if record is not None else 0
    if given_back:
        record.attempts = 0
    db.commit()
    logger.info("quiz_attempts_reset", extra={"admin_id": admin.id, "user_id": user.id, "module_id": module.id,
                                              "attempts_given_back": given_back})
    return QuizReset(attempts_used=0, max_attempts=quiz.max_attempts(evaluation))
