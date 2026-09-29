"""Read models for courses and modules (counts, media URLs, evaluation summaries) without N+1 queries."""

from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.db.models import Course, Enrollment, Evaluation, Module
from app.schemas.courses import (
    CourseDetail,
    CourseSettings,
    CourseSummary,
    EvaluationSummary,
    ModuleAdmin,
)
from app.services import quiz
from app.services.media import MediaResolver
from app.services.metrics import percentage
from app.services.progress import completed_enrollments, evaluations_by_module, ordered_modules

ACTIVE_GENERATION = ("queued", "generating")


@dataclass
class CourseCounts:
    modules: int = 0
    enrolled: int = 0
    completed: int = 0
    generating: int = 0

    @property
    def completion_rate(self) -> float:
        return percentage(self.completed, self.enrolled)


def course_counts(db: Session, course_ids: list[int]) -> dict[int, CourseCounts]:
    counts: dict[int, CourseCounts] = defaultdict(CourseCounts)
    if not course_ids:
        return counts
    module_rows = (
        db.query(
            Module.course_id,
            func.count(Module.id),
            func.sum(case((Module.generation_status.in_(ACTIVE_GENERATION), 1), else_=0)),
        )
        .filter(Module.course_id.in_(course_ids))
        .group_by(Module.course_id)
        .all()
    )
    for course_id, total, generating in module_rows:
        counts[course_id].modules = total
        counts[course_id].generating = int(generating or 0)
    enrollment_rows = (
        db.query(Enrollment.course_id, func.count(Enrollment.id), completed_enrollments())
        .filter(Enrollment.course_id.in_(course_ids))
        .group_by(Enrollment.course_id)
        .all()
    )
    for course_id, total, completed in enrollment_rows:
        counts[course_id].enrolled = total
        counts[course_id].completed = int(completed or 0)
    return counts


def course_summary(course: Course, counts: CourseCounts, cover_url: str | None) -> CourseSummary:
    return CourseSummary(
        id=course.id,
        title=course.title,
        description=course.description or "",
        language=course.language or "es",
        status=course.status,
        source=course.source or "manual",
        module_count=counts.modules,
        enrolled_count=counts.enrolled,
        completed_count=counts.completed,
        completion_rate=counts.completion_rate,
        generating_count=counts.generating,
        cover_url=cover_url,
        created_at=course.created_at,
        updated_at=course.updated_at,
        published_at=course.published_at,
    )


def module_admin(module: Module, media: MediaResolver, evaluation: Evaluation | None) -> ModuleAdmin:
    questions = quiz.evaluation_questions(evaluation)
    storyboard = module.storyboard or {}
    return ModuleAdmin(
        id=module.id,
        course_id=module.course_id,
        title=module.title,
        description=module.description or "",
        order=module.order,
        source=module.source or "text",
        content_text=module.content_text or "",
        generation_status=module.generation_status or "pending",
        generation_error=module.generation_error,
        video=media.video(module),
        poster_url=media.url(module.poster_asset_id),
        captions_url=media.url(module.captions_asset_id),
        document=media.document(module),
        duration_seconds=module.duration_seconds,
        scene_count=len(storyboard.get("scenes", [])),
        video_warning=(storyboard.get("render") or {}).get("warning") if module.source == "ai" else None,
        evaluation=(
            EvaluationSummary(
                question_count=len(questions),
                max_attempts=quiz.max_attempts(evaluation),
                passing_score=quiz.passing_score(evaluation),
            )
            if questions
            else None
        ),
        updated_at=module.updated_at,
    )


def course_detail(db: Session, course: Course) -> CourseDetail:
    modules = ordered_modules(course)
    evaluations = evaluations_by_module(db, modules)
    media = MediaResolver(db).prepare(modules=modules, extra_asset_ids=[course.cover_asset_id])
    counts = course_counts(db, [course.id])[course.id]
    summary = course_summary(course, counts, media.url(course.cover_asset_id))
    return CourseDetail(
        **summary.model_dump(),
        settings=CourseSettings.model_validate(course.settings or {}),
        modules=[module_admin(module, media, evaluations.get(module.id)) for module in modules],
    )
