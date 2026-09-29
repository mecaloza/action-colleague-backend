"""
Videos the previous app left in HeyGen (`heygen://video/{id}`, `heygen://pending/{id}`).

HeyGen's v1 API is the only one that knows these ids, and it retires on 2026-10-31. Until then
`fresh_url` gives a playable (short-lived) link: pages use it until `legacy.migrate` has copied the
video into the private bucket, and the migration downloads from it. After that date it returns None.
"""

import logging
import re
from datetime import UTC, date, datetime

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)

HEYGEN_STATUS_URL = "https://api.heygen.com/v1/video_status.get"
PENDING_PREFIX = "heygen://pending/"
VIDEO_PREFIX = "heygen://video/"
RETIRED_ON = date(2026, 10, 31)
_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


def video_id(video_url: str | None) -> str | None:
    """The HeyGen id of a `heygen://` reference, or None (anything else, or a malformed id)."""
    for prefix in (VIDEO_PREFIX, PENDING_PREFIX):
        if video_url and video_url.startswith(prefix):
            heygen_id = video_url[len(prefix):]
            return heygen_id if _VIDEO_ID.fullmatch(heygen_id) else None
    return None

TIMEOUT_SECONDS = 5  # on a page request: better no video than a page that hangs


def fresh_url(heygen_id: str) -> str | None:
    api_key = get_settings().heygen_api_key
    if not api_key or datetime.now(UTC).date() > RETIRED_ON:
        return None
    try:
        response = httpx.get(
            HEYGEN_STATUS_URL,
            params={"video_id": heygen_id},
            headers={"X-Api-Key": api_key},
            timeout=TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        data = response.json().get("data") or {}
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("legacy_heygen_url_error", extra={"heygen_video_id": heygen_id, "error_type": type(exc).__name__})
        return None
    return data.get("video_url") if data.get("status") == "completed" else None
