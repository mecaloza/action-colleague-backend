from typing import Literal

from pydantic import BaseModel

from app.schemas.common import UtcDatetime
from app.schemas.paths import PathRef
from app.schemas.profiles import ProfileRef

# today | this week (from Monday) | this month | since ever, in the platform's time zone.
Period = Literal["today", "week", "month", "all"]


class RankingUser(BaseModel):
    id: int
    name: str
    profile: ProfileRef | None = None


class RankingItem(BaseModel):
    rank: int  # ties share it (1, 2, 2, 4)
    user: RankingUser
    points: int
    completed_courses: int  # in the period (and the path, when there is one)


class Ranking(BaseModel):
    """The admin's: who earned the most points in the period, over every course or in a path."""

    period: Period
    starts_at: UtcDatetime | None = None  # null for "all"
    path: PathRef | None = None
    items: list[RankingItem]
    total_people: int  # ranked: active collaborators who earned points in it


class LearnerRankingItem(BaseModel):
    rank: int
    name: str  # full, or "Ana P." when the parameters hide full names
    initials: str
    points: int
    is_me: bool


class RankingMe(BaseModel):
    rank: int
    points: int


class LearnerRanking(BaseModel):
    """The learner's: the top 10 and their own place (null until they earn points in the period). Hidden by the
    parameters: `visible` false and nothing else."""

    visible: bool
    period: Period
    starts_at: UtcDatetime | None = None
    path: PathRef | None = None
    items: list[LearnerRankingItem]
    me: RankingMe | None = None
    total_people: int
