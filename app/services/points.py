"""
Points ("puntos"): what learners earn for learning, kept as a ledger (`learner_points`) that rankings add up by period.

- A play or a quiz earns its improvement only: the learner's best (a play's percentage of its maximum, a quiz's score,
  both 0-100) minus what it already earned. Replaying never farms points, beating one's best earns the difference.
  Admins' previews never count (they have no enrollment).
- Completing a module, a course or a path earns the parameters' points once per person (completed again after it
  reopened, nothing more), dated when it was completed.
- Written by the moments that count them, inside their locks: a play finished (`learn`), a quiz submitted, and the
  completion transitions of `progress` and `paths`, next to the certificates: one statement whatever the number of
  people completing at once.
- What was earned before points existed is derived by the maintenance pass (`backfill`), dated when it happened.
"""

import logging
from collections.abc import Iterable
from math import floor

from sqlalchemy import DateTime, Integer, String, cast, exists, func, insert, literal, null, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import Session

from app.db.models import ActivityAttempt, Enrollment, EvaluationAttempt, ModuleProgress, PathAssignment, PointsEntry
from app.services import settings as platform
from app.services.certificates import aware, utcnow

logger = logging.getLogger(__name__)

_COLUMNS = ["user_id", "course_id", "path_id", "source", "source_key", "points", "earned_at"]
_BATCH = 500  # rows per insert: far below the database's limit of parameters per statement


