from typing import Literal

from pydantic import BaseModel

from app.schemas.common import UtcDatetime


class ActivityItem(BaseModel):
    kind: Literal["passed_quiz", "failed_quiz", "completed_course", "certificate_issued", "path_completed"]
    user_id: int
    user_name: str
    # A course's (a certificate's title for one issued). Null for a path's, or once the certificate's course is gone.
    course_id: int | None = None
    course_title: str = ""
    module_title: str | None = None
    path_id: int | None = None  # a path completed, or a path's certificate (null once that path is gone)
    path_title: str | None = None
    certificate_code: str | None = None
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
