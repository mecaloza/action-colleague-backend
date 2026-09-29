"""ElevenLabs and HeyGen clients against a fake HTTP transport: errors, pagination, file names."""

import httpx
import pytest

from app.services.video.avatar import AvatarError, HeyGen
from app.services.video.voice import ElevenLabs, VoiceError
from tests.conftest import auth_headers


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_elevenlabs_network_errors_become_voice_errors():
    def down(request):
        raise httpx.ConnectTimeout("timed out", request=request)

    voice = ElevenLabs("key", "eleven_multilingual_v2", client=_client(down))
    with pytest.raises(VoiceError, match="conectar con ElevenLabs"):
        voice.speak("Hola", "voice-1")


def test_elevenlabs_lists_every_page_of_voices():
    pages = {
        None: {"voices": [{"voice_id": "a", "name": "Ana"}], "has_more": True, "next_page_token": "p2"},
        "p2": {"voices": [{"voice_id": "b", "name": "Beto"}], "has_more": False},
    }

    def handler(request):
        return httpx.Response(200, json=pages[request.url.params.get("next_page_token")])

    voices = ElevenLabs("key", "eleven_multilingual_v2", client=_client(handler)).voices()
    assert [voice.id for voice in voices] == ["a", "b"]


def test_heygen_failed_download_is_an_avatar_error(tmp_path):
    def handler(request):
        return httpx.Response(404, text="gone")  # read while streaming, the body is not in memory yet

    heygen = HeyGen("key", "avatar_iii", client=_client(handler))
    with pytest.raises(AvatarError, match="404"):
        heygen.download("https://files.heygen.test/video.mp4", tmp_path / "presenter.mp4")


def test_heygen_network_errors_can_be_retried():
    def down(request):
        raise httpx.ReadError("reset", request=request)

    heygen = HeyGen("key", "avatar_iii", client=_client(down))
    with pytest.raises(AvatarError) as error:
        heygen.status("video-1")
    assert error.value.retryable


def test_a_voice_sample_name_cannot_leave_the_temporary_folder(client, admin, monkeypatch):
    from app.services.ai.fake import FakeVoice

    seen = []
    monkeypatch.setattr(FakeVoice, "clone", lambda self, name, sample, mime: seen.append(sample) or "cloned-1")

    response = client.post(
        "/api/v1/studio/voices/clone",
        headers=auth_headers(client, admin.email),
        data={"name": "Mi voz"},
        files={"file": ("../../../etc/evil.mp3", b"ID3" + b"\0" * 100, "audio/mpeg")},
    )

    assert response.status_code == 201, response.text
    assert seen[0].name == "evil.mp3" and seen[0].parent.name.startswith("tmp")