def percent(score: int, max_score: int) -> int:
    """A play's score as a whole percentage of its maximum, half up (in integers: exact), 0-100."""
    return min(max((200 * score + max_score) // (2 * max_score), 0), 100) if max_score > 0 else 0


def whole(score: float) -> int:
    """A quiz score (0-100, one decimal) in whole points, half up."""
    return min(max(floor(score + 0.5), 0), 100)


def key(source: str, target_id: int) -> str:
    return f"{source}:{target_id}"


def _key_sql(source: str, column):
    """`key` in SQL, for set-based writes."""
    return literal(f"{source}:", String) + cast(column, String)


def _earned(source: str, user_column, key_expression):
    """Correlated: the person already has points for this (the source lets a milestone use its unique index)."""
    return exists().where(
        PointsEntry.user_id == user_column, PointsEntry.source == source, PointsEntry.source_key == key_expression
    )


def _now():
    return literal(utcnow(), DateTime(timezone=True))


def _insert(db: Session):
    return postgresql.insert if db.get_bind().dialect.name == "postgresql" else sqlite.insert


def _write_once(db: Session, rows) -> int:
    """Milestones, from a SELECT of `_COLUMNS`: one written meanwhile by another request is skipped."""
    db.flush()  # the completions this request made are read below
    result = db.execute(_insert(db)(PointsEntry).from_select(_COLUMNS, rows).on_conflict_do_nothing())
    return max(result.rowcount or 0, 0)


# ── Improvements: plays and quizzes ───────────────────────────────────


def _earned_so_far(user_id: int, entry_key: str):
    return (
        select(func.coalesce(func.sum(PointsEntry.points), 0))
        .where(PointsEntry.user_id == user_id, PointsEntry.source_key == entry_key)
        .scalar_subquery()
    )


def _improve(db: Session, enrollment: Enrollment, source: str, entry_key: str, best: int, earned: int) -> int:
    gained = best - int(earned or 0)
    if gained <= 0:
        return 0
    db.execute(insert(PointsEntry).values(
        user_id=enrollment.user_id, course_id=enrollment.course_id, source=source, source_key=entry_key,
        points=gained, earned_at=utcnow(),
    ))
    return gained


def award_play(db: Session, enrollment: Enrollment, activity_id: int) -> int:
    """A play of this activity just finished (by its last move, `finish`, or a stale play closed on start): what the
    learner's best percentage in it improved. Called holding the lock that counts plays of its module. Returns the
    points earned now."""
    entry_key = key("activity", activity_id)
    # Each play's `percent`, in SQL (its maximum depends on the play: a quick challenge without its clock has less).
    best = (
        select(func.max((200 * ActivityAttempt.score + ActivityAttempt.max_score) // (2 * ActivityAttempt.max_score)))
        .where(
            ActivityAttempt.enrollment_id == enrollment.id,
            ActivityAttempt.activity_id == activity_id,
            ActivityAttempt.finished_at.isnot(None),
            ActivityAttempt.max_score > 0,
        )
        .scalar_subquery()
    )
    best_percent, earned = db.execute(select(best, _earned_so_far(enrollment.user_id, entry_key))).one()
    return _improve(db, enrollment, "activity", entry_key, min(max(int(best_percent or 0), 0), 100), earned)


def award_quiz(db: Session, enrollment: Enrollment, module_id: int, score: float) -> int:
    """A quiz submission (holding its progress row): what it improved on the learner's best score in the module's quiz
    (a new quiz in its place included: its points go on from what the old one earned)."""
    entry_key = key("quiz", module_id)
    earned = db.execute(select(_earned_so_far(enrollment.user_id, entry_key))).scalar()
    return _improve(db, enrollment, "quiz", entry_key, whole(score), earned)


# ── Milestones: modules, courses and paths completed ──────────────────


def award_modules(db: Session, *conditions) -> int:
    """The completed modules (`module_progress` rows meeting `conditions`) that earned nothing yet: the parameters'
    points, dated when each one was completed. One statement."""
    points = platform.get_settings(db).ranking.points_module
    entry_key = _key_sql("module", ModuleProgress.module_id)
    completed_at = func.coalesce(ModuleProgress.completed_at, Enrollment.completed_at, Enrollment.enrolled_at, _now())
    rows = (
        select(Enrollment.user_id, Enrollment.course_id, null(), literal("module"), entry_key, literal(points, Integer),
               completed_at)
        .select_from(ModuleProgress)
        .join(Enrollment, Enrollment.id == ModuleProgress.enrollment_id)
        .where(ModuleProgress.completed.is_(True), *conditions, ~_earned("module", Enrollment.user_id, entry_key))
    )
    return _write_once(db, rows)


def award_courses(db: Session, *conditions) -> int:
    """The completed enrollments meeting `conditions` whose course earned nothing yet (a reopened one completed again
    earns nothing more). One statement."""
    points = platform.get_settings(db).ranking.points_course
    entry_key = _key_sql("course", Enrollment.course_id)
    rows = (
        select(Enrollment.user_id, Enrollment.course_id, null(), literal("course"), entry_key, literal(points, Integer),
               func.coalesce(Enrollment.completed_at, Enrollment.enrolled_at, _now()))
        .where(Enrollment.status == "completed", *conditions, ~_earned("course", Enrollment.user_id, entry_key))
    )
    return _write_once(db, rows)


def award_paths(db: Session, *conditions) -> int:
    """The completed path assignments meeting `conditions` whose path earned nothing yet. One statement."""
    points = platform.get_settings(db).ranking.points_path
    entry_key = _key_sql("path", PathAssignment.path_id)
    rows = (
        select(PathAssignment.user_id, null(), PathAssignment.path_id, literal("path"), entry_key,
               literal(points, Integer), PathAssignment.completed_at)
        .where(PathAssignment.completed_at.isnot(None), *conditions,
               ~_earned("path", PathAssignment.user_id, entry_key))
    )
    return _write_once(db, rows)


# ── The past ──────────────────────────────────────────────────────────


def _past_improvements(rows: Iterable, source: str) -> list[dict]:
    """Rows of (user, course, target id, value 0-100, when), in order of time per person and target: each time one
    beat the person's best, what it added, then."""
    best: dict[tuple[int, int], int] = {}
    entries = []
    now = utcnow()
    for user_id, course_id, target_id, value, at in rows:
        gained = value - best.get((user_id, target_id), 0)
        if gained > 0:
            best[(user_id, target_id)] = value
            entries.append({"user_id": user_id, "course_id": course_id, "path_id": None, "source": source,
                            "source_key": key(source, target_id), "points": gained, "earned_at": aware(at) or now})
    return entries


def _past_plays(db: Session) -> list[dict]:
    """Finished plays (never an admin's preview) of activities that earned nothing yet."""
    entry_key = _key_sql("activity", ActivityAttempt.activity_id)
    rows = db.execute(
        select(Enrollment.user_id, Enrollment.course_id, ActivityAttempt.activity_id, ActivityAttempt.score,
               ActivityAttempt.max_score, ActivityAttempt.finished_at)
        .join(Enrollment, Enrollment.id == ActivityAttempt.enrollment_id)
        .where(ActivityAttempt.finished_at.isnot(None), ActivityAttempt.max_score > 0,
               ~_earned("activity", Enrollment.user_id, entry_key))
        .order_by(Enrollment.user_id, ActivityAttempt.activity_id, ActivityAttempt.finished_at, ActivityAttempt.id)
    )
    return _past_improvements(
        ((user_id, course_id, activity_id, percent(score, max_score), at)
         for user_id, course_id, activity_id, score, max_score, at in rows),
        "activity",
    )


def _past_quizzes(db: Session) -> list[dict]:
    """Quiz attempts of modules whose quiz earned nothing yet."""
    entry_key = _key_sql("quiz", EvaluationAttempt.module_id)
    rows = db.execute(
        select(Enrollment.user_id, Enrollment.course_id, EvaluationAttempt.module_id, EvaluationAttempt.score,
               func.coalesce(EvaluationAttempt.created_at, Enrollment.enrolled_at))
        .join(Enrollment, Enrollment.id == EvaluationAttempt.enrollment_id)
        .where(EvaluationAttempt.score.isnot(None), ~_earned("quiz", Enrollment.user_id, entry_key))
        .order_by(Enrollment.user_id, EvaluationAttempt.module_id, EvaluationAttempt.created_at, EvaluationAttempt.id)
    )
    return _past_improvements(
        ((user_id, course_id, module_id, whole(score), at) for user_id, course_id, module_id, score, at in rows), "quiz"
    )


def backfill(db: Session) -> int:
    """
    The points of what learners did before points existed, by the same rules and dated when they did it (the
    maintenance pass of every deploy). Idempotent per person and what earned them: a milestone already earned is
    skipped, and so is an activity or a quiz that earned anything already (its improvements were counted as they
    happened, the past ones included). So a learner who earned points between a deploy and this pass still gets the
    rest of their past.
    """
    written = award_modules(db) + award_courses(db) + award_paths(db)
    entries = _past_plays(db) + _past_quizzes(db)
    for start in range(0, len(entries), _BATCH):
        db.execute(insert(PointsEntry), entries[start:start + _BATCH])
    written += len(entries)
    db.commit()
    if written:
        logger.info("points_backfilled", extra={"count": written,
                                                 "user_ids": sorted({e["user_id"] for e in entries})[:50]})
    return written
