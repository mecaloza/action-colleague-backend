"""
The automatic review of a module's AI video ("revisión de calidad"): frames of the video, what its own checks find (the
layout lint of its slides; FFmpeg's black screens, long silences and a presenter standing still) and what the AI sees
in the frames and reads in its script, kept as findings ("issues") the admin fixes with a change to the script, or
dismisses. The video itself is never touched: a review only tells.

A render has its video reviewed as soon as it publishes it, when the parameters say so (`qa.auto_review`); an admin
can ask for it again any time. The studio shows the newest review of the module's current video. Its frames live
beside the video (`review/` in its folder) and go with it: its asset names them (`meta.review_frames`).
"""

import logging

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.db.models import Job, MediaAsset, Module, VideoReview
from app.schemas.reviews import ReviewBadge, ReviewFrame, ReviewIssue
from app.schemas.reviews import VideoReview as VideoReviewOut
from app.services.ai import designer
from app.services.media import REVIEW_FRAMES, SIGNED_URL_SECONDS, sign_paths
from app.services.video import compose
from app.services.video.review import FRAME_HEIGHT, FRAME_WIDTH, Frame, Video, timeline_of
from app.worker import queue

logger = logging.getLogger(__name__)

FINISHED = ("done", "error")
OPEN, DISMISSED, FIXED = "open", "dismissed", "fixed"
JOB_TYPE = "video.review"
JOB_ATTEMPTS = 3  # a model that doesn't answer may answer later
# The layout lint's findings (app.services.slides.lint): what kind they are, and how serious.
LINT_CATEGORIES = {
    "text_truncated": "cut_text", "word_broken": "cut_text", "presenter_overlap": "presenter_covers",
    "visual_under_presenter": "presenter_covers", "motif_overlap": "overlap", "frame_overlap": "overlap",
    "text_overlap": "overlap", "outside_safe_area": "other",
}
LINT_SEVERITIES = {"error": "major", "warning": "minor"}
MAX_TEXT_CHARS = 600  # of a finding the model words (description, suggested observation)


# ── Asking for one ────────────────────────────────────────────────────


def request(
    db: Session, module: Module, *, created_by: int | None = None, frames: list[dict] | None = None,
    checks: dict | None = None, render_job_id: str | None = None,
) -> tuple[VideoReview, Job]:
    """Have the module's video reviewed: a new review with its job, or the one still waiting for this very video.
    `frames` and `checks`: what its render already took from it. The caller holds the module and commits."""
    key = f"review:{module.id}:{module.video_asset_id}"
    active = queue.active_with_key(db, key)
    waiting = db.get(VideoReview, (active.payload or {}).get("review_id")) if active is not None else None
    if active is not None and waiting is not None:
        return waiting, active
    review = VideoReview(
        module_id=module.id, video_asset_id=module.video_asset_id, status="queued", frames=frames or [],
        checks=checks, issues=[], created_by=created_by,
        render_job_id=render_job_id or ((module.storyboard or {}).get("render") or {}).get("job_id"),
    )
    db.add(review)
    db.flush()
    job = queue.enqueue(
        db, JOB_TYPE, {"review_id": review.id}, course_id=module.course_id, module_id=module.id,
        created_by=created_by, dedupe_key=key, max_attempts=JOB_ATTEMPTS, commit=False,
    )
    review.job_id = job.id
    return review, job


def keep_frames_with_video(db: Session, video_asset_id: str | None, paths: list[str]) -> bool:
    """Name frames on their video's asset, so they go when it goes (the caller commits). False when it is gone."""
    asset = db.get(MediaAsset, video_asset_id, with_for_update=True, populate_existing=True) if video_asset_id else None
    if asset is None:
        return False
    meta = dict(asset.meta or {})
    meta[REVIEW_FRAMES] = list(dict.fromkeys([*(meta.get(REVIEW_FRAMES) or []), *paths]))
    asset.meta = meta
    return True


# ── Reading them ──────────────────────────────────────────────────────


def current(db: Session, module: Module) -> VideoReview | None:
    """The newest review of the module's AI video (None without one, or before it is first reviewed)."""
    if module.source != "ai" or not module.video_asset_id:
        return None
    return (
        db.query(VideoReview)
        .filter(VideoReview.module_id == module.id, VideoReview.video_asset_id == module.video_asset_id)
        .order_by(VideoReview.id.desc())
        .first()
    )


