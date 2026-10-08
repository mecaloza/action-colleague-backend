"""
A person's overview for admins ("ficha"): who they are, their numbers, their paths, their courses module by module
(quiz and activities), their certificates and what they did lately. A fixed number of reads, whatever their number of
courses, modules, plays or attempts.
"""

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (
    Activity,
    ActivityAttempt,
    Certificate,
    Course,
    Enrollment,
    EvaluationAttempt,
    JobProfile,
    Module,
    PointsEntry,
    RefreshToken,
    User,
)
from app.schemas.people import (
    PersonActivity,
    PersonCourse,
    PersonKpis,
    PersonModule,
    PersonOverview,
    PersonPath,
    PersonUser,
    RecentEvent,
)
from app.schemas.profiles import ProfileRef
from app.services import certificates, paths, points, progress
from app.services.learner_views import max_score, quiz_summary
from app.services.media import MediaResolver
from app.services.metrics import percentage
from app.services.path_views import learner_path
from app.services.points import aware

RECENT_LIMIT = 25


def _person(db: Session, user: User) -> PersonUser:
    profile = db.get(JobProfile, user.profile_id) if user.profile_id else None
    last_seen = db.execute(select(func.max(RefreshToken.created_at)).where(RefreshToken.user_id == user.id)).scalar()
    return PersonUser(
        id=user.id,
        name=user.name,
        email=user.email,
        role=user.role,
        position=user.position or "",
        department=user.department or "",
        profile=ProfileRef(id=profile.id, name=profile.name) if profile else None,
        is_active=bool(user.is_active),
        created_at=user.created_at,
        last_seen_at=last_seen,
    )


class _Plays:
    """The person's finished plays (never an admin's preview), by enrollment and activity: one read."""

    def __init__(self, db: Session, enrollment_ids: list[int]):
        self.best: dict[int, dict[int, int]] = {enrollment_id: {} for enrollment_id in enrollment_ids}
        self.stats: dict[int, tuple[int, datetime | None]] = {}  # activity id -> (plays, last one)
        if not enrollment_ids:
            return
        rows = db.execute(
            select(ActivityAttempt.enrollment_id, ActivityAttempt.activity_id, func.max(ActivityAttempt.score),
                   func.count(ActivityAttempt.id), func.max(ActivityAttempt.finished_at))
            .where(ActivityAttempt.enrollment_id.in_(enrollment_ids), ActivityAttempt.finished_at.isnot(None))
            .group_by(ActivityAttempt.enrollment_id, ActivityAttempt.activity_id)
        )
        for enrollment_id, activity_id, best, count, last in rows:
            self.best[enrollment_id][activity_id] = int(best or 0)
            self.stats[activity_id] = (int(count), last)


def _module(state: progress.ModuleState, plays: _Plays) -> PersonModule:
    activities = []
    for activity in state.activities:
        count, last = plays.stats.get(activity.id, (0, None))
        activities.append(PersonActivity(
            activity_id=activity.id, title=activity.title, type=activity.type,
            best_score=state.best_scores.get(activity.id), max_score=max_score(activity), plays=count,
            last_played_at=last,
        ))
    return PersonModule(
        module_id=state.module.id,
        title=state.module.title,
        order=state.module.order,
        completed=state.completed,
        completed_at=state.progress.completed_at if state.completed else None,
        quiz=quiz_summary(state),
        activities=activities,
    )


