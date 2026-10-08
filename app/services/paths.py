"""
Learning paths: who takes each one, their courses, the order they open in and when a path is completed.

- Assignments. A person takes a path because of their profile (active collaborators of a linked profile, while the
  path is published; it follows the profile, but once completed it stays as history) or because an admin assigned it
  (it stays whatever their profile). Each person of a published path is enrolled in every course of the path, and
  keeps those enrollments while they have it. Nothing here ever deletes an enrollment or progress: whoever leaves a
  path keeps what they did.
- Locks, computed on read and never stored. In a sequential path a course opens once every earlier published course is
  completed. A course the person already started stays open (a reorder or a new path never takes back what they
  began); courses that are not published are skipped (learners can't open them anyway).
- Completion. A path is completed once every one of its published courses is (and it has one). It is stored on the
  assignment by the completion transitions of `progress` and derived again on read; storing it issues the path's
  certificate (`certificates`).

Every write is one statement whatever the audience (a profile with hundreds of people, a course in many paths).
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from pydantic import ValidationError
from sqlalchemy import DateTime, Integer, and_, case, delete, exists, func, literal, null, or_, select, union, update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import Session

from app.db.models import (
    ActivityAttempt,
    Course,
    Enrollment,
    JobProfile,
    LearningPath,
    LearningPathCourse,
    LearningPathProfile,
    Module,
    ModuleProgress,
    PathAssignment,
    User,
)
from app.schemas.paths import CourseRef, PathSettings
from app.services import certificates
from app.services.metrics import percentage

NO_SYNC = {"synchronize_session": False}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def aware(value: datetime | None) -> datetime | None:
    """SQLite hands timezone-aware columns back naive (they are UTC): comparable again."""
    return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value


def settings_of(path: LearningPath) -> PathSettings:
    try:
        return PathSettings.model_validate(path.settings or {})
    except ValidationError:  # edited by hand into something invalid: the defaults, never a broken path
        return PathSettings()


def _insert(db: Session):
    return postgresql.insert if db.get_bind().dialect.name == "postgresql" else sqlite.insert


# ── Completion ────────────────────────────────────────────────────────


def _published_course_of_path(*conditions):
    """Correlated: a published course of the assignment's path (meeting `conditions`)."""
    return (
        exists()
        .where(
            LearningPathCourse.path_id == PathAssignment.path_id,
            Course.id == LearningPathCourse.course_id,
            Course.status == "published",
            *conditions,
        )
        .correlate(PathAssignment)
    )


def completed_clause():
    """SQL, per `path_assignments` row: its person completed every published course of the path (one at least)."""
    # Explicit correlation: two levels down, a table is only correlated when asked to.
    completed = (
        exists()
        .where(
            Enrollment.user_id == PathAssignment.user_id,
            Enrollment.course_id == LearningPathCourse.course_id,
            Enrollment.status == "completed",
        )
        .correlate(PathAssignment, LearningPathCourse)
    )
    return and_(_published_course_of_path(), ~_published_course_of_path(~completed))


def _scope(path_ids=None, user_ids=None, course_ids=None) -> list:
    conditions = []
    if path_ids is not None:
        conditions.append(PathAssignment.path_id.in_(path_ids))
    if user_ids is not None:
        conditions.append(PathAssignment.user_id.in_(user_ids))
    if course_ids is not None:
        with_course = select(LearningPathCourse.path_id).where(LearningPathCourse.course_id.in_(course_ids))
        conditions.append(PathAssignment.path_id.in_(with_course))
    return conditions


def refresh_completion(db: Session, *, path_ids=None, user_ids=None, course_ids=None) -> list[int]:
    """
    Store which assignments of published paths are completed, and clear the ones that have a course to do again (a
    course added to the path, a module added to a course): the same rule as an enrollment's, except for a completed
    path kept only as history (its profile no longer gives it: reopened, the next sync would drop it). Draft and
    archived paths keep what they had. Three statements; returns the assignments that just became completed, whose
    certificates are issued (two more, only then).
    """
    db.flush()  # the enrollments this request changed are read below
    published = select(LearningPath.id).where(LearningPath.status == "published")
    scope = [PathAssignment.path_id.in_(published), *_scope(path_ids, user_ids, course_ids)]
    # One at a time per assignment (in id order): of two courses of a path completed at once (two tabs, a learner and
    # an admin), the second one waits here and then reads the first one's completion, committed.
    db.execute(select(PathAssignment.id).where(*scope).order_by(PathAssignment.id).with_for_update(of=PathAssignment))
    done = completed_clause()
    completed = db.execute(
        update(PathAssignment)
        .where(*scope, PathAssignment.completed_at.is_(None), done)
        .values(completed_at=utcnow())
        .returning(PathAssignment.id),
        execution_options=NO_SYNC,
    ).scalars().all()
    history = and_(PathAssignment.source == "profile", ~backed_by_profile())
    db.execute(
        update(PathAssignment)
        .where(*scope, PathAssignment.completed_at.isnot(None), ~done, ~history)
        .values(completed_at=None),
        execution_options=NO_SYNC,
    )
    certificates.issue_for_assignments(db, completed)
    return completed