def _still_open(issues: list[dict] | None) -> list[dict]:
    return [issue for issue in issues or [] if issue.get("status", OPEN) == OPEN]


def _critical(issues: list[dict]) -> int:
    return sum(1 for issue in issues if issue.get("severity") == "critical")


def badge(status: str, verdict: str | None, issues: list[dict] | None) -> ReviewBadge:
    still_open = _still_open(issues)
    return ReviewBadge(status=status, verdict=verdict, open_issues=len(still_open), critical=_critical(still_open))


def _ai_videos(modules: list[Module]) -> dict[str, int]:
    """{video asset id: module id} of the modules with an AI video."""
    return {module.video_asset_id: module.id for module in modules if module.source == "ai" and module.video_asset_id}


def _newest(videos: dict[str, int], *conditions):
    """The id of the newest review of each of these videos (among those that meet `conditions`)."""
    return (
        select(func.max(VideoReview.id)).where(VideoReview.video_asset_id.in_(list(videos)), *conditions)
        .group_by(VideoReview.video_asset_id)
    )


def badges(db: Session, modules: list[Module]) -> dict[int, ReviewBadge]:
    """The newest review of each module's AI video, at a glance, in one query."""
    videos = _ai_videos(modules)
    if not videos:
        return {}
    rows = db.query(
        VideoReview.module_id, VideoReview.video_asset_id, VideoReview.status, VideoReview.verdict, VideoReview.issues
    ).filter(VideoReview.id.in_(_newest(videos)))
    return {
        module_id: badge(status, verdict, issues)
        for module_id, video, status, verdict, issues in rows if videos.get(video) == module_id
    }


def blocking(db: Session, modules: list[Module]) -> dict[int, int]:
    """The open critical findings that keep each module from being published (with `qa.block_publish`), in one query:
    those of the newest review of its AI video and, while that one isn't done (waiting, running or failed), those of
    the newest one that is too. Asking for a review again, or one that fails, never lets them through: only a review
    that is done stands for the video."""
    videos = _ai_videos(modules)
    if not videos:
        return {}
    rows = db.query(VideoReview.module_id, VideoReview.video_asset_id, VideoReview.issues).filter(
        or_(VideoReview.id.in_(_newest(videos)), VideoReview.id.in_(_newest(videos, VideoReview.status == "done")))
    )
    found: dict[int, int] = {}
    for module_id, video, issues in rows:
        if videos.get(video) == module_id:
            found[module_id] = found.get(module_id, 0) + _critical(_still_open(issues))
    return {module_id: count for module_id, count in found.items() if count}


def review_out(review: VideoReview) -> VideoReviewOut:
    """The review as the API shows it, its frames signed."""
    frames = list(review.frames or [])
    signed = sign_paths([frame["path"] for frame in frames if frame.get("path")], SIGNED_URL_SECONDS,
                        review_id=review.id)
    return VideoReviewOut(
        id=review.id, module_id=review.module_id, video_asset_id=review.video_asset_id, status=review.status,
        verdict=review.verdict, summary=review.summary or "", error=review.error, job_id=review.job_id,
        issues=[ReviewIssue.model_validate(issue) for issue in review.issues or []],
        frames=[
            ReviewFrame(index=frame["index"], at_seconds=frame["at_seconds"], scene_id=frame.get("scene_id"),
                        kind=frame.get("kind", "random"), url=signed.get(frame.get("path")))
            for frame in frames
        ],
        checks=dict(review.checks or {}), created_at=review.created_at, finished_at=review.finished_at,
    )


# ── What the admin does with each finding ─────────────────────────────


def set_issue_status(review: VideoReview, issue_id: str, status: str) -> bool:
    """One finding dismissed, open again or fixed (the caller holds the review and commits). False when the review
    has no such finding."""
    issues = [dict(issue) for issue in review.issues or []]
    for issue in issues:
        if issue.get("id") == issue_id:
            issue["status"] = status
            review.issues = issues
            return True
    return False


