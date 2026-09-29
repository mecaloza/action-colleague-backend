"""
AI presenter with HeyGen API v3.

HeyGen never lays out the video: it only animates an avatar lip-synced to OUR narration (the same
master audio the final video uses), rendered square at 720p. The composer crops it into a bubble.
The narration is uploaded once and the video request carries an Idempotency-Key chosen by the
render job, so a retried request never pays twice.
"""

import logging
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)

API = "https://api.heygen.com"
MAX_ASSET_BYTES = 32 * 1024 * 1024  # HeyGen's upload limit; bigger narrations go by URL
CHUNK_BYTES = 1024 * 1024  # when downloading a file
REQUEST_TIMEOUT_SECONDS = 60
TRANSFER_TIMEOUT_SECONDS = 600  # uploading the narration, downloading the finished video
LOOKS_PAGE_SIZE = 50
MAX_LOOK_PAGES = 4  # a few pages are plenty for a picker
LOG_DETAIL_CHARS = 300  # of an error response kept in the log line


class AvatarError(RuntimeError):
    """A presenter request failed; the message can be shown to the admin.

    `retryable`: a temporary failure (rate limit or server error), so trying again later can work.
    """

    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


@dataclass
class AvatarLook:
    id: str
    name: str
    preview_image_url: str
    preview_video_url: str
    gender: str = ""


@dataclass
class RenderStatus:
    status: str  # pending | processing | completed | failed
    video_url: str | None = None
    error: str | None = None


class AvatarProvider(Protocol):
    def looks(self) -> list[AvatarLook]: ...
    def upload_audio(self, audio: Path) -> str: ...
    def start(
        self, avatar_id: str, background: str, *, idempotency_key: str,
        audio_asset_id: str | None = None, audio_url: str | None = None,
    ) -> str: ...
    def status(self, video_id: str) -> RenderStatus: ...
    def download(self, url: str, dest: Path) -> None: ...


def _unexpected(action: str) -> AvatarError:
    """A response we can't read (not JSON, or missing what we need): maybe a hiccup, so worth a retry."""
    logger.error("heygen_unexpected_response", extra={"action": action})
    return AvatarError(f"HeyGen respondió algo inesperado al {action}.", retryable=True)


def _avatar_look(item: dict) -> AvatarLook:
    return AvatarLook(
        id=item["id"],
        name=item.get("name") or "",
        preview_image_url=item.get("preview_image_url") or "",
        preview_video_url=item.get("preview_video_url") or "",
        gender=item.get("gender") or "",
    )