# ── Assignments and enrollments ───────────────────────────────────────


def ensure_enrollments(db: Session, *, path_ids=None, user_ids=None, assigned_by: int | None = None) -> None:
    """Enroll the people of published paths in every course of their paths (whatever its status: learners only ever
    see published courses). One statement; existing enrollments are left as they are."""
    rows = (
        select(
            PathAssignment.user_id,
            LearningPathCourse.course_id,
            literal("assigned"),
            literal(0.0),
            literal(assigned_by, Integer),
        )
        .join(LearningPath, LearningPath.id == PathAssignment.path_id)
        .join(LearningPathCourse, LearningPathCourse.path_id == PathAssignment.path_id)
        # Always a WHERE: SQLite reads "INSERT ... SELECT ... ON CONFLICT" ambiguously without one.
        .where(LearningPath.status == "published", *_scope(path_ids, user_ids))
        .distinct()
    )
    columns = ["user_id", "course_id", "status", "progress_pct", "assigned_by"]
    insert = _insert(db)(Enrollment).from_select(columns, rows)
    db.execute(insert.on_conflict_do_nothing(index_elements=["user_id", "course_id"]))


def _due_at(paths: list[LearningPath], now: datetime, path_column):
    """Each path's deadline for someone assigned `now`, as SQL (one CASE, NULL for paths without one)."""
    deadlines = {
        path.id: literal(now + timedelta(days=days), DateTime(timezone=True))
        for path in paths
        if (days := settings_of(path).due_days)
    }
    return case(deadlines, value=path_column, else_=null()) if deadlines else null()


def _add_by_profile(db: Session, *, path_ids=None, user_ids=None, assigned_by: int | None = None) -> None:
    """The active collaborators of each linked profile get its published paths (admins never get one by profile)."""
    query = db.query(LearningPath).filter(LearningPath.status == "published")
    if path_ids is not None:
        query = query.filter(LearningPath.id.in_(path_ids))
    if user_ids is not None:
        their_profiles = select(User.profile_id).where(User.id.in_(user_ids))
        linked = select(LearningPathProfile.path_id).where(LearningPathProfile.profile_id.in_(their_profiles))
        query = query.filter(LearningPath.id.in_(linked))
    paths = query.all()
    if not paths:
        return
    now = utcnow()
    people = (
        select(
            LearningPathProfile.path_id,
            User.id,
            literal("profile"),
            literal(assigned_by, Integer),
            literal(now, DateTime(timezone=True)),
            _due_at(paths, now, LearningPathProfile.path_id),
        )
        .join(User, User.profile_id == LearningPathProfile.profile_id)
        .where(
            LearningPathProfile.path_id.in_([path.id for path in paths]),
            User.is_active.is_(True),
            User.role == "collaborator",
        )
    )
    if user_ids is not None:
        people = people.where(User.id.in_(user_ids))
    columns = ["path_id", "user_id", "source", "assigned_by", "assigned_at", "due_at"]
    insert = _insert(db)(PathAssignment).from_select(columns, people)
    db.execute(insert.on_conflict_do_nothing(index_elements=["path_id", "user_id"]))


def backed_by_profile():
    """Correlated, per `path_assignments` row: a profile gives the path to its person (a collaborator whose profile is
    linked to it; deactivated people keep theirs). The rule of `_add_by_profile` and `backing_profile` too."""
    return (
        exists()
        .where(
            LearningPathProfile.path_id == PathAssignment.path_id,
            User.id == PathAssignment.user_id,
            User.profile_id == LearningPathProfile.profile_id,
            User.role == "collaborator",
        )
        .correlate(PathAssignment)
    )


