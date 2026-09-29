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
    with pytest.raises(AvatarError, match="404") as error:
        heygen.download("https://files.heygen.test/video.mp4", tmp_path / "presenter.mp4")
    assert error.value.retryable  # the link is short-lived: the next status check brings a fresh one


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
    assert seen[0].name == "muestra.mp3" and seen[0].parent.name.startswith("tmp")


@pytest.mark.parametrize("filename", ["..", "x" * 300 + ".mp3"])
def test_any_sample_file_name_is_accepted_safely(client, admin, monkeypatch, filename):
    from app.services.ai.fake import FakeVoice

    seen = []
    monkeypatch.setattr(FakeVoice, "clone", lambda self, name, sample, mime: seen.append(sample) or "cloned-1")
    response = client.post(
        "/api/v1/studio/voices/clone", headers=auth_headers(client, admin.email),
        data={"name": "Mi voz"}, files={"file": (filename, b"ID3" + b"\0" * 100, "audio/mpeg")},
    )
    assert response.status_code == 201, response.text
    assert seen[0].name == "muestra.mp3"


def test_voice_samples_are_validated(client, admin, monkeypatch):
    from app.api.routes import studio

    headers = auth_headers(client, admin.email)
    sample = {"file": ("muestra.mp3", b"ID3" + b"\0" * 100, "audio/mpeg")}
    blank = client.post("/api/v1/studio/voices/clone", headers=headers, data={"name": "   "}, files=sample)
    assert blank.status_code == 422 and "nombre" in blank.json()["detail"]
    image = client.post(
        "/api/v1/studio/voices/clone", headers=headers, data={"name": "Mi voz"},
        files={"file": ("foto.png", b"\x89PNG", "image/png")},
    )
    assert image.status_code == 422
    monkeypatch.setattr(studio, "MAX_VOICE_SAMPLE_BYTES", 50)
    too_big = client.post("/api/v1/studio/voices/clone", headers=headers, data={"name": "Mi voz"}, files=sample)
    assert too_big.status_code == 413


def test_heygen_video_requests_carry_the_jobs_key_and_the_uploaded_narration(tmp_path):
    sent = []

    def handler(request):
        sent.append(request)
        if request.url.path == "/v3/assets":
            return httpx.Response(200, json={"data": {"asset_id": "asset-1"}})
        return httpx.Response(200, json={"data": {"video_id": "video-1"}})

    heygen = HeyGen("key", "avatar_iii", client=_client(handler))
    audio = tmp_path / "narration.mp3"
    audio.write_bytes(b"ID3" + b"\0" * 100)
    asset_id = heygen.upload_audio(audio)
    assert heygen.start("look-1", "#1D1D1D", idempotency_key="ac-presenter-job-1", audio_asset_id=asset_id) == "video-1"

    create = sent[-1]
    assert create.headers["Idempotency-Key"] == "ac-presenter-job-1" and create.headers["x-api-key"] == "key"
    body = create.read().decode()
    assert '"audio_asset_id":"asset-1"' in body.replace(" ", "") and "audio_url" not in body


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={"data": None}),
        httpx.Response(200, json={"data": {"id": "no-asset-id"}}),
        httpx.Response(200, text="<html>not json</html>"),
        httpx.Response(200, json=["a", "list"]),
    ],
)
def test_unexpected_heygen_answers_are_avatar_errors(tmp_path, response):
    heygen = HeyGen("key", "avatar_iii", client=_client(lambda request: response))
    audio = tmp_path / "narration.mp3"
    audio.write_bytes(b"ID3")
    with pytest.raises(AvatarError, match="inesperado") as error:
        heygen.upload_audio(audio)
    assert error.value.retryable
    with pytest.raises(AvatarError):
        heygen.start("look-1", "#1D1D1D", idempotency_key="k", audio_asset_id="asset-1")
    if response.headers.get("content-type", "").startswith("application/json") and response.json() == {"data": None}:
        with pytest.raises(AvatarError):
            heygen.status("video-1")


def test_heygen_redirect_loops_and_garbled_bodies_are_retryable():
    def loop(request):
        raise httpx.TooManyRedirects("loop", request=request)

    heygen = HeyGen("key", "avatar_iii", client=_client(loop))
    with pytest.raises(AvatarError) as error:
        heygen.status("video-1")
    assert error.value.retryable


@pytest.mark.parametrize(
    ("status", "body", "permanent", "message"),
    [
        (401, {"detail": {"status": "quota_exceeded", "message": "You have 0 credits"}}, True, "créditos"),
        (401, {"detail": {"status": "invalid_api_key"}}, True, "API key"),
        (429, {"detail": {"status": "too_many_concurrent_requests"}}, False, "limitando"),
        (404, {"detail": {"status": "voice_not_found"}}, True, "voz elegida"),
        (422, {"detail": [{"loc": ["body", "text"], "msg": "too long"}]}, True, "422"),
        (503, {"detail": "busy"}, False, "503"),
    ],
)
def test_elevenlabs_errors_say_whether_retrying_helps(status, body, permanent, message):
    voice = ElevenLabs("key", "eleven_multilingual_v2", client=_client(lambda request: httpx.Response(status, json=body)))
    with pytest.raises(VoiceError, match=message) as error:
        voice.speak("Hola", "voice-1")
    assert error.value.permanent is permanent


def test_a_voice_id_can_not_reach_another_elevenlabs_endpoint():
    paths = []

    def handler(request):
        paths.append(request.url.raw_path.decode())
        return httpx.Response(200, json={"audio_base64": "", "alignment": None})

    ElevenLabs("key", "eleven_multilingual_v2", client=_client(handler)).speak("Hola", "../../v1/user")
    assert paths[0].startswith("/v1/text-to-speech/..%2F..%2Fv1%2Fuser/with-timestamps")
