"""
Playable URLs for module media.

New media lives in private storage as `MediaAsset` rows and is served through short-lived signed
URLs, signed in one batch per response. Modules created by the previous app may still carry a
plain URL in `video_url` (public Storage) or a `heygen://video/{id}` reference; those keep working
through a read-only fallback until the legacy media migration moves them to storage.
"""

import time
from collections.abc import Iterable

from sqlalchemy.orm import Session

from app.db.models import MediaAsset, Module
from app.schemas.courses import MediaRef
from app.services import legacy_heygen

SIGNED_URL_SECONDS = 6 * 3600
_LEGACY_CACHE_SECONDS = 20 * 60
_legacy_cache: dict[str, tuple[float, str | None]] = {}


def legacy_video_url(value: str | None) -> str | None:
    if not value:
        return None
    if value.startswith(("http://", "https://")):
        return value
    video_id = legacy_heygen.video_id(value)
    if not video_id or value.startswith(legacy_heygen.PENDING_PREFIX):
        return None
    cached = _legacy_cache.get(video_id)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    url = legacy_heygen.fresh_url(video_id)
    _legacy_cache[video_id] = (time.monotonic() + _LEGACY_CACHE_SECONDS, url)
    return url


def sign_assets(assets: list[MediaAsset], expires_in: int) -> dict[str, str]:
    """Signed download URLs by asset id. Storage signing arrives with the storage service."""
    return {}


def _asset_ids(module: Module) -> tuple[str | None, ...]:
    return (module.video_asset_id, module.poster_asset_id, module.captions_asset_id, module.document_asset_id)


class MediaResolver:
    """Collects the assets a response needs, signs them in one call and hands out URLs."""

    def __init__(self, db: Session):
        self.db = db
        self._assets: dict[str, MediaAsset] = {}
        self._urls: dict[str, str] = {}

    def prepare(self, modules: Iterable[Module] = (), extra_asset_ids: Iterable[str | None] = ()) -> "MediaResolver":
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