def _drop_unbacked(db: Session, *, path_ids=None, user_ids=None) -> None:
    """Assignments given by a profile that no longer gives the path go; completed ones stay (they are history), also
    those whose courses were completed while the path was not published (stored once it is published again)."""
    db.flush()
    db.execute(
        delete(PathAssignment).where(
            PathAssignment.source == "profile",
            PathAssignment.completed_at.is_(None),
            ~completed_clause(),
            ~backed_by_profile(),
            *_scope(path_ids, user_ids),
        ),
        execution_options=NO_SYNC,
    )


def sync_path(db: Session, path: LearningPath, assigned_by: int | None = None) -> None:
    """After a path is published, or its profiles or courses change: its people, their enrollments, completion. Those
    who leave go first: no enrollment of theirs is written below (see `sync_user` on the order of locks)."""
    _drop_unbacked(db, path_ids=[path.id])
    if path.status == "published":
        _add_by_profile(db, path_ids=[path.id], assigned_by=assigned_by)
        ensure_enrollments(db, path_ids=[path.id], assigned_by=assigned_by)
        refresh_completion(db, path_ids=[path.id])  # completed by someone already, or with a course to do again


def hold_profiles(db: Session, profile_ids: Iterable[int]) -> None:
    """Profiles just linked to a path: whoever is being given one of them right now (`sync_user`) waits for this
    change, then reads the new link. NO KEY UPDATE: their profile change itself (a foreign key check) doesn't wait."""
    db.execute(
        select(JobProfile.id)
        .where(JobProfile.id.in_(list(profile_ids)))
        .order_by(JobProfile.id)
        .with_for_update(key_share=True, of=JobProfile)
    )


def _wait_for_path_changes(db: Session, user: User) -> None:
    """
    Read the person's paths only once any change to them in flight is committed: publishing a path of their profile
    or one it gave them, changing its profiles (`update_path` holds the path's row), linking their profile to another
    path (`hold_profiles`). That change could not see this person's new profile, nor this sync that change. Paths
    first, then the profile, the order `update_path` takes them in; shared locks, so syncs of different people never
    wait for each other.
    """
    linked = select(LearningPathProfile.path_id).where(LearningPathProfile.profile_id == user.profile_id)
    given = select(PathAssignment.path_id).where(PathAssignment.user_id == user.id, PathAssignment.source == "profile")
    db.execute(
        select(LearningPath.id)
        .where(or_(LearningPath.id.in_(linked), LearningPath.id.in_(given)))
        .order_by(LearningPath.id)
        .with_for_update(read=True, of=LearningPath)
    )
    if user.profile_id is not None:
        db.execute(
            select(JobProfile.id).where(JobProfile.id == user.profile_id).with_for_update(read=True, of=JobProfile)
        )


def sync_user(db: Session, user: User, assigned_by: int | None = None) -> None:
    """
    After a person's profile, role or active state changes: the paths of their profile, and only those. Enrollments
    are written before any of their assignments is locked, the order of a course they may be completing right now
    (its enrollment, then their assignments); so the paths they lose go last, their completion stored by then.
    """
    _wait_for_path_changes(db, user)  # no assignment locked yet while waiting
    _add_by_profile(db, user_ids=[user.id], assigned_by=assigned_by)
    ensure_enrollments(db, user_ids=[user.id], assigned_by=assigned_by)
    refresh_completion(db, user_ids=[user.id])
    _drop_unbacked(db, user_ids=[user.id])


def assign(db: Session, path: LearningPath, user_ids: list[int], assigned_by: int) -> None:
    """Assign people by hand. Someone who already has the path keeps its date and deadline; one who has it by profile
    now has it by hand (it stays if their profile changes)."""
    now = utcnow()
    days = settings_of(path).due_days
    rows = [
        {"path_id": path.id, "user_id": user_id, "source": "manual", "assigned_by": assigned_by, "assigned_at": now,
         "due_at": now + timedelta(days=days) if days else None}
        for user_id in user_ids
    ]
    db.execute(_insert(db)(PathAssignment).values(rows).on_conflict_do_nothing(index_elements=["path_id", "user_id"]))
    published = path.status == "published"
    if published:
        ensure_enrollments(db, path_ids=[path.id], user_ids=user_ids, assigned_by=assigned_by)
    # After the enrollments, as in `sync_user`: no assignment is locked while one of them waits.
    db.execute(
        update(PathAssignment)
        .where(PathAssignment.path_id == path.id, PathAssignment.user_id.in_(user_ids),
               PathAssignment.source == "profile")
        .values(source="manual", assigned_by=assigned_by),
        execution_options=NO_SYNC,
    )
    if published:
        refresh_completion(db, path_ids=[path.id], user_ids=user_ids)


