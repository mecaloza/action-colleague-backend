"""
Playable URLs for videos the previous app left in HeyGen (`heygen://video/{id}`).

Read-only fallback until `heygen_persist` copies them to Storage. HeyGen's v1 API (the only one
that knows these ids) retires on 2026-10-31; after that this returns None.
"""

import logging

import httpx

from app.core.config import get_settings
from app.services.heygen_persist import HEYGEN_STATUS_URL, PENDING_PREFIX, heygen_video_id

logger = logging.getLogger(__name__)

__all__ = ["PENDING_PREFIX", "fresh_url", "video_id"]

video_id = heygen_video_id


def fresh_url(heygen_id: str) -> str | None:
    api_key = get_settings().heygen_api_key
    if not api_key:
        return None
    try:
        response = httpx.get(
            HEYGEN_STATUS_URL, params={"video_id": heygen_id}, headers={"X-Api-Key": api_key}, timeout=15
        )
        response.raise_for_status()
        data = response.json().get("data") or {}
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("legacy_heygen_url_error", extra={"heygen_video_id": heygen_id, "error_type": type(exc).__name__})
        return None
    return data.get("video_url") if data.get("status") == "completed" else None
