"""
video.review: a module's AI video -> its review ("revisión de calidad"; see app.services.video_reviews). The frames its
render took (or, for a video reviewed again or made before reviews, frames taken here from the published video) are
read against what each scene should show, with what the video's own checks found; then the AI reads the script against
the quiz and the course's materials.

It never changes the module, its video or its generation status. A review whose frames can't be taken or looked at
says why (status "error") and keeps what the checks found, and the script is read all the same (that needs no
pictures); a model that doesn't answer is asked again later (the job retries).
"""

import logging
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from app.db.models import Job, MediaAsset, Module, VideoReview
from app.services import quiz, studio, video_reviews
from app.services.ai import designer
from app.services.ai.llm import LLMError, get_llm
from app.services.storage import StorageError, get_storage
from app.services.video import compose
from app.services.video import review as video_review
from app.services.video.review import Video
from app.worker.runner import JobContext, JobError, handler, open_session

logger = logging.getLogger(__name__)

REPLACED = "El video cambió antes de revisarlo; esta revisión ya no aplica."
# A video whose render isn't known (made before its render kept its script): the module's script now may not be the
# one it was made from, and an empty one would make every question of the quiz look unanswerable.
NO_SCRIPT = "No se sabe con qué guion se produjo este video."


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _review_failed(db: Session, job: Job, error: str) -> None:
    review = db.get(VideoReview, (job.payload or {}).get("review_id"))
    if review is not None and review.status not in video_reviews.FINISHED:
        review.status, review.error, review.verdict, review.finished_at = "error", error, None, _now()


class _Replaced(Exception):
    """The video was replaced (or the module deleted) while it was being reviewed."""


@dataclass
class _Ask:
    """What the review works with, read while the database is open."""

    review_id: int
    video_asset_id: str
    video_path: str
    course_title: str
    module_title: str
    video: Video
    frames: list[dict]  # those its render took ([] until taken)
    checks: dict | None
    lint: dict | None  # the layout lint of the slides as the render drew them
    materials: str
    questions: list[dict]

    @property
    def folder(self) -> str:
        """The video's folder in storage: its frames go beside it."""
        return self.video_path.rsplit("/", 1)[0]


def _read(db: Session, review: VideoReview, module: Module, asset: MediaAsset) -> _Ask:
    render = (module.storyboard or {}).get("render") or {}
    job_id = review.render_job_id or render.get("job_id")
    state = (db.query(Job.state).filter(Job.id == job_id).scalar() if job_id else None) or {}
    return _Ask(
        review_id=review.id, video_asset_id=asset.id, video_path=asset.path, course_title=module.course.title,
        module_title=module.title,
        video=video_reviews.video_from_render(state, presenter=bool(render.get("avatar_id")),
                                              duration=asset.duration_seconds or module.duration_seconds),
        frames=[dict(frame) for frame in review.frames or []], checks=dict(review.checks) if review.checks else None,
        lint=state.get("lint"), materials=studio.materials_text(db, module.course_id),
        questions=quiz.dump_questions(quiz.evaluation_questions(module.evaluation)),
    )


def _sample(ctx: JobContext, ask: _Ask, work: Path) -> dict[int, Path]:
    """Take the frames and the checks from the published video (it is downloaded) and keep them on the review.
    Returns each frame's local file, by its index."""
    storage = get_storage()
    local = work / "video.mp4"
    storage.download_file(ask.video_path, local)
    duration = ask.video.duration or compose.clip_seconds(local)
    frames = video_review.sample(ask.video.timeline, duration, seed=ctx.job_id)
    if not frames:
        raise video_review.ReviewError("El video es demasiado corto para revisarlo.")
    files = video_review.extract_frames(local, frames, work / "frames")
    checks = {"lint": ask.lint, **video_review.video_checks(local, duration, ask.video.bubble()),
              "presenter_seconds": None}  # its clip is the render's: unknown here (a still bubble tells anyway)
    folder = f"{ask.folder}/review/{ask.review_id}"  # beside the video, apart from its other reviews' frames
    entries = [{**entry, "path": f"{folder}/{video_review.frame_name(entry['index'])}"}
               for entry in video_reviews.frame_entries(ask.video, frames)]
    with open_session() as db:  # named on the video first: whatever happens next, they go when it goes
        if not video_reviews.keep_frames_with_video(db, ask.video_asset_id, [entry["path"] for entry in entries]):
            raise _Replaced()
        db.commit()
    for entry, file in zip(entries, files):
        storage.upload_file(entry["path"], file, "image/jpeg")
    with open_session() as db:
        review = db.get(VideoReview, ask.review_id, with_for_update=True)
        gone = review is None or db.get(MediaAsset, ask.video_asset_id) is None
        if not gone:
            review.frames, review.checks = entries, checks
            db.commit()
    if gone:  # the video went while its frames were uploading: they go too
        _delete_files([entry["path"] for entry in entries])
        raise _Replaced()
    ask.frames, ask.checks = entries, checks
    return {entry["index"]: file for entry, file in zip(entries, files)}


def _delete_files(paths: list[str]) -> None:
    """Best effort: a leftover file only costs storage."""
    try:
        get_storage().delete(paths)
    except StorageError as exc:
        logger.warning("review_cleanup_failed", extra={"files": len(paths), "error": str(exc)[:300]})


