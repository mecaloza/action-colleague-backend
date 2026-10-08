from typing import Any, Literal

from pydantic import BaseModel

from app.schemas.common import UtcDatetime
from app.schemas.media import JobOut

ReviewStatus = Literal["queued", "running", "done", "error"]
ReviewVerdict = Literal["ok", "warnings", "problems"]
# What a finding is about: in the frames (the AI's), on a slide (the layout lint's: cut_text, overlap,
# presenter_covers, other), in the video itself (FFmpeg's: empty, other) or in the script (the AI's: content).
IssueCategory = Literal["overlap", "cut_text", "presenter_covers", "illegible", "garbled_text", "empty", "mismatch",
                        "other", "content"]
IssueSeverity = Literal["critical", "major", "minor"]
IssueStatus = Literal["open", "dismissed", "fixed"]  # fixed: a change to the script made for it was applied
# Who found it: the AI in the frames, the AI in the script, the layout lint of the slides, FFmpeg in the video.
IssueSource = Literal["vision", "content", "lint", "video"]
FrameKind = Literal["settled", "scene", "random"]


class ReviewIssue(BaseModel):
    id: str
    source: IssueSource
    scene_id: str | None = None
    at_seconds: float | None = None  # where to see it in the video
    frame_index: int | None = None  # the frame that shows it
    category: IssueCategory
    severity: IssueSeverity
    description: str  # for the admin, in Spanish
    suggested_observation: str = ""  # how to ask for the fix ("Pedir un cambio"); "" when a change can't fix it
    box: list[float] | None = None  # [x0, y0, x1, y1], fractions (0-1) of the frame's width and height
    status: IssueStatus = "open"


class ReviewFrame(BaseModel):
    index: int
    at_seconds: float
    scene_id: str | None = None
    kind: FrameKind  # settled: the scene's slide whole; scene: at random inside it; random: anywhere in the video
    url: str | None = None  # a signed link to the JPEG (960 px wide)


class VideoReview(BaseModel):
    id: int
    module_id: int
    video_asset_id: str | None = None
    status: ReviewStatus
    verdict: ReviewVerdict | None = None  # what the review found (null until it is done, and when it failed)
    summary: str = ""
    error: str | None = None  # why it could not be done (what the video's own checks found is still there)
    job_id: str | None = None
    issues: list[ReviewIssue]
    frames: list[ReviewFrame]
    # What the video's own checks found: {lint: {warnings: [LayoutWarning]} | null, black: [Span], silence: [Span],
    # frozen: [Span] | null, presenter_seconds: float | null, content_error?: str}; Span = {start, end, seconds}.
    checks: dict[str, Any]
    created_at: UtcDatetime | None = None
    finished_at: UtcDatetime | None = None


class VideoReviewQueued(BaseModel):
    review: VideoReview
    job: JobOut


class ReviewBadge(BaseModel):
    """The review of a module's video at a glance (`ModuleAdmin.review`)."""

    status: ReviewStatus
    verdict: ReviewVerdict | None = None
    open_issues: int  # still open (neither dismissed nor fixed)
    critical: int  # of them, critical
