"""Read models for learning paths: the admin's list, detail and people, and the learner's paths. No N+1 queries."""

from datetime import datetime

from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.db.models import (
    Course,
    JobProfile,
    LearningPath,
    LearningPathCourse,
    LearningPathProfile,
    Module,
    PathAssignment,
    User,
)
from app.schemas.paths import (
    LearnerPath,
    LearnerPathStep,
    PathCourse,
    PathDetail,
    PathPerson,
    PathPersonUser,
    PathRow,
    PersonStep,
)
from app.schemas.profiles import ProfileRef
from app.services import paths
from app.services.media import MediaResolver
from app.services.metrics import percentage


def _profiles_by_path(db: Session, path_ids: list[int]) -> dict[int, list[ProfileRef]]:
    profiles: dict[int, list[ProfileRef]] = {path_id: [] for path_id in path_ids}
    rows = (
        db.query(LearningPathProfile.path_id, JobProfile.id, JobProfile.name)
        .join(JobProfile, JobProfile.id == LearningPathProfile.profile_id)
        .filter(LearningPathProfile.path_id.in_(path_ids))
        .order_by(func.lower(JobProfile.name), JobProfile.id)
    )
    for path_id, profile_id, name in rows:
        profiles[path_id].append(ProfileRef(id=profile_id, name=name))
    return profiles


def _people_counts(db: Session, path_ids: list[int]) -> dict[int, tuple[int, int]]:
    """(people, completed) by path. Completion is derived from the courses, as the learners see it."""
    rows = (
        db.query(
            PathAssignment.path_id,
            func.count(PathAssignment.id),
            func.sum(case((paths.completed_clause(), 1), else_=0)),
        )
        .filter(PathAssignment.path_id.in_(path_ids))
        .group_by(PathAssignment.path_id)
    )
    return {path_id: (people, int(completed or 0)) for path_id, people, completed in rows}


def path_rows(db: Session, path_list: list[LearningPath]) -> list[PathRow]:
    path_ids = [path.id for path in path_list]
    if not path_ids:
        return []
    course_counts = dict(
        db.query(LearningPathCourse.path_id, func.count(LearningPathCourse.id))
        .filter(LearningPathCourse.path_id.in_(path_ids))
        .group_by(LearningPathCourse.path_id)
        .all()
    )
    profiles = _profiles_by_path(db, path_ids)
    people = _people_counts(db, path_ids)
    rows = []
    for path in path_list:
        total, completed = people.get(path.id, (0, 0))
        rows.append(
            PathRow(
                id=path.id,
                title=path.title,
                description=path.description or "",
                status=path.status,
                course_count=course_counts.get(path.id, 0),
                profiles=profiles[path.id],
                people_count=total,
                completed_count=completed,
                completion_rate=percentage(completed, total),
                updated_at=path.updated_at,
            )
        )
    return rows


def path_detail(db: Session, path: LearningPath) -> PathDetail:
    courses = (
        db.query(Course)
        .join(LearningPathCourse, LearningPathCourse.course_id == Course.id)
        .filter(LearningPathCourse.path_id == path.id)
        .order_by(LearningPathCourse.order, LearningPathCourse.id)
        .all()
    )
    module_counts = dict(
        db.query(Module.course_id, func.count(Module.id))
        .filter(Module.course_id.in_([course.id for course in courses]))
        .group_by(Module.course_id)
        .all()
    ) if courses else {}
    media = MediaResolver(db).prepare(extra_asset_ids=[course.cover_asset_id for course in courses])
    people, completed = _people_counts(db, [path.id]).get(path.id, (0, 0))
    return PathDetail(
        id=path.id,
        title=path.title,
        description=path.description or "",
        status=path.status,
        settings=paths.settings_of(path),
        profiles=_profiles_by_path(db, [path.id])[path.id],
        courses=[
            PathCourse(
                course_id=course.id,
                title=course.title,
                status=course.status,
                module_count=module_counts.get(course.id, 0),
                order=order,
                cover_url=media.url(course.cover_asset_id),
            )
            for order, course in enumerate(courses, start=1)
        ],
        people_count=people,
        completed_count=completed,
        published_at=path.published_at,
        created_at=path.created_at,
        updated_at=path.updated_at,
    )


def path_people(db: Session, path: LearningPath, user_ids: list[int] | None = None) -> list[PathPerson]:
    """Who takes the path and how far each one got, course by course (a handful of reads for any number)."""
    query = (
        db.query(PathAssignment, User)
        .join(User, User.id == PathAssignment.user_id)
        .filter(PathAssignment.path_id == path.id)
        .populate_existing()  # completion and deadlines are written in bulk, never into the session
    )
    if user_ids is not None:
        query = query.filter(PathAssignment.user_id.in_(user_ids))
    rows = query.order_by(func.lower(User.name), User.id).all()
    if not rows:
        return []
    steps = paths.path_steps(db, [path.id])[path.id]
    journeys = paths.journeys(db, [user.id for _, user in rows], course_ids=[s.course_id for s in steps], modules=True)
    profile_ids = {user.profile_id for _, user in rows if user.profile_id}
    profiles = {
        p.id: ProfileRef(id=p.id, name=p.name) for p in db.query(JobProfile).filter(JobProfile.id.in_(profile_ids))
    } if profile_ids else {}
    now = paths.utcnow()
    people = []
    for assignment, user in rows:
        journey = journeys[user.id]
        assigned = paths.assigned_path(path, assignment, steps)
        current = journey.current(assigned)
        people.append(
            PathPerson(
                user=PathPersonUser(
                    id=user.id,
                    name=user.name,
                    email=user.email,
                    position=user.position or "",
                    department=user.department or "",
                    profile=profiles.get(user.profile_id),
                ),
                source=assignment.source,
                assigned_at=assignment.assigned_at,
                due_at=assignment.due_at,
                completed_at=journey.completed_at(assigned),
                status=journey.status(assigned, now),
                progress_pct=journey.progress_pct(assigned),
                current_course_id=current.course_id if current else None,
                steps=[
                    PersonStep(
                        course_id=step.course_id,
                        status=journey.step_status(step.course_id),
                        progress_pct=journey.module_progress(step.course_id)[2],
                    )
                    for step in steps
                ],
            )
        )
    return people


def _learner_path(path: paths.AssignedPath, journey: paths.Journey, media: MediaResolver, now: datetime) -> LearnerPath:
    steps = []
    for step in path.steps:
        done, total, pct = journey.module_progress(step.course_id)
        lock = journey.locked_by.get(step.course_id)
        steps.append(
            LearnerPathStep(
                course_id=step.course_id,
                title=step.title,
                cover_url=media.url(step.cover_asset_id),
                status=journey.step_status(step.course_id),
                progress_pct=pct,
                total_modules=total,
                completed_modules=done,
                locked_by=lock.ref() if lock else None,
            )
        )
    current = journey.current(path)
    return LearnerPath(
        id=path.id,
        title=path.title,
        description=path.description,
        status=journey.status(path, now),
        progress_pct=journey.progress_pct(path),
        assigned_at=path.assigned_at,
        due_at=path.due_at,
        completed_at=journey.completed_at(path),
        current_course_id=current.course_id if current else None,
        steps=steps,
    )


def learner_paths(db: Session, user_id: int) -> list[LearnerPath]:
    """The published paths assigned to the learner, oldest first."""
    journey = paths.journey(db, user_id, modules=True)
    media = MediaResolver(db).prepare(
        extra_asset_ids=[step.cover_asset_id for path in journey.paths for step in path.steps]
    )
    now = paths.utcnow()
    return [_learner_path(path, journey, media, now) for path in journey.paths]
