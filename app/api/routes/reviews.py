"""
The automatic review of the modules' AI videos ("revisión de calidad"): the newest review of a module's video, asking
for it again, and what the admin does with each finding (dismiss it or open it again; a change to the script made for
one marks it fixed: see the revisions).
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_admin
from app.api.lookups import module_or_404
from app.api.routes.studio import _require_ai
from app.db.models import User, VideoReview
from app.schemas.reviews import VideoReview as VideoReviewOut
from app.schemas.reviews import VideoReviewQueued
from app.services import video_reviews
from app.services.media_views import job_out

router = APIRouter(tags=["reviews"], dependencies=[Depends(require_admin)])
logger = logging.getLogger(__name__)


@router.get("/modules/{module_id}/review", response_model=VideoReviewOut)
def get_review(module_id: int, db: Session = Depends(get_db)):
    """The newest review of the module's video: its frames, what was found and what was done with each finding."""
    review = video_reviews.current(db, module_or_404(db, module_id))
    if review is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Este video aún no tiene revisión")
    return video_reviews.review_out(review)


@router.post("/modules/{module_id}/review", response_model=VideoReviewQueued, status_code=status.HTTP_202_ACCEPTED)
def request_review(module_id: int, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    """Review the module's video (again): with new frames, taken from the published video. While one is waiting or
    running for this video, that one."""
    _require_ai()
    module = module_or_404(db, module_id)
    if module.source != "ai":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"«{module.title}» tiene su propio video; la revisión automática es para videos producidos con IA",
        )
    # Held (NO KEY UPDATE: learners' progress still references it): two asks at once, one waits and gets the other's.
    db.refresh(module, with_for_update={"key_share": True})
    if not module.video_asset_id:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"«{module.title}» aún no tiene video")
    review, job = video_reviews.request(db, module, created_by=admin.id)
    db.commit()
    logger.info("video_review_requested", extra={"module_id": module.id, "review_id": review.id, "job_id": job.id})
    return VideoReviewQueued(review=video_reviews.review_out(review), job=job_out(job))


def _review_or_404(db: Session, review_id: int) -> VideoReview:
    review = db.get(VideoReview, review_id, with_for_update=True)  # one change to its findings at a time
    if review is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Revisión no encontrada")
    return review


def _set_issue(db: Session, review_id: int, issue_id: str, wanted: str, admin: User) -> VideoReviewOut:
    review = _review_or_404(db, review_id)
    if not video_reviews.set_issue_status(review, issue_id, wanted):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Hallazgo no encontrado")
    db.commit()
    # Who did it: ignoring a critical finding is what lets a course be published with `qa.block_publish`.
    logger.info("review_issue_set", extra={"review_id": review.id, "module_id": review.module_id, "issue_id": issue_id,
                                           "status": wanted, "admin_id": admin.id})
    return video_reviews.review_out(review)


@router.post("/reviews/{review_id}/issues/{issue_id}/dismiss", response_model=VideoReviewOut)
def dismiss_issue(
    review_id: int, issue_id: str, db: Session = Depends(get_db), admin: User = Depends(require_admin)
):
    """Ignore a finding: it no longer counts as open (nor blocks publishing)."""
    return _set_issue(db, review_id, issue_id, video_reviews.DISMISSED, admin)


@router.post("/reviews/{review_id}/issues/{issue_id}/reopen", response_model=VideoReviewOut)
def reopen_issue(
    review_id: int, issue_id: str, db: Session = Depends(get_db), admin: User = Depends(require_admin)
):
    """Count a finding as open again (one dismissed, or fixed by a change that didn't fix it after all)."""
    return _set_issue(db, review_id, issue_id, video_reviews.OPEN, admin)
