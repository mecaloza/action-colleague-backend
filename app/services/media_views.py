"""API shapes for media assets and jobs (signed URLs included)."""

from sqlalchemy.orm import Session

from app.db.models import Job, MediaAsset
from app.schemas.media import JobOut, MediaAssetOut
from app.services.media import SIGNED_URL_SECONDS, sign_paths


def asset_out(db: Session, asset: MediaAsset) -> MediaAssetOut:
    meta = asset.meta or {}
    page_paths = meta.get("pages", [])
    ready = asset.status == "ready"  # only processed files have something to play
    signed = sign_paths([asset.path, *page_paths], SIGNED_URL_SECONDS, asset_id=asset.id) if ready else {}
    return MediaAssetOut(
        id=asset.id,
        kind=asset.kind,
        status=asset.status,
        mime_type=asset.mime_type,
        size_bytes=asset.size_bytes,
        duration_seconds=asset.duration_seconds,
        width=asset.width,
        height=asset.height,
        original_filename=asset.original_filename,
        course_id=asset.course_id,
        url=signed.get(asset.path),
        error=asset.error,
        pages=[signed[path] for path in page_paths if path in signed],
        text_chars=meta.get("text_chars"),
        created_at=asset.created_at,
    )


def job_out(job: Job) -> JobOut:
    return JobOut(
        id=job.id,
        type=job.type,
        status=job.status,
        progress=job.progress or 0,
        step=job.step or "",
        error=job.error,
        course_id=job.course_id,
        module_id=job.module_id,
        result=job.result,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )
