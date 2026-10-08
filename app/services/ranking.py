"""
Rankings ("los mejores hoy por ruta"): who earned the most points (`points`) in a period, over every course or in one
learning path. One grouped read, whatever the number of people.

- A period starts in the platform's time zone: today at midnight, this week on Monday, this month on the 1st ("all"
  has no start).
- Only active collaborators are ranked (admins and deactivated people never), once they earned points in it.
- A path's ranking is its people's (whoever has it, by profile or by hand): what they earned in its courses, and for
  completing the path.
- Ties share their place (1, 2, 2, 4) and then go by name. Learners see the top 10 and their own place, under their
  full names or as "Ana P." (the parameters say which), or nothing at all when the parameters hide it.
"""

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session

from app.db.models import JobProfile, LearningPath, LearningPathCourse, PathAssignment, PointsEntry, User
from app.schemas.paths import PathRef
from app.schemas.profiles import ProfileRef
from app.schemas.ranking import LearnerRanking, LearnerRankingItem, Ranking, RankingItem, RankingMe, RankingUser
from app.services import settings as platform
from app.services.certificates import utcnow

LEARNER_TOP = 10


def period_start(period: str, zone: str, now: datetime | None = None) -> datetime | None:
    """When the period began, in the platform's time zone (None: since ever)."""
    if period == "all":
        return None
    tz = ZoneInfo(zone)
    day = (now or utcnow()).astimezone(tz).date()
    if period == "week":
        day -= timedelta(days=day.weekday())
    elif period == "month":
        day = day.replace(day=1)
    return datetime.combine(day, time.min, tzinfo=tz)


@dataclass
class Standing:
    rank: int
    user_id: int
    name: str
    profile_id: int | None
    points: int
    completed_courses: int


def standings(db: Session, *, since: datetime | None, path_id: int | None = None) -> list[Standing]:
    """Everyone ranked in the period (and the path), best first."""
    total = func.sum(PointsEntry.points)
    query = (
        select(User.id, User.name, User.profile_id, total, func.sum(case((PointsEntry.source == "course", 1), else_=0)))
        .select_from(PointsEntry)
        .join(User, User.id == PointsEntry.user_id)
        .where(User.is_active.is_(True), User.role == "collaborator")
    )
    if since is not None:
        query = query.where(PointsEntry.earned_at >= since.astimezone(timezone.utc))
    if path_id is not None:
        courses = select(LearningPathCourse.course_id).where(LearningPathCourse.path_id == path_id)
        people = select(PathAssignment.user_id).where(PathAssignment.path_id == path_id)
        query = query.where(
            User.id.in_(people),
            or_(PointsEntry.course_id.in_(courses), and_(PointsEntry.source == "path", PointsEntry.path_id == path_id)),
        )
    rows = db.execute(
        query.group_by(User.id, User.name, User.profile_id)
        .having(total > 0)
        .order_by(total.desc(), func.lower(User.name), User.id)
    ).all()
    ranked, rank, previous = [], 0, None
    for place, (user_id, name, profile_id, points, completed) in enumerate(rows, start=1):
        if points != previous:
            rank, previous = place, points
        ranked.append(Standing(rank, user_id, name, profile_id, int(points), int(completed or 0)))
    return ranked


def _words(name: str) -> list[tuple[str, str]]:
    """The words of a name that have a letter or a digit, each with the first one (punctuation is no initial)."""
    return [(word, letter) for word in name.split() if (letter := next((c for c in word if c.isalnum()), None))]


def short_name(name: str) -> str:
    """'Ana P.': the first name and the initial of the next one (a single name stays as it is)."""
    words = _words(name)
    if len(words) < 2:
        return name.strip()
    return f"{words[0][0]} {words[1][1].upper()}."


def initials(name: str) -> str:
    return "".join(letter for _, letter in _words(name)[:2]).upper() or "?"


def _path_ref(path: LearningPath | None) -> PathRef | None:
    return PathRef(id=path.id, title=path.title) if path else None


def admin_ranking(db: Session, period: str, path: LearningPath | None, limit: int) -> Ranking:
    since = period_start(period, platform.get_settings(db).general.timezone)
    ranked = standings(db, since=since, path_id=path.id if path else None)
    top = ranked[:limit]
    profile_ids = {row.profile_id for row in top if row.profile_id}
    profiles = {
        profile.id: ProfileRef(id=profile.id, name=profile.name)
        for profile in db.query(JobProfile).filter(JobProfile.id.in_(profile_ids))
    } if profile_ids else {}
    return Ranking(
        period=period,
        starts_at=since,
        path=_path_ref(path),
        items=[
            RankingItem(
                rank=row.rank,
                user=RankingUser(id=row.user_id, name=row.name, profile=profiles.get(row.profile_id)),
                points=row.points,
                completed_courses=row.completed_courses,
            )
            for row in top
        ],
        total_people=len(ranked),
    )


def learner_ranking(db: Session, user: User, period: str, path: LearningPath | None) -> LearnerRanking:
    """For `user`, over every course or in one of their paths (the caller checked it is)."""
    settings = platform.get_settings(db)
    since = period_start(period, settings.general.timezone)
    if not settings.ranking.visible_to_learners:
        return LearnerRanking(visible=False, period=period, starts_at=since, path=_path_ref(path), items=[],
                              total_people=0)
    ranked = standings(db, since=since, path_id=path.id if path else None)
    full_names = settings.ranking.show_full_names
    mine = next((row for row in ranked if row.user_id == user.id), None)
    return LearnerRanking(
        visible=True,
        period=period,
        starts_at=since,
        path=_path_ref(path),
        items=[
            LearnerRankingItem(
                rank=row.rank,
                name=row.name.strip() if full_names else short_name(row.name),
                initials=initials(row.name),
                points=row.points,
                is_me=row.user_id == user.id,
            )
            for row in ranked[:LEARNER_TOP]
        ],
        me=RankingMe(rank=mine.rank, points=mine.points) if mine else None,
        total_people=len(ranked),
    )
