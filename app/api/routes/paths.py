"""Admin: learning paths. Courses in an order for the people of some profiles, and anyone assigned by hand."""

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import delete, func, insert
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_admin
from app.db.models import (
    Course,
    JobProfile,
    LearningPath,
    LearningPathCourse,
    LearningPathProfile,
    PathAssignment,
    User,
)
from app.schemas.paths import (
    PathCoursesPut,
    PathCreate,
    PathDetail,
    PathPeopleAdd,
    PathPerson,
    PathRow,
    PathStatus,
    PathUpdate,
)
from app.services import paths
from app.services.path_views import path_detail, path_people, path_rows

router = APIRouter(prefix="/paths", tags=["paths"], dependencies=[Depends(require_admin)])


def _path(db: Session, path_id: int, lock: bool = False) -> LearningPath:
    query = db.query(LearningPath).filter(LearningPath.id == path_id)
    if lock:
        # Two admins changing the same path take turns: each sync then sees what the other one changed.
        query = query.with_for_update().populate_existing()
    path = query.first()
    if path is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ruta no encontrada")
    return path


def _check_profiles(db: Session, profile_ids: set[int]) -> None:
    if profile_ids and db.query(func.count(JobProfile.id)).filter(JobProfile.id.in_(profile_ids)).scalar() != len(
        profile_ids
    ):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Alguno de los perfiles no existe")


def _set_profiles(db: Session, path: LearningPath, profile_ids: set[int]) -> bool:
    """Link exactly these profiles; True when something changed."""
    current = {profile_id for (profile_id,) in db.query(LearningPathProfile.profile_id).filter(
        LearningPathProfile.path_id == path.id
    )}
    if profile_ids == current:
        return False
    if current - profile_ids:
        db.execute(
            delete(LearningPathProfile).where(
                LearningPathProfile.path_id == path.id, LearningPathProfile.profile_id.in_(current - profile_ids)
            ),
            execution_options=paths.NO_SYNC,
        )
    if profile_ids - current:
        db.execute(
            insert(LearningPathProfile),
            [{"path_id": path.id, "profile_id": profile_id} for profile_id in sorted(profile_ids - current)],
        )
    return True


@router.get("", response_model=list[PathRow])
def list_paths(status_filter: PathStatus | None = Query(None, alias="status"), db: Session = Depends(get_db)):
    query = db.query(LearningPath)
    # Archived paths only show up when asked for, as archived courses do.
    query = query.filter(LearningPath.status == status_filter) if status_filter else query.filter(
        LearningPath.status != "archived"
    )
    return path_rows(db, query.order_by(LearningPath.updated_at.desc(), LearningPath.id.desc()).all())