def recompute_due_dates(db: Session, path: LearningPath) -> None:
    """The path's deadline changed: whoever is still on it gets the new one, counted from the day they got the path."""
    db.flush()
    days = settings_of(path).due_days
    pending = (PathAssignment.path_id == path.id, PathAssignment.completed_at.is_(None))
    if days is None:
        db.execute(update(PathAssignment).where(*pending).values(due_at=None), execution_options=NO_SYNC)
        return
    rows = db.execute(select(PathAssignment.id, PathAssignment.assigned_at).where(*pending)).all()
    if rows:  # by primary key, in one batch
        db.execute(
            update(PathAssignment),
            [{"id": row_id, "due_at": aware(assigned_at) + timedelta(days=days)} for row_id, assigned_at in rows],
        )


def backing_profile(db: Session, path_id: int, user_id: int) -> JobProfile | None:
    """The profile that gives this collaborator the path, if any: removing the path by hand would not last."""
    return (
        db.query(JobProfile)
        .join(User, User.profile_id == JobProfile.id)
        .join(LearningPathProfile, LearningPathProfile.profile_id == JobProfile.id)
        .filter(User.id == user_id, User.role == "collaborator", LearningPathProfile.path_id == path_id)
        .first()
    )


def path_with_course(db: Session, user_id: int, course_id: int) -> LearningPath | None:
    """The published path (the first one they got) through which this person takes the course: their enrollment in it
    can't go while they have the path, or the courses after it would stay locked."""
    return (
        db.query(LearningPath)
        .join(PathAssignment, PathAssignment.path_id == LearningPath.id)
        .join(LearningPathCourse, LearningPathCourse.path_id == LearningPath.id)
        .filter(
            PathAssignment.user_id == user_id,
            LearningPathCourse.course_id == course_id,
            LearningPath.status == "published",
        )
        .order_by(PathAssignment.assigned_at, PathAssignment.id)
        .first()
    )


def drop_profile(db: Session, profile_id: int) -> None:
    """Before a profile is deleted: its people keep their courses and completed paths, and lose the rest it gave."""
    user_ids = db.execute(select(User.id).where(User.profile_id == profile_id)).scalars().all()
    db.execute(update(User).where(User.profile_id == profile_id).values(profile_id=None), execution_options=NO_SYNC)
    db.execute(
        delete(LearningPathProfile).where(LearningPathProfile.profile_id == profile_id), execution_options=NO_SYNC
    )
    if user_ids:
        _drop_unbacked(db, user_ids=user_ids)


def forget_course(db: Session, course_id: int) -> list[int]:
    """A course is being deleted: out of every path. Returns those paths (their completion is refreshed after)."""
    path_ids = db.execute(
        select(LearningPathCourse.path_id).where(LearningPathCourse.course_id == course_id)
    ).scalars().all()
    db.execute(delete(LearningPathCourse).where(LearningPathCourse.course_id == course_id), execution_options=NO_SYNC)
    return path_ids


# ── A person's paths, as they see them ────────────────────────────────


@dataclass(frozen=True)
class PathStep:
    course_id: int
    title: str
    cover_asset_id: str | None = None

    def ref(self) -> CourseRef:
        return CourseRef(course_id=self.course_id, title=self.title)


@dataclass
class AssignedPath:
    id: int
    title: str
    description: str
    settings: PathSettings
    source: str
    assigned_at: datetime | None
    due_at: datetime | None
    completed_at: datetime | None  # as stored: the journey derives it again
    steps: list[PathStep]  # its published courses, in order


@dataclass
class CourseState:
    """A person's enrollment in a course of their paths."""

    status: str
    completed_at: datetime | None
    started: bool = False  # anything done in it: a module, a quiz attempt, a video position, a play
    completed_modules: int = 0


