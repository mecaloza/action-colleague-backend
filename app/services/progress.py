"""
Learner progress: which modules are unlocked or completed, and the enrollment's overall status.

Modules unlock sequentially: a module is available once every previous module is completed.
A module with an evaluation is completed by passing it; a module without one is completed
explicitly by the learner (after watching or reading it). This is the only place that decides.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import case, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, contains_eager, selectinload

from app.db.models import Course, Enrollment, Evaluation, Module, ModuleProgress
from app.services.metrics import percentage


@dataclass
class ModuleState:
    module: Module
    progress: ModuleProgress | None
    evaluation: Evaluation | None
    unlocked: bool

    @property
    def completed(self) -> bool:
        return bool(self.progress and self.progress.completed)

    @property
    def passed(self) -> bool:
        return bool(self.progress and self.progress.passed)

    @property
    def attempts_used(self) -> int:
        return (self.progress.attempts or 0) if self.progress else 0


def completed_enrollments():
    """SQL aggregate for `GROUP BY` queries: how many of the grouped enrollments are completed."""
    return func.sum(case((Enrollment.status == "completed", 1), else_=0))


def user_enrollments(db: Session, user_id: int, *, published_only: bool = False) -> list[Enrollment]:
    """A person's enrollments, newest first. Learners only see published courses; admins see them all."""
    query = (
        db.query(Enrollment)
        .join(Course, Course.id == Enrollment.course_id)
        .options(contains_eager(Enrollment.course).selectinload(Course.modules), selectinload(Enrollment.module_progress))
        .filter(Enrollment.user_id == user_id)
    )
    if published_only:
        query = query.filter(Course.status == "published")
    return query.order_by(Enrollment.enrolled_at.desc(), Enrollment.id.desc()).all()


def ordered_modules(course: Course) -> list[Module]:
    return sorted(course.modules, key=lambda module: (module.order, module.id))


def evaluations_by_module(db: Session, modules: list[Module]) -> dict[int, Evaluation]:
    if not modules:
        return {}
    evaluations = db.query(Evaluation).filter(Evaluation.module_id.in_([m.id for m in modules])).all()
    return {evaluation.module_id: evaluation for evaluation in evaluations}


def module_states(
    db: Session, enrollment: Enrollment, evaluations: dict[int, Evaluation] | None = None
) -> list[ModuleState]:
    """`evaluations` (by module id) can be preloaded for many enrollments at once."""
    modules = ordered_modules(enrollment.course)
    records = {record.module_id: record for record in enrollment.module_progress}
    if evaluations is None:
        evaluations = evaluations_by_module(db, modules)
    states: list[ModuleState] = []
    unlocked = True
    for module in modules:
        record = records.get(module.id)
        # What the learner already completed stays open, even if a reorder put it after a pending module.
        state = ModuleState(module, record, evaluations.get(module.id), unlocked or bool(record and record.completed))
        states.append(state)
        unlocked = unlocked and state.completed
    return states


def preview_states(db: Session, course: Course) -> list[ModuleState]:
    """Every module unlocked and no progress: the course as an admin previews it."""
    modules = ordered_modules(course)
    evaluations = evaluations_by_module(db, modules)
    return [
        ModuleState(module=module, progress=None, evaluation=evaluations.get(module.id), unlocked=True)
        for module in modules
    ]


def get_or_create_progress(db: Session, enrollment: Enrollment, module: Module, lock: bool = False) -> ModuleProgress:
    """The learner's progress row for a module. `lock` serializes concurrent quiz submissions."""
    query = db.query(ModuleProgress).filter(
        ModuleProgress.enrollment_id == enrollment.id, ModuleProgress.module_id == module.id
    )
    if lock:
        # populate_existing: the row may already be in the session (loaded with the enrollment)
        # with values read before the lock; the locked read must replace them.
        query = query.with_for_update().populate_existing()
    record = query.first()
    if record is not None:
        return record
    try:
        with db.begin_nested():
            record = ModuleProgress(
                enrollment_id=enrollment.id, module_id=module.id, completed=False, passed=False, attempts=0
            )
            db.add(record)
    except IntegrityError:  # created by a concurrent request (unique enrollment + module)
        record = query.one()
    return record


def mark_completed(record: ModuleProgress) -> None:
    if not record.completed:
        record.completed = True
        record.completed_at = datetime.now(timezone.utc)


def _apply_progress(enrollment: Enrollment, done: int, total: int, started: bool) -> None:
    enrollment.progress_pct = percentage(done, total)
    if total and done == total:
        enrollment.status = "completed"
        enrollment.completed_at = enrollment.completed_at or datetime.now(timezone.utc)
    else:
        # Any progress record counts as started (a saved position or a quiz attempt, not only a completion).
        enrollment.status = "in_progress" if started else "assigned"
        enrollment.completed_at = None


def refresh_enrollment(db: Session, enrollment: Enrollment) -> None:
    """
    Recompute progress percentage, status and completion date from module progress.

    The enrollment row is locked and the module list re-read after the lock: an admin adding or
    removing modules recomputes the same row (see below), and the last writer must see both changes.
    """
    db.flush()
    db.refresh(enrollment, with_for_update=True)
    db.expire(enrollment.course, ["modules"])
    states = module_states(db, enrollment)
    done = sum(1 for state in states if state.completed)
    _apply_progress(enrollment, done, len(states), any(state.progress for state in states))


def refresh_course_enrollments(db: Session, course: Course) -> None:
    """After modules are added or removed, every learner's percentage and status must follow (two reads in total)."""
    db.flush()
    # Locked in id order (two admins can't deadlock); learners lock their progress row first, then this.
    enrollments = (
        db.query(Enrollment)
        .filter(Enrollment.course_id == course.id)
        .order_by(Enrollment.id)
        .with_for_update()
        .populate_existing()
        .all()
    )
    # Read after the locks: another admin's module change committed while we waited counts too.
    module_ids = [module_id for (module_id,) in db.query(Module.id).filter(Module.course_id == course.id)]
    stats: dict[int, tuple[int, bool]] = {}
    if module_ids and enrollments:
        rows = (
            db.query(
                ModuleProgress.enrollment_id,
                func.sum(case((ModuleProgress.completed.is_(True), 1), else_=0)),
            )
            .join(Enrollment, Enrollment.id == ModuleProgress.enrollment_id)
            .filter(Enrollment.course_id == course.id, ModuleProgress.module_id.in_(module_ids))
            .group_by(ModuleProgress.enrollment_id)
        )
        stats = {enrollment_id: (int(done or 0), True) for enrollment_id, done in rows}
    for enrollment in enrollments:
        done, started = stats.get(enrollment.id, (0, False))
        _apply_progress(enrollment, done, len(module_ids), started)