def _images(entries: list[dict], local: dict[int, Path], work: Path) -> list[bytes]:
    """Each frame's JPEG, in order: taken here, or downloaded from beside the video."""
    images = []
    for entry in entries:
        file = local.get(entry["index"])
        if file is None:
            file = work / f"frame_{entry['index']:02d}.jpg"
            get_storage().download_file(entry["path"], file)
        images.append(file.read_bytes())
    return images


def _keep(review_id: int, issues: list[dict], **fields) -> None:
    """Save the findings (keeping what the admin did with those the review already had) and `fields`."""
    with open_session() as db:
        review = db.get(VideoReview, review_id, with_for_update=True)
        if review is None:
            return
        review.issues = video_reviews.numbered(review.id, issues, review.issues)
        for name, value in fields.items():
            setattr(review, name, value)
        db.commit()


def _finish(review_id: int, issues: list[dict], *, error: str | None = None, **fields) -> None:
    _keep(review_id, issues, status="error" if error else "done", error=error,
          verdict=None if error else video_reviews.verdict(issues), finished_at=_now(), **fields)


def _usage(*calls: dict) -> dict | None:
    """The tokens of every call, added up."""
    total: dict = {}
    for usage in calls:
        for key, value in usage.items():
            if isinstance(value, int):
                total[key] = total.get(key, 0) + value
    return total or None


@handler(video_reviews.JOB_TYPE, on_failure=_review_failed)
def review_video(ctx: JobContext) -> dict:
    review_id = ctx.payload["review_id"]
    with open_session() as db:
        review = db.get(VideoReview, review_id)
        if review is None or review.status in video_reviews.FINISHED:
            return {"skipped": "review gone or done"}
        module = db.get(Module, review.module_id)
        asset = db.get(MediaAsset, review.video_asset_id) if review.video_asset_id else None
        if module is None or asset is None or module.video_asset_id != asset.id:
            review.status, review.error, review.finished_at = "error", REPLACED, _now()
            db.commit()
            return {"skipped": "video replaced"}
        ask = _read(db, review, module, asset)
        review.status, review.error = "running", None
        db.commit()

    ctx.progress(5, "Preparando la revisión")
    error = None  # why the frames can't be looked at (what the checks find and the script's reading still count)
    images: list[bytes] = []
    with tempfile.TemporaryDirectory(prefix="review-") as tmp:
        work = Path(tmp)
        local: dict[int, Path] = {}
        if not ask.frames:
            ctx.progress(10, "Tomando fotogramas del video")
            try:
                local = _sample(ctx, ask, work)
            except video_review.ReviewError as exc:  # the video can't be read: trying again won't help
                logger.warning("review_sampling_failed", extra={"review_id": review_id, "error": str(exc)[:300]})
                error, ask.checks = str(exc), {"lint": ask.lint}  # the render's lint is known without the video
            except _Replaced:
                _finish(review_id, [], error=REPLACED)
                return {"skipped": "video replaced"}
        if error is None:
            images = _images(ask.frames, local, work)

    issues = video_reviews.check_issues(ask.video, ask.frames, ask.checks)
    _keep(review_id, issues)  # what the checks found shows while the AI looks
    try:
        llm = get_llm()
    except LLMError as exc:  # no AI here now: maybe later
        raise JobError(str(exc)) from exc
    looking, reading, summaries = {}, {}, []  # the tokens each call takes; what each one says
    if error is None:
        ctx.progress(40, "Revisando los fotogramas con IA")
        try:
            seen = designer.review_frames(
                llm, course_title=ask.course_title, module_title=ask.module_title,
                frames=[ask.video.context(frame) for frame in video_reviews.frames_of(ask.video, ask.frames)],
                known=[issue["description"] for issue in issues], images=images, usage=looking,
            )
        except LLMError as exc:
            if not exc.permanent:
                raise JobError(str(exc)) from exc
            logger.warning("review_vision_failed", extra={"review_id": review_id, "error": str(exc)[:300]})
            error = str(exc)  # e.g. a model that takes no pictures: it can still read the script
        else:
            issues += video_reviews.vision_issues(seen, ask.frames, ask.video)
            summaries.append(seen.summary)

    ctx.progress(80, "Revisando el contenido")
    checks = dict(ask.checks or {})
    if not ask.video.scenes:
        checks["content_error"] = NO_SCRIPT
    else:
        try:
            content = designer.review_content(
                llm, course_title=ask.course_title, module_title=ask.module_title, scenes=ask.video.scenes,
                questions=ask.questions, materials=ask.materials, usage=reading,
            )
        except LLMError as exc:  # the frames' review stands on its own
            logger.warning("review_content_failed", extra={"review_id": review_id, "error": str(exc)[:300]})
            checks["content_error"] = str(exc)
        else:
            issues += video_reviews.content_issues(content, ask.video)
            summaries.append(content.summary)
    summary = " ".join(text.strip() for text in summaries if text.strip())
    _finish(review_id, issues, error=error, summary=summary, checks=checks, usage=_usage(looking, reading),
            model=getattr(llm, "model", None))
    logger.info("video_reviewed", extra={"review_id": review_id, "module_id": ctx.module_id, "issues": len(issues),
                                         "frames": len(ask.frames), "failed": error is not None})
    if error is not None:
        return {"review_id": review_id, "error": error, "issues": len(issues)}
    return {"review_id": review_id, "verdict": video_reviews.verdict(issues), "issues": len(issues)}
