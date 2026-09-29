"""
Narration (text-to-speech) and transcription with ElevenLabs.

TTS uses the with-timestamps endpoint so every scene returns its audio plus a character-level
alignment (used to build captions). Voices come from /v2/voices; instant cloning from /v1/voices/add.
Speech-to-text (Scribe) turns uploaded or recorded videos into captions.
"""

import base64
import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)

API = "https://api.elevenlabs.io"
REQUEST_TIMEOUT_SECONDS = 180
TRANSCRIBE_TIMEOUT_SECONDS = 900  # the request waits for the whole transcription
VOICES_PAGE_SIZE = 100
MAX_VOICE_PAGES = 5  # up to 500 voices: the library's premade ones plus the account's own
LOG_DETAIL_CHARS = 300  # of an error response kept in the log line
AUDIO_FORMAT = "mp3_44100_128"
TRANSCRIPTION_MODEL = "scribe_v2"
CONTEXT_CHARS = 1000  # of the neighbouring scenes sent along, so the intonation flows from one scene to the next
VOICE_SETTINGS = {"stability": 0.5, "similarity_boost": 0.75, "style": 0.0, "use_speaker_boost": True}


class VoiceError(RuntimeError):
    """A narration or voice request failed; the message can be shown to the admin.

    `permanent`: retrying can't help (bad key, no quota, a voice that no longer exists, a rejected request).
    """

    def __init__(self, message: str, permanent: bool = False):
        super().__init__(message)
        self.permanent = permanent


@dataclass
class Alignment:
    """When each character of the narrated text starts and ends, in seconds."""

    characters: list[str]
    starts: list[float]
    ends: list[float]


@dataclass
class Speech:
    audio: bytes  # mp3
    alignment: Alignment | None


@dataclass
class VoiceInfo:
    id: str
    name: str
    gender: str = ""
    accent: str = ""
    language: str = ""
    preview_url: str = ""
    category: str = ""


@dataclass
class Word:
    """A transcribed word and when it is said, in seconds."""

    text: str
    start: float
    end: float


class VoiceProvider(Protocol):
    def voices(self) -> list[VoiceInfo]: ...
    def speak(self, text: str, voice_id: str, previous_text: str = "", next_text: str = "") -> Speech: ...
    def clone(self, name: str, sample: Path, mime_type: str) -> str: ...
    def transcribe(self, media_url: str, language: str | None) -> list[Word]: ...


def _voice_info(voice: dict) -> VoiceInfo:
    labels = voice.get("labels") or {}
    return VoiceInfo(
        id=voice["voice_id"],
        name=voice.get("name", ""),
        gender=labels.get("gender", ""),
        accent=labels.get("accent", ""),
        language=labels.get("language", ""),
        preview_url=voice.get("preview_url") or "",
        category=voice.get("category", ""),
    )


def _alignment(raw: dict | None) -> Alignment | None:
    """The character timings of a with-timestamps response; None when it came without them."""
    if not raw or not raw.get("characters"):
        return None
    return Alignment(raw["characters"], raw["character_start_times_seconds"], raw["character_end_times_seconds"])