@router.post("", response_model=PathDetail, status_code=status.HTTP_201_CREATED)
def create_path(payload: PathCreate, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    path = LearningPath(
        title=payload.title, description=payload.description, status="draft", settings={}, created_by=admin.id
    )
    db.add(path)
    db.commit()
    return path_detail(db, path)


@router.get("/{path_id}", response_model=PathDetail)
def get_path(path_id: int, db: Session = Depends(get_db)):
    return path_detail(db, _path(db, path_id))


@router.patch("/{path_id}", response_model=PathDetail)
def update_path(
    path_id: int, payload: PathUpdate, db: Session = Depends(get_db), admin: User = Depends(require_admin)
):
    """
    Publishing gives the path to the people of its profiles and enrolls everyone in its courses. Back to draft or
    archived, learners stop seeing it and its courses stop being locked by it; who has it is kept. Linking other
    profiles adds and removes the people they bring; a new deadline applies to whoever has not completed the path.
    """
    path = _path(db, path_id, lock=True)
    profile_ids = set(payload.profile_ids) if payload.profile_ids is not None else None
    if profile_ids is not None:
        _check_profiles(db, profile_ids)
    was_published, due_days = path.status == "published", paths.settings_of(path).due_days
    # Explicit nulls mean "no change": a path always has a title and a status.
    for field, value in payload.model_dump(exclude_unset=True, exclude_none=True,
                                           include={"title", "description", "status"}).items():
        setattr(path, field, value)
    if path.status == "published":
        path.published_at = path.published_at or paths.utcnow()
    if payload.settings is not None:
        path.settings = {**(path.settings or {}), **payload.settings.model_dump(exclude_unset=True)}
    profiles_changed = profile_ids is not None and _set_profiles(db, path, profile_ids)
    if profiles_changed:
        path.updated_at = paths.utcnow()  # its own row is untouched otherwise
    db.flush()
    if paths.settings_of(path).due_days != due_days:
        paths.recompute_due_dates(db, path)
    if profiles_changed or (path.status == "published" and not was_published):
        paths.sync_path(db, path, assigned_by=admin.id)
    db.commit()
    return path_detail(db, path)


@router.put("/{path_id}/courses", response_model=PathDetail)
def put_path_courses(
    path_id: int, payload: PathCoursesPut, db: Session = Depends(get_db), admin: User = Depends(require_admin)
):
    """The path's courses, in order. Its people are enrolled in the new ones (if it is published)."""
    path = _path(db, path_id, lock=True)
    course_ids = payload.course_ids
    if len(set(course_ids)) != len(course_ids):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Cada curso puede estar una sola vez en la ruta")
    courses = {course.id: course for course in db.query(Course).filter(Course.id.in_(course_ids))} if course_ids else {}
    if len(courses) != len(course_ids):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Alguno de los cursos no existe")
    current = {course_id for (course_id,) in db.query(LearningPathCourse.course_id).filter(
        LearningPathCourse.path_id == path.id
    )}
    # One archived while in the path can stay (the list is sent whole, to reorder); it can't be added.
    if any(courses[course_id].status == "archived" and course_id not in current for course_id in course_ids):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Un curso archivado no se puede agregar a una ruta")
    db.execute(delete(LearningPathCourse).where(LearningPathCourse.path_id == path.id), execution_options=paths.NO_SYNC)
    if course_ids:
        db.execute(
            insert(LearningPathCourse),
            [{"path_id": path.id, "course_id": course_id, "order": order}
             for order, course_id in enumerate(course_ids, start=1)],
        )
    path.updated_at = paths.utcnow()
    db.flush()
    paths.sync_path(db, path, assigned_by=admin.id)
    db.commit()
    return path_detail(db, path)


@router.delete("/{path_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_path(path_id: int, db: Session = Depends(get_db)):
    """The path, its links and who had it go; everyone keeps their courses and progress."""
    path = _path(db, path_id, lock=True)
    for model in (PathAssignment, LearningPathCourse, LearningPathProfile):
        db.execute(delete(model).where(model.path_id == path.id), execution_options=paths.NO_SYNC)
    db.delete(path)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{path_id}/people", response_model=list[PathPerson])
def list_path_people(path_id: int, db: Session = Depends(get_db)):
    return path_people(db, _path(db, path_id))


@router.post("/{path_id}/people", response_model=list[PathPerson])
def add_path_people(
    path_id: int, payload: PathPeopleAdd, db: Session = Depends(get_db), admin: User = Depends(require_admin)
):
    """Assign the path by hand (anyone active, admins included). Those who already have it keep it as it was."""
    path = _path(db, path_id, lock=True)
    user_ids = sorted(set(payload.user_ids))
    active = db.query(func.count(User.id)).filter(User.id.in_(user_ids), User.is_active.is_(True)).scalar()
    if active != len(user_ids):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Alguno de los usuarios no existe o está inactivo")
    paths.assign(db, path, user_ids, assigned_by=admin.id)
    db.commit()
    return path_people(db, path, user_ids)


@router.delete("/{path_id}/people/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_path_person(path_id: int, user_id: int, db: Session = Depends(get_db)):
    """Take the path away from someone; their courses and progress stay. Not while their profile gives it to them:
    the next change of the path would give it back."""
    path = _path(db, path_id, lock=True)
    assignment = (
        db.query(PathAssignment).filter(PathAssignment.path_id == path.id, PathAssignment.user_id == user_id).first()
    )
    if assignment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Esta persona no tiene la ruta")
    profile = paths.backing_profile(db, path.id, user_id)
    if profile is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Esta persona tiene la ruta por su perfil «{profile.name}». Cambia su perfil para quitarla.",
        )
    db.delete(assignment)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
