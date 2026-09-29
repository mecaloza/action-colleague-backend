"""What a learner sees of a course: modules with lock state, media only when unlocked, quiz summaries."""

from sqlalchemy.orm import Session

from app.db.models import Course, Enrollment
from app.schemas.learn import (
    LearnerCourse,
    LearnerCourseDetail,
    LearnerCourseInfo,
    LearnerEnrollment,
    LearnerModule,
    QuizSummary,
)
from app.services import quiz
from app.services.media import MediaResolver
from app.services.metrics import percentage
from app.services.progress import ModuleState


def learner_course(enrollment: Enrollment, states: list[ModuleState], cover_url: str | None = None) -> LearnerCourse:
    """A row of a person's course list: overall progress and the module that comes next."""
    course = enrollment.course
    completed = sum(1 for state in states if state.completed)
    next_state = next((state for state in states if not state.completed), None)
    return LearnerCourse(
        course_id=enrollment.course_id,
        title=course.title,
        description=course.description or "",
        cover_url=cover_url,
        status=enrollment.status,
        progress_pct=percentage(completed, len(states)),
        total_modules=len(states),
        completed_modules=completed,
        next_module_id=next_state.module.id if next_state else None,
        next_module_title=next_state.module.title if next_state else None,
        total_duration_seconds=sum(state.module.duration_seconds or 0 for state in states),
        enrolled_at=enrollment.enrolled_at,
        completed_at=enrollment.completed_at,
    )


def quiz_summary(state: ModuleState) -> QuizSummary | None:
    questions = quiz.evaluation_questions(state.evaluation)
    if not questions:
        return None
    return QuizSummary(
        question_count=len(questions),
        max_attempts=quiz.max_attempts(state.evaluation),
        attempts_used=state.attempts_used,
        passed=state.passed,
        best_score=state.progress.score if state.progress else None,
        passing_score=quiz.passing_score(state.evaluation),
    )


def _learner_module(state: ModuleState, media: MediaResolver) -> LearnerModule:
    module, unlocked = state.module, state.unlocked
    return LearnerModule(
        id=module.id,
        title=module.title,
        description=module.description or "",
        order=module.order,
        source=module.source or "text",
        unlocked=unlocked,
        completed=state.completed,
        duration_seconds=module.duration_seconds,
        # Locked modules show their poster, but their content never leaves the server.
        video=media.video(module) if unlocked else None,
        poster_url=media.url(module.poster_asset_id),
        captions_url=media.url(module.captions_asset_id) if unlocked else None,
        document=media.document(module) if unlocked else None,
        content_text=(module.content_text or "") if unlocked else None,
        last_position_seconds=(state.progress.last_position_seconds or 0) if state.progress else 0,
        quiz=quiz_summary(state),
    )


def _learner_enrollment(enrollment: Enrollment) -> LearnerEnrollment:
    return LearnerEnrollment(
        id=enrollment.id,
        status=enrollment.status,
        progress_pct=enrollment.progress_pct or 0.0,
        completed_at=enrollment.completed_at,
    )


def course_player_detail(
    db: Session, course: Course, states: list[ModuleState], enrollment: Enrollment | None
) -> LearnerCourseDetail:
    media = MediaResolver(db).prepare(modules=[state.module for state in states])
    return LearnerCourseDetail(
        course=LearnerCourseInfo(
            id=course.id, title=course.title, description=course.description or "", language=course.language or "es"
        ),
        enrollment=_learner_enrollment(enrollment) if enrollment else None,
        modules=[_learner_module(state, media) for state in states],
    )
