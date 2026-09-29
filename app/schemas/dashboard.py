from typing import Literal

from pydantic import BaseModel

from app.schemas.common import UtcDatetime


class ActivityItem(BaseModel):
    kind: Literal["passed_quiz", "failed_quiz", "completed_course"]
    user_name: str
    course_id: int
    course_title: str
    module_title: str | None = None
    score: float | None = None
    at: UtcDatetime | None = None


class TopCourse(BaseModel):
    id: int
    title: str
    enrolled_count: int
    completion_rate: float


class Dashboard(BaseModel):
    courses: dict[str, int]
    learners: dict[str, int]
    enrollments: dict[str, int]
    completion_rate: float
    average_score: float | None = None
    recent_activity: list[ActivityItem]
    top_courses: list[TopCourse]
