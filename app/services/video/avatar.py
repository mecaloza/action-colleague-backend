"""
AI presenter with HeyGen API v3.

HeyGen never lays out the video: it only animates an avatar lip-synced to OUR narration (the same
master audio the final video uses), rendered square at 720p. The composer crops it into a bubble.
Requests carry an Idempotency-Key derived from (audio, avatar, engine), so retries never pay twice.
"""

import hashlib
import logging
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Protocol

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)

API = "https://api.heygen.com"
MAX_ASSET_BYTES = 32 * 1024 * 1024  # HeyGen's upload limit; bigger narrations go by URL
CHUNK_BYTES = 1024 * 1024  # when hashing or downloading a file
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
    def start(self, audio: Path, avatar_id: str, background: str, audio_url: str | None = None) -> str: ...
    def status(self, video_id: str) -> RenderStatus: ...
    def download(self, url: str, dest: Path) -> None: ...


def idempotency_key(audio: Path, avatar_id: str, engine: str) -> str:
    """The same narration, avatar and engine always give the same key."""
    digest = hashlib.sha256()
    with audio.open("rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK_BYTES), b""):
            digest.update(chunk)
    digest.update(f"|{avatar_id}|{engine}".encode())
    return f"ac-presenter-{digest.hexdigest()[:48]}"


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
        """A dropped connection or timeout is worth a retry, like a 5xx."""
        try:
            yield
        except httpx.TransportError as exc:
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
    def _data(response: httpx.Response) -> dict:
        body = response.json()
        return body.get("data", body)

    def _looks_page(self, token: str | None) -> dict:
        params: dict = {"ownership": "public", "avatar_type": "studio_avatar", "limit": LOOKS_PAGE_SIZE}
        if token:
            params["token"] = token
        with self._network("listar los avatares"):
            response = self.client.get(f"{API}/v3/avatars/looks", headers=self.headers, params=params)
        return self._check(response, "listar los avatares").json()

    def _supports_engine(self, look: dict) -> bool:
        engines = look.get("supported_api_engines") or []
        return not engines or self.engine in engines

    def looks(self) -> list[AvatarLook]:
        looks: list[AvatarLook] = []
        token = None
        for _ in range(MAX_LOOK_PAGES):
            page = self._looks_page(token)
            looks += [_avatar_look(item) for item in page.get("data") or [] if self._supports_engine(item)]
            token = page.get("next_token") if page.get("has_more") else None
            if not token:
                break
        return looks

    def _upload_audio(self, audio: Path) -> str:
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
        return self._data(response)["asset_id"]

    def start(self, audio: Path, avatar_id: str, background: str, audio_url: str | None = None) -> str:
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
        if audio_url and audio.stat().st_size > MAX_ASSET_BYTES:
            body["audio_url"] = audio_url  # too big to upload: HeyGen downloads it from the link
        else:
            body["audio_asset_id"] = self._upload_audio(audio)
        with self._network("crear el video del presentador"):
            response = self.client.post(
                f"{API}/v3/videos",
                headers={**self.headers, "Idempotency-Key": idempotency_key(audio, avatar_id, self.engine)},
                json=body,
            )
        return self._data(self._check(response, "crear el video del presentador"))["video_id"]

    def status(self, video_id: str) -> RenderStatus:
        with self._network("consultar el video"):
            response = self.client.get(f"{API}/v3/videos/{video_id}", headers=self.headers)
        response = self._check(response, "consultar el video")
        data = self._data(response)
        return RenderStatus(
            status=data.get("status", "processing"),
            video_url=data.get("video_url"),
            error=data.get("failure_message") or data.get("failure_code"),
        )

    def download(self, url: str, dest: Path) -> None:
        with self._network("descargar el video del presentador"), self.client.stream(
            "GET", url, follow_redirects=True, timeout=TRANSFER_TIMEOUT_SECONDS
        ) as response:
            self._check(response, "descargar el video del presentador")
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