class HeyGen:
    def __init__(self, api_key: str, engine: str, client: httpx.Client | None = None):
        self.client = client or httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS)
        self.headers = {"x-api-key": api_key}
        self.engine = engine

    @staticmethod
    @contextmanager
    def _network(action: str):
        """A dropped connection, timeout, redirect loop or garbled body is worth a retry, like a 5xx."""
        try:
            yield
        except httpx.RequestError as exc:
            logger.warning("heygen_unreachable", extra={"action": action, "error_type": type(exc).__name__})
            raise AvatarError(f"No pudimos conectar con HeyGen al {action}.", retryable=True) from exc

    def _check(self, response: httpx.Response, action: str) -> httpx.Response:
        if response.is_error:
            response.read()  # streamed responses have no body in memory yet
            detail = response.text[:LOG_DETAIL_CHARS]
            logger.error("heygen_error", extra={"action": action, "status": response.status_code, "detail": detail})
            if response.status_code == 401:
                raise AvatarError("La API key de HeyGen no es válida.")
            if response.status_code in (402, 403):
                raise AvatarError("La cuenta de HeyGen no tiene créditos o permisos para este presentador.")
            retryable = response.status_code == 429 or response.status_code >= 500
            raise AvatarError(f"HeyGen respondió {response.status_code} al {action}.", retryable=retryable)
        return response

    @staticmethod
    def _body(response: httpx.Response, action: str) -> dict:
        """The JSON object of a response."""
        try:
            body = response.json()
        except ValueError as exc:
            raise _unexpected(action) from exc
        if not isinstance(body, dict):
            raise _unexpected(action)
        return body

    @classmethod
    def _field(cls, response: httpx.Response, key: str, action: str) -> str:
        """A required text field of the response's `data` object (or of the body itself)."""
        body = cls._body(response, action)
        data = body.get("data", body)
        value = data.get(key) if isinstance(data, dict) else None
        if not isinstance(value, str) or not value:
            raise _unexpected(action)
        return value

    def _looks_page(self, token: str | None) -> dict:
        params: dict = {"ownership": "public", "avatar_type": "studio_avatar", "limit": LOOKS_PAGE_SIZE}
        if token:
            params["token"] = token
        with self._network("listar los avatares"):
            response = self.client.get(f"{API}/v3/avatars/looks", headers=self.headers, params=params)
        return self._body(self._check(response, "listar los avatares"), "listar los avatares")

    def _supports_engine(self, look: dict) -> bool:
        engines = look.get("supported_api_engines") or []
        return not engines or self.engine in engines

    def looks(self) -> list[AvatarLook]:
        looks: list[AvatarLook] = []
        token = None
        for _ in range(MAX_LOOK_PAGES):
            page = self._looks_page(token)
            items = [item for item in page.get("data") or [] if isinstance(item, dict) and item.get("id")]
            looks += [_avatar_look(item) for item in items if self._supports_engine(item)]
            token = page.get("next_token") if page.get("has_more") else None
            if not token:
                break
        return looks

    def upload_audio(self, audio: Path) -> str:
        """Upload the narration (up to MAX_ASSET_BYTES) and return its asset id."""
        with audio.open("rb") as fh, self._network("subir la narración"):
            response = self._check(
                self.client.post(
                    f"{API}/v3/assets",
                    headers=self.headers,
                    files={"file": (audio.name, fh, "audio/mpeg")},
                    timeout=TRANSFER_TIMEOUT_SECONDS,
                ),
                "subir la narración",
            )
        return self._field(response, "asset_id", "subir la narración")

    def start(
        self, avatar_id: str, background: str, *, idempotency_key: str,
        audio_asset_id: str | None = None, audio_url: str | None = None,
    ) -> str:
        """Ask for the presenter video with an uploaded narration (or one HeyGen downloads from `audio_url`)."""
        body: dict = {
            "type": "avatar",
            "avatar_id": avatar_id,
            "resolution": "720p",
            "aspect_ratio": "1:1",
            "fit": "cover",
            "background": {"type": "color", "value": background},
            "output_format": "mp4",
            "engine": {"type": self.engine},
        }
        if audio_asset_id:
            body["audio_asset_id"] = audio_asset_id
        else:
            body["audio_url"] = audio_url  # too big to upload: HeyGen downloads it from the link
        action = "crear el video del presentador"
        with self._network(action):
            response = self.client.post(
                f"{API}/v3/videos", headers={**self.headers, "Idempotency-Key": idempotency_key}, json=body
            )
        return self._field(self._check(response, action), "video_id", action)

    def status(self, video_id: str) -> RenderStatus:
        action = "consultar el video"
        with self._network(action):
            response = self.client.get(f"{API}/v3/videos/{quote(video_id, safe='')}", headers=self.headers)
        body = self._body(self._check(response, action), action)
        data = body.get("data", body)
        if not isinstance(data, dict):
            raise _unexpected(action)
        return RenderStatus(
            status=str(data.get("status") or "processing"),
            video_url=data.get("video_url") or None,
            error=data.get("failure_message") or data.get("failure_code"),
        )

    def download(self, url: str, dest: Path) -> None:
        action = "descargar el video del presentador"
        with self._network(action), self.client.stream(
            "GET", url, follow_redirects=True, timeout=TRANSFER_TIMEOUT_SECONDS
        ) as response:
            if response.is_error:
                # The link is signed and short-lived: the next status check brings a fresh one.
                logger.warning("heygen_download_failed", extra={"status": response.status_code})
                raise AvatarError(f"No pudimos descargar el video del presentador ({response.status_code}).", retryable=True)
            with dest.open("wb") as fh:
                for chunk in response.iter_bytes(CHUNK_BYTES):
                    fh.write(chunk)


@lru_cache
def get_avatar_provider() -> AvatarProvider | None:
    """None when no presenter can be rendered (no key): videos are then composed without it."""
    settings = get_settings()
    if settings.use_fake_providers:
        from app.services.ai.fake import FakeAvatar

        return FakeAvatar()
    if not settings.heygen_api_key:
        return None
    return HeyGen(settings.heygen_api_key, settings.heygen_engine)
