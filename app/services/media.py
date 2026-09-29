"""
Playable URLs for module media.

New media lives in private storage as `MediaAsset` rows and is served through short-lived signed
URLs, signed in one batch per response. Modules created by the previous app may still carry a
plain URL in `video_url` (public Storage) or a `heygen://video/{id}` reference; those keep working
through a read-only fallback until the legacy media migration moves them to storage.
"""

import logging
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.db.models import Course, MediaAsset, Module
from app.schemas.courses import MediaRef
from app.services import legacy_heygen
from app.services.storage import StorageError, get_storage

logger = logging.getLogger(__name__)

SIGNED_URL_SECONDS = 6 * 3600
_LEGACY_CACHE_SECONDS = 20 * 60
_legacy_cache: dict[str, tuple[float, str | None]] = {}


def _heygen_reference(value: str | None) -> str | None:
    if not value or value.startswith(legacy_heygen.PENDING_PREFIX):
        return None
    return legacy_heygen.video_id(value)


def _cached(video_id: str) -> tuple[float, str | None] | None:
    cached = _legacy_cache.get(video_id)
    return cached if cached and cached[0] > time.monotonic() else None


def _remember(video_id: str, url: str | None) -> str | None:
    _legacy_cache[video_id] = (time.monotonic() + _LEGACY_CACHE_SECONDS, url)
    return url


def prefetch_legacy_urls(values: Iterable[str | None]) -> None:
    """Ask HeyGen for every uncached reference of a response at once, instead of one after another."""
    if datetime.now(UTC).date() > legacy_heygen.RETIRED_ON:
        return  # its API is gone: those references resolve to nothing
    ids = sorted({video_id for value in values if (video_id := _heygen_reference(value)) and not _cached(video_id)})
    if not ids:
        return
    with ThreadPoolExecutor(max_workers=min(8, len(ids))) as pool:
        for video_id, url in zip(ids, pool.map(legacy_heygen.fresh_url, ids)):
            _remember(video_id, url)


def legacy_video_url(value: str | None) -> str | None:
    if not value:
        return None
    if value.startswith(("http://", "https://")):
        return value
    video_id = _heygen_reference(value)
    if not video_id:
        return None
    cached = _cached(video_id)
    return cached[1] if cached else _remember(video_id, legacy_heygen.fresh_url(video_id))


def sign_paths(paths: list[str], expires_in: int, **log_context) -> dict[str, str]:
    """Signed download URLs by storage path, in one call. Storage errors leave media out, not the page."""
    if not paths:
        return {}
    try:
        return get_storage().signed_urls(paths, expires_in)
    except StorageError as exc:
        logger.warning("media_sign_failed", extra={**log_context, "error": str(exc)})
        return {}


def sign_assets(assets: list[MediaAsset], expires_in: int) -> dict[str, str]:
    """Signed download URLs by asset id."""
    by_path = sign_paths([asset.path for asset in assets], expires_in, assets=len(assets))
    return {asset.id: by_path[asset.path] for asset in assets if asset.path in by_path}


def _referenced(db: Session, asset_ids: set[str]) -> set[str]:
    """Which of these assets a module or a course still points at."""
    columns = (Module.video_asset_id, Module.poster_asset_id, Module.captions_asset_id, Module.document_asset_id)
    rows = db.query(*columns).filter(or_(*(column.in_(asset_ids) for column in columns))).all()
    used = {value for row in rows for value in row if value in asset_ids}
    used.update(cover for (cover,) in db.query(Course.cover_asset_id).filter(Course.cover_asset_id.in_(asset_ids)))
    return used


def discard_assets(db: Session, asset_ids: Iterable[str | None]) -> None:
    """
    Delete assets nothing points at any more: rows now (committing the caller's changes too), files
    after. Storage failures are logged, not raised: an orphaned file costs little, a failed edit more.
    """
    wanted = {asset_id for asset_id in asset_ids if asset_id}
    # Rows locked before the check (in id order: two cleanups can't deadlock), so nothing can start
    # pointing at them in between; ON DELETE SET NULL would silently undo that new link.
    assets = (
        db.query(MediaAsset).filter(MediaAsset.id.in_(wanted)).order_by(MediaAsset.id).with_for_update().all()
        if wanted else []
    )
    used = _referenced(db, {asset.id for asset in assets}) if assets else set()
    assets = [asset for asset in assets if asset.id not in used]
    for asset in assets:
        db.delete(asset)
    db.flush()
    paths = {path for asset in assets for path in (asset.path, *(asset.meta or {}).get("pages", []))}
    if paths:  # two rows can share a file (a retried or handed-back job): it goes with the last one
        paths -= {path for (path,) in db.query(MediaAsset.path).filter(MediaAsset.path.in_(paths))}
    db.commit()
    if paths:
        try:
            get_storage().delete(sorted(paths))
        except StorageError as exc:
            logger.warning("media_files_left_behind", extra={"files": len(paths), "error": str(exc)[:300]})


def _asset_ids(module: Module) -> tuple[str | None, ...]:
    return (module.video_asset_id, module.poster_asset_id, module.captions_asset_id, module.document_asset_id)


class MediaResolver:
    """Collects the assets a response needs, signs them in one call and hands out URLs."""

    def __init__(self, db: Session):
        self.db = db
        self._assets: dict[str, MediaAsset] = {}
        self._urls: dict[str, str] = {}

    def prepare(self, modules: Iterable[Module] = (), extra_asset_ids: Iterable[str | None] = ()) -> "MediaResolver":
        modules = list(modules)
        prefetch_legacy_urls(module.video_url for module in modules if not module.video_asset_id)
        ids = {asset_id for module in modules for asset_id in _asset_ids(module) if asset_id}
        ids.update(asset_id for asset_id in extra_asset_ids if asset_id)
        if ids:
            assets = self.db.query(MediaAsset).filter(MediaAsset.id.in_(ids), MediaAsset.status == "ready").all()
            self._assets = {asset.id: asset for asset in assets}
            self._urls = sign_assets(list(self._assets.values()), SIGNED_URL_SECONDS)
        return self

    def url(self, asset_id: str | None) -> str | None:
        return self._urls.get(asset_id) if asset_id else None

    def _ref(self, asset_id: str | None) -> MediaRef | None:
        asset = self._assets.get(asset_id) if asset_id else None
        url = self.url(asset_id)
        if not asset or not url:
            return None
        return MediaRef(
            url=url,
            mime_type=asset.mime_type,
            duration_seconds=asset.duration_seconds,
            width=asset.width,
            height=asset.height,
        )

    def video(self, module: Module) -> MediaRef | None:
        ref = self._ref(module.video_asset_id)
        if ref:
            return ref
        legacy = legacy_video_url(module.video_url)
        return MediaRef(url=legacy, mime_type="video/mp4", duration_seconds=module.duration_seconds) if legacy else None

    def document(self, module: Module) -> MediaRef | None:
        return self._ref(module.document_asset_id)