class ElevenLabs:
    def __init__(self, api_key: str, model: str, client: httpx.Client | None = None):
        self.client = client or httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS)
        self.headers = {"xi-api-key": api_key}
        self.model = model

    def _check(self, response: httpx.Response, action: str) -> httpx.Response:
        if response.is_error:
            status, detail = response.status_code, response.text[:LOG_DETAIL_CHARS]
            logger.error("elevenlabs_error", extra={"action": action, "status": status, "detail": detail})
            reason = detail.lower()
            if status != 429 and "quota" in reason:  # ElevenLabs answers 401 "quota_exceeded" when the credits run out
                raise VoiceError("Se acabaron los créditos de ElevenLabs; recárgalos para seguir narrando.", permanent=True)
            if status == 401:
                raise VoiceError("La API key de ElevenLabs no es válida.", permanent=True)
            if status == 429:
                raise VoiceError("ElevenLabs está limitando las solicitudes; intenta de nuevo en unos minutos.")
            if status in (400, 404, 422) and ("voice_not_found" in reason or "voice_id" in reason):
                raise VoiceError("La voz elegida ya no está disponible en ElevenLabs; elige otra.", permanent=True)
            # Other 4xx (but timeouts and conflicts): the request itself was rejected, so sending it again won't help.
            permanent = 400 <= status < 500 and status not in (408, 409)
            raise VoiceError(f"ElevenLabs respondió {status} al {action}.", permanent=permanent)
        return response

    def _request(self, method: str, url: str, action: str, **kwargs) -> httpx.Response:
        """One call; a dropped connection or timeout becomes a VoiceError too (the job retries it)."""
        try:
            response = self.client.request(method, url, headers=self.headers, **kwargs)
        except httpx.TransportError as exc:
            logger.warning("elevenlabs_unreachable", extra={"action": action, "error_type": type(exc).__name__})
            raise VoiceError(f"No pudimos conectar con ElevenLabs al {action}.") from exc
        return self._check(response, action)

    def voices(self) -> list[VoiceInfo]:
        voices: list[VoiceInfo] = []
        params: dict = {"page_size": VOICES_PAGE_SIZE}
        for _ in range(MAX_VOICE_PAGES):
            page = self._request("GET", f"{API}/v2/voices", "listar las voces", params=params).json()
            voices += [_voice_info(voice) for voice in page.get("voices", [])]
            if not (page.get("has_more") and page.get("next_page_token")):
                break
            params = {**params, "next_page_token": page["next_page_token"]}
        return voices

    def speak(self, text: str, voice_id: str, previous_text: str = "", next_text: str = "") -> Speech:
        body = {"text": text, "model_id": self.model, "voice_settings": VOICE_SETTINGS}
        if previous_text:
            body["previous_text"] = previous_text[-CONTEXT_CHARS:]
        if next_text:
            body["next_text"] = next_text[:CONTEXT_CHARS]
        response = self._request(
            "POST",
            f"{API}/v1/text-to-speech/{quote(voice_id, safe='')}/with-timestamps",  # never a path of its own
            "generar la narración",
            params={"output_format": AUDIO_FORMAT},
            json=body,
        )
        data = response.json()
        return Speech(base64.b64decode(data["audio_base64"]), _alignment(data.get("alignment")))

    def clone(self, name: str, sample: Path, mime_type: str) -> str:
        with sample.open("rb") as fh:
            response = self._request(
                "POST",
                f"{API}/v1/voices/add",
                "clonar la voz",
                data={"name": name, "remove_background_noise": "true"},
                files={"files": (sample.name, fh, mime_type)},
            )
        return response.json()["voice_id"]

    def transcribe(self, media_url: str, language: str | None) -> list[Word]:
        data = {
            "model_id": TRANSCRIPTION_MODEL,
            "source_url": media_url,
            "timestamps_granularity": "word",
            "tag_audio_events": "false",
        }
        if language:
            data["language_code"] = language
        response = self._request(
            "POST", f"{API}/v1/speech-to-text", "transcribir el video", data=data, timeout=TRANSCRIBE_TIMEOUT_SECONDS
        )
        return [
            Word(item["text"], float(item["start"]), float(item["end"]))
            for item in response.json().get("words", [])
            if item.get("type", "word") == "word" and item.get("start") is not None and item.get("end") is not None
        ]


@lru_cache
def get_voice_provider() -> VoiceProvider:
    settings = get_settings()
    if settings.use_fake_providers:
        from app.services.ai.fake import FakeVoice

        return FakeVoice()
    if not settings.elevenlabs_api_key:
        raise VoiceError("La narración no está configurada en el servidor (ELEVENLABS_API_KEY).")
    return ElevenLabs(settings.elevenlabs_api_key, settings.elevenlabs_model)