def mark_issue(db: Session, module_id: int, issue_id: str | None, status: str) -> None:
    """A change to the module made for a finding (`qa_issue_id`) was applied (fixed) or undone (open again). The
    finding is found by its id, which starts with its review's ("12-3"); one of another module, or that no longer
    exists, is left alone. The caller commits."""
    review_id, _, number = (issue_id or "").partition("-")
    if not review_id.isdigit() or not number:
        return
    review = db.get(VideoReview, int(review_id), with_for_update=True, populate_existing=True)
    if review is None or review.module_id != module_id:
        return
    if set_issue_status(review, issue_id, status):
        logger.info("review_issue_marked", extra={"review_id": review.id, "issue_id": issue_id, "status": status})


# ── What the video shows ──────────────────────────────────────────────


def video_from_render(state: dict, *, presenter: bool, duration: float | None = None) -> Video:
    """What a video shows, from the state its render kept: the script it froze, when each scene shows and, as the
    composition used them, the presenter and the scenes' pictures (`presenter`: told otherwise, for a render from
    before the state said)."""
    scenes = list((state.get("input") or {}).get("scenes") or [])
    timeline = timeline_of(state, scenes)
    composed = state.get("composed") or {}
    pictures: frozenset[int] = frozenset()
    if composed.get("visuals", True):  # unless the video came out without them (FFmpeg couldn't use them)
        made = (state.get("visuals") or {}).items()
        pictures = frozenset(int(key) for key, entry in made if str(key).isdigit() and (entry or {}).get("path"))
    return Video(
        scenes=scenes, timeline=timeline, duration=timeline.total if timeline else float(duration or 0),
        presenter=bool(composed.get("presenter", presenter)), pictures=pictures,
    )


def frame_entries(video: Video, frames: list[Frame]) -> list[dict]:
    """The frames as a review keeps them (whoever stores them adds where: `path`)."""
    return [
        {"index": frame.index, "at_seconds": round(frame.at, 2), "scene_id": video.scene_id(frame.scene),
         "kind": frame.kind}
        for frame in frames
    ]


def frames_of(video: Video, entries: list[dict]) -> list[Frame]:
    """The frames a review keeps, back as frames of the video."""
    places = video.places()
    return [Frame(entry["index"], float(entry["at_seconds"]), places.get(entry.get("scene_id")), entry.get("kind", ""))
            for entry in entries]


# ── Findings ──────────────────────────────────────────────────────────


def _clock(seconds: float) -> str:
    whole = max(0, int(seconds))
    return f"{whole // 60}:{whole % 60:02d}"


def _finding(source: str, category: str, severity: str, description: str, *, scene_id: str | None = None,
             at_seconds: float | None = None, frame_index: int | None = None, suggested_observation: str = "",
             box: list[float] | None = None) -> dict:
    return {
        "source": source, "scene_id": scene_id, "at_seconds": None if at_seconds is None else round(at_seconds, 2),
        "frame_index": frame_index, "category": category, "severity": severity,
        "description": description.strip()[:MAX_TEXT_CHARS], "box": box,
        "suggested_observation": suggested_observation.strip()[:MAX_TEXT_CHARS],
    }


