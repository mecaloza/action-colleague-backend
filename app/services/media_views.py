"""API shapes for media assets and jobs (signed URLs included)."""

from sqlalchemy.orm import Session

from app.db.models import Job, MediaAsset
from app.schemas.media import JobOut, MediaAssetOut
from app.services.media import SIGNED_URL_SECONDS, sign_paths


def _paths(asset: MediaAsset) -> list[str]:
    """What an API response links to; only processed files have something to show."""
    return [asset.path, *(asset.meta or {}).get("pages", [])] if asset.status == "ready" else []


def asset_out(db: Session, asset: MediaAsset, signed: dict[str, str] | None = None) -> MediaAssetOut:
    meta = asset.meta or {}
    page_paths = meta.get("pages", [])
    if signed is None:
        signed = sign_paths(_paths(asset), SIGNED_URL_SECONDS, asset_id=asset.id)
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


def assets_out(db: Session, assets: list[MediaAsset]) -> list[MediaAssetOut]:
    """Many assets with their links signed in one storage call."""
    signed = sign_paths([path for asset in assets for path in _paths(asset)], SIGNED_URL_SECONDS, assets=len(assets))
    return [asset_out(db, asset, signed) for asset in assets]


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