@dataclass
class Journey:
    """A person's published paths with the state of their courses and the locks."""

    paths: list[AssignedPath] = field(default_factory=list)  # oldest assignment first
    courses: dict[int, CourseState] = field(default_factory=dict)
    locked_by: dict[int, PathStep] = field(default_factory=dict)  # a locked course -> the course to complete first
    modules: dict[int, int] = field(default_factory=dict)  # course id -> its modules (when loaded)

    def completed(self, course_id: int) -> bool:
        state = self.courses.get(course_id)
        return state is not None and state.status == "completed"

    def started(self, course_id: int) -> bool:
        state = self.courses.get(course_id)
        return state is not None and state.started

    def _begun(self, course_id: int) -> bool:
        state = self.courses.get(course_id)
        return state is not None and (state.started or state.status != "assigned")

    def step_status(self, course_id: int) -> str:
        if self.completed(course_id):
            return "completed"
        if course_id in self.locked_by:
            return "locked"
        return "in_progress" if self._begun(course_id) else "available"

    def module_progress(self, course_id: int) -> tuple[int, int, float]:
        """(completed modules, modules, percentage) of a course; needs `modules` loaded."""
        total = self.modules.get(course_id, 0)
        state = self.courses.get(course_id)
        done = min(state.completed_modules, total) if state else 0
        return done, total, 100.0 if self.completed(course_id) else percentage(done, total)

    def is_completed(self, path: AssignedPath) -> bool:
        return bool(path.steps) and all(self.completed(step.course_id) for step in path.steps)

    def current(self, path: AssignedPath) -> PathStep | None:
        """The course to go on with: the first one neither completed nor locked."""
        return next((step for step in path.steps if self.step_status(step.course_id) not in ("completed", "locked")),
                    None)

    def completed_at(self, path: AssignedPath) -> datetime | None:
        if not self.is_completed(path):
            return None
        return aware(path.completed_at) or max(
            (at for step in path.steps if (at := aware(self.courses[step.course_id].completed_at))), default=None
        )

    def status(self, path: AssignedPath, now: datetime) -> str:
        if self.is_completed(path):
            return "completed"
        if path.due_at is not None and aware(path.due_at) < now:
            return "overdue"
        return "in_progress" if any(self._begun(step.course_id) for step in path.steps) else "not_started"

    def progress_pct(self, path: AssignedPath) -> float:
        """Each course weighs the same; within a course, its modules."""
        if not path.steps:
            return 0.0
        return round(sum(self.module_progress(step.course_id)[2] for step in path.steps) / len(path.steps), 1)

    def _with(self, course_id: int) -> list[AssignedPath]:
        return [path for path in self.paths if any(step.course_id == course_id for step in path.steps)]

    def place(self, course_id: int) -> tuple[AssignedPath, int] | None:
        """The first path with this course and its step there (1-based, among the published courses)."""
        path = next(iter(self._with(course_id)), None)
        if path is None:
            return None
        return path, next(i for i, step in enumerate(path.steps, start=1) if step.course_id == course_id)

    def next_course(self, course_id: int) -> PathStep | None:
        """After this course is completed: the next one available in its paths."""
        for path in self._with(course_id):
            for step in path.steps:
                if step.course_id != course_id and self.step_status(step.course_id) in ("available", "in_progress"):
                    return step
        return None

    def completed_path(self, course_id: int) -> AssignedPath | None:
        return next((path for path in self._with(course_id) if self.is_completed(path)), None)


def _locks(journey: Journey) -> dict[int, PathStep]:
    """
    A course is locked when it is in a sequential path where an earlier published course is not completed, and the
    same holds in every other sequential path it is in. Started (or completed) courses are never locked. Locked by:
    the first course not completed of the first path that locks it.
    """
    locked: dict[int, PathStep] = {}
    free: set[int] = set()
    for path in journey.paths:
        if not path.settings.sequential:
            continue
        pending: PathStep | None = None
        for step in path.steps:
            done = journey.completed(step.course_id)
            if pending is None or done or journey.started(step.course_id):
                free.add(step.course_id)
            else:
                locked.setdefault(step.course_id, pending)
            if pending is None and not done:
                pending = step
    return {course_id: by for course_id, by in locked.items() if course_id not in free}


def path_steps(db: Session, path_ids: Iterable[int]) -> dict[int, list[PathStep]]:
    """The published courses of these paths, in order."""
    path_ids = list(path_ids)
    steps: dict[int, list[PathStep]] = {path_id: [] for path_id in path_ids}
    if not path_ids:
        return steps
    rows = db.execute(
        select(LearningPathCourse.path_id, Course.id, Course.title, Course.cover_asset_id)
        .join(Course, Course.id == LearningPathCourse.course_id)
        .where(LearningPathCourse.path_id.in_(path_ids), Course.status == "published")
        .order_by(LearningPathCourse.path_id, LearningPathCourse.order, LearningPathCourse.id)
    )
    for path_id, course_id, title, cover_asset_id in rows:
        steps[path_id].append(PathStep(course_id, title, cover_asset_id))
    return steps


def assigned_path(path: LearningPath, assignment: PathAssignment, steps: list[PathStep]) -> AssignedPath:
    return AssignedPath(
        id=path.id,
        title=path.title,
        description=path.description or "",
        settings=settings_of(path),
        source=assignment.source,
        assigned_at=assignment.assigned_at,
        due_at=assignment.due_at,
        completed_at=assignment.completed_at,
        steps=steps,
    )


def _load_courses(db: Session, journeys: dict[int, Journey], course_ids: set[int], modules: bool) -> None:
    states: dict[int, CourseState] = {}  # by enrollment id
    rows = db.execute(
        select(Enrollment.id, Enrollment.user_id, Enrollment.course_id, Enrollment.status, Enrollment.completed_at)
        .where(Enrollment.user_id.in_(list(journeys)), Enrollment.course_id.in_(course_ids))
    )
    for enrollment_id, user_id, course_id, status, completed_at in rows:
        states[enrollment_id] = journeys[user_id].courses[course_id] = CourseState(status, completed_at)
    if modules:
        counts = dict(db.execute(
            select(Module.course_id, func.count(Module.id))
            .where(Module.course_id.in_(course_ids))
            .group_by(Module.course_id)
        ).all())
        for journey in journeys.values():
            journey.modules = counts
    if not states:
        return
    enrollment_ids = list(states)
    progressed = select(ModuleProgress.enrollment_id).where(
        ModuleProgress.enrollment_id.in_(enrollment_ids),
        or_(ModuleProgress.completed.is_(True), ModuleProgress.attempts > 0, ModuleProgress.last_position_seconds > 0),
    )
    played = select(ActivityAttempt.enrollment_id).where(ActivityAttempt.enrollment_id.in_(enrollment_ids))
    for (enrollment_id,) in db.execute(union(progressed, played)):
        states[enrollment_id].started = True
    if modules:
        done = db.execute(
            select(ModuleProgress.enrollment_id, func.count(ModuleProgress.id))
            .join(Enrollment, Enrollment.id == ModuleProgress.enrollment_id)
            .join(Module, and_(Module.id == ModuleProgress.module_id, Module.course_id == Enrollment.course_id))
            .where(ModuleProgress.enrollment_id.in_(enrollment_ids), ModuleProgress.completed.is_(True))
            .group_by(ModuleProgress.enrollment_id)
        )
        for enrollment_id, count in done:
            states[enrollment_id].completed_modules = int(count or 0)


def journeys(
    db: Session, user_ids: Iterable[int], *, course_ids: Iterable[int] = (), modules: bool = False
) -> dict[int, Journey]:
    """
    Each person's published paths, the state of their courses and the locks: a handful of reads for any number of
    people (one when nobody has a path). `course_ids`: courses whose state is wanted even outside those paths;
    `modules`: also each course's module counts (for progress bars).
    """
    result = {user_id: Journey() for user_id in dict.fromkeys(user_ids)}
    if not result:
        return result
    rows = db.execute(
        select(PathAssignment, LearningPath)
        .join(LearningPath, LearningPath.id == PathAssignment.path_id)
        .where(PathAssignment.user_id.in_(list(result)), LearningPath.status == "published")
        .order_by(PathAssignment.assigned_at, PathAssignment.id)
        # Completion is written in bulk (never into the session): an assignment loaded before must be read again.
        .execution_options(populate_existing=True)
    ).all()
    steps = path_steps(db, {path.id for _, path in rows})
    for assignment, path in rows:
        result[assignment.user_id].paths.append(assigned_path(path, assignment, steps[path.id]))
    wanted = {step.course_id for journey in result.values() for path in journey.paths for step in path.steps}
    wanted.update(course_ids)
    if wanted:
        _load_courses(db, result, wanted, modules)
    for journey in result.values():
        journey.locked_by = _locks(journey)
    return result


def journey(db: Session, user_id: int, *, modules: bool = False) -> Journey:
    return journeys(db, [user_id], modules=modules)[user_id]