def check_issues(video: Video, frames: list[dict], checks: dict | None) -> list[dict]:
    """What the video's own checks found, as findings: the layout lint of each slide (on the scene's settled frame),
    black screens, long silences and the presenter standing still or ending before the narration."""
    checks = checks or {}
    settled = {frame.get("scene_id"): frame for frame in frames if frame.get("kind") == "settled"}
    numbers = {scene_id: index + 1 for scene_id, index in video.places().items()}
    found = []
    for warning in (checks.get("lint") or {}).get("warnings") or []:
        scene_id, message = warning.get("scene_id"), warning.get("message", "")
        frame, number = settled.get(scene_id), numbers.get(scene_id)
        where = f"En la escena {number}: " if number else ""
        category = LINT_CATEGORIES.get(warning.get("code"), "other")
        found.append(_finding(
            "lint", category, LINT_SEVERITIES.get(warning.get("severity"), "minor"),
            f"{where}{message}", scene_id=scene_id if number else None,
            at_seconds=frame["at_seconds"] if frame else None, frame_index=frame["index"] if frame else None,
            suggested_observation=f"{where}{message}",
        ))

    def scene_at(seconds: float) -> str | None:
        return video.scene_id(video.timeline.scene_at(seconds)) if video.timeline else None

    stretches = (
        ("black", "empty", "La pantalla queda en negro {seconds:.1f} s desde el {clock}."),
        ("silence", "other", "No se oye la narración durante {seconds:.1f} s desde el {clock}."),
        ("frozen", "other", "El presentador se queda inmóvil {seconds:.1f} s desde el {clock}."),
    )
    for key, category, text in stretches:  # made by the render, not the script: no change to it fixes them
        for span in checks.get(key) or []:
            found.append(_finding("video", category, "major",
                                  text.format(seconds=span["seconds"], clock=_clock(span["start"])),
                                  scene_id=scene_at(span["start"]), at_seconds=span["start"]))
    clip, total = checks.get("presenter_seconds"), video.timeline.total if video.timeline else video.duration
    if clip is not None and clip < total - compose.PRESENTER_HOLD_SECONDS:
        found.append(_finding(
            "video", "other", "major",
            f"El video del presentador dura {clip:.0f} s y la narración {total:.0f} s: al final el presentador se "
            "queda inmóvil y luego desaparece.", scene_id=scene_at(clip), at_seconds=clip,
        ))
    return found


def _box(box: list[int]) -> list[float] | None:
    """The model's box (pixels of the frame) as fractions of it; None for the whole frame or a box that isn't one."""
    if len(box) != 4:
        return None
    left, top = max(0, min(box[0], FRAME_WIDTH)), max(0, min(box[1], FRAME_HEIGHT))
    right, bottom = max(0, min(box[2], FRAME_WIDTH)), max(0, min(box[3], FRAME_HEIGHT))
    if right <= left or bottom <= top:
        return None
    return [round(left / FRAME_WIDTH, 4), round(top / FRAME_HEIGHT, 4), round(right / FRAME_WIDTH, 4),
            round(bottom / FRAME_HEIGHT, 4)]


def vision_issues(answer: designer.FrameReview, frames: list[dict], video: Video) -> list[dict]:
    """What the model saw in the frames, each where it saw it (the frame tells the scene, whatever the model says)."""
    by_index = {frame["index"]: frame for frame in frames}
    known = video.places()
    found = []
    for issue in answer.issues:
        frame = by_index.get(issue.frame_index)
        scene_id = frame.get("scene_id") if frame else issue.scene_id if issue.scene_id in known else None
        found.append(_finding(
            "vision", issue.category, issue.severity, issue.description, scene_id=scene_id,
            at_seconds=frame["at_seconds"] if frame else None, frame_index=frame["index"] if frame else None,
            suggested_observation=issue.suggested_observation, box=_box(issue.box) if frame else None,
        ))
    return found


def content_issues(answer: designer.ContentReview, video: Video) -> list[dict]:
    """What the model read wrong in the script, each at its scene (none: about the quiz or the whole module)."""
    places = video.places()
    found = []
    for issue in answer.issues:
        index = places.get(issue.scene_id) if issue.scene_id else None
        at = video.timeline.starts[index] if index is not None and video.timeline else None
        found.append(_finding("content", "content", issue.severity, issue.description,
                              scene_id=video.scene_id(index), at_seconds=at,
                              suggested_observation=issue.suggested_observation))
    return found


def numbered(review_id: int, issues: list[dict], previous: list[dict] | None) -> list[dict]:
    """The findings with their ids ("{review}-{n}") and, for those the review already had, the status the admin gave
    them meanwhile (what the checks found is kept while the AI looks, and it comes first)."""
    statuses = {issue.get("id"): issue.get("status", OPEN) for issue in previous or []}
    return [
        {"id": f"{review_id}-{number}", **issue, "status": statuses.get(f"{review_id}-{number}", OPEN)}
        for number, issue in enumerate(issues, start=1)
    ]


def verdict(issues: list[dict]) -> str:
    severities = {issue["severity"] for issue in issues}
    if severities & {"critical", "major"}:
        return "problems"
    return "warnings" if severities else "ok"