def _recent(db: Session, user: User, enrollments: list[Enrollment], certificate_rows: list[Certificate],
            journey: paths.Journey) -> list[RecentEvent]:
    """The latest of each kind (two reads; the rest is already loaded), newest first."""
    events: list[RecentEvent] = []
    quizzes = db.execute(
        select(EvaluationAttempt.passed, EvaluationAttempt.score, EvaluationAttempt.created_at, Module.title,
               Course.title)
        .join(Module, Module.id == EvaluationAttempt.module_id)
        .join(Course, Course.id == Module.course_id)
        .where(EvaluationAttempt.user_id == user.id, EvaluationAttempt.created_at.isnot(None))
        .order_by(EvaluationAttempt.created_at.desc(), EvaluationAttempt.id.desc())
        .limit(RECENT_LIMIT)
    )
    for passed, score, at, module_title, course_title in quizzes:
        events.append(RecentEvent(kind="quiz_passed" if passed else "quiz_failed", at=aware(at), title=module_title,
                                  detail=course_title, score=score))
    plays = db.execute(
        select(ActivityAttempt.score, ActivityAttempt.max_score, ActivityAttempt.finished_at, Activity.title,
               Course.title)
        .join(Activity, Activity.id == ActivityAttempt.activity_id)
        .join(Module, Module.id == Activity.module_id)
        .join(Course, Course.id == Module.course_id)
        .where(ActivityAttempt.user_id == user.id, ActivityAttempt.enrollment_id.isnot(None),
               ActivityAttempt.finished_at.isnot(None))
        .order_by(ActivityAttempt.finished_at.desc(), ActivityAttempt.id.desc())
        .limit(RECENT_LIMIT)
    )
    for score, most, at, activity_title, course_title in plays:
        events.append(RecentEvent(kind="activity_finished", at=aware(at), title=activity_title, detail=course_title,
                                  score=points.percent(score, most) if most else None))
    for enrollment in enrollments:
        course = enrollment.course
        titles = {module.id: module.title for module in course.modules}
        events += [
            RecentEvent(kind="module_completed", at=aware(record.completed_at), title=titles[record.module_id],
                        detail=course.title)
            for record in enrollment.module_progress
            if record.completed and record.completed_at and record.module_id in titles
        ]
        if enrollment.status == "completed" and enrollment.completed_at:
            events.append(RecentEvent(kind="course_completed", at=aware(enrollment.completed_at), title=course.title))
    events += [
        RecentEvent(kind="certificate_issued", at=aware(certificate.issued_at), title=certificate.title,
                    detail=certificate.code, score=certificate.score)
        for certificate in certificate_rows
    ]
    events += [
        RecentEvent(kind="path_completed", at=aware(completed_at), title=path.title)
        for path in journey.paths
        if (completed_at := journey.completed_at(path))
    ]
    events.sort(key=lambda event: event.at, reverse=True)
    return events[:RECENT_LIMIT]


def overview(db: Session, user: User) -> PersonOverview:
    now = certificates.utcnow()
    enrollments = progress.user_enrollments(db, user.id)  # drafts included: the admin sees everything
    modules = [module for enrollment in enrollments for module in enrollment.course.modules]
    evaluations = progress.evaluations_by_module(db, modules)
    activities = progress.ready_activities(db, modules)
    plays = _Plays(db, [enrollment.id for enrollment in enrollments])
    journey = paths.journey(db, user.id, modules=True)
    certificate_rows = (
        db.query(Certificate)
        .filter(Certificate.user_id == user.id)
        .order_by(Certificate.issued_at.desc(), Certificate.id.desc())
        .all()
    )
    in_force = [certificate for certificate in certificate_rows if certificate.revoked_at is None]
    course_refs = {c.course_id: certificates.ref(c) for c in in_force if c.kind == "course" and c.course_id}
    path_refs = {c.path_id: certificates.ref(c) for c in in_force if c.kind == "path" and c.path_id}

    courses, scores = [], []
    for enrollment in enrollments:
        states = progress.module_states(
            db, enrollment, evaluations, course_unlocked=enrollment.course_id not in journey.locked_by,
            activities=activities, best_scores=plays.best[enrollment.id],
        )
        scores += [state.progress.score for state in states if state.progress and state.progress.score is not None]
        courses.append(PersonCourse(
            course_id=enrollment.course_id,
            title=enrollment.course.title,
            status=enrollment.status,
            course_status=enrollment.course.status,
            progress_pct=percentage(sum(1 for state in states if state.completed), len(states)),
            enrolled_at=enrollment.enrolled_at,
            completed_at=enrollment.completed_at,
            locked=enrollment.course_id in journey.locked_by,
            certificate=course_refs.get(enrollment.course_id),
            modules=[_module(state, plays) for state in states],
        ))

    media = MediaResolver(db).prepare(
        extra_asset_ids=[step.cover_asset_id for path in journey.paths for step in path.steps]
    )
    earned = db.execute(select(func.coalesce(func.sum(PointsEntry.points), 0)).where(PointsEntry.user_id == user.id))
    return PersonOverview(
        user=_person(db, user),
        kpis=PersonKpis(
            courses_assigned=len(enrollments),
            courses_completed=sum(1 for enrollment in enrollments if enrollment.status == "completed"),
            average_score=round(sum(scores) / len(scores), 1) if scores else None,
            points=int(earned.scalar() or 0),
            certificates=sum(1 for c in certificate_rows if certificates.status_of(c, now) == "valid"),
            activities_played=len(plays.stats),
        ),
        paths=[
            PersonPath(**learner_path(path, journey, media, now, path_refs.get(path.id)).model_dump(),
                       source=path.source)
            for path in journey.paths
        ],
        courses=courses,
        certificates=[certificates.admin_view(certificate, now) for certificate in certificate_rows],
        recent=_recent(db, user, enrollments, certificate_rows, journey),
    )
