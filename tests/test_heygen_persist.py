import logging
import shutil
import subprocess

import httpx
import pytest
from sqlalchemy import update

from app.db.models import Course, Module
from app.services import heygen_persist
from app.services.heygen_persist import PersistConfig, persist_all_heygen_videos

CONFIG = PersistConfig(heygen_key="hg-key", supabase_url="https://sb.test", supabase_key="sb-key")
PUBLIC_PREFIX = "https://sb.test/storage/v1/object/public/course-videos/modules"
SIGNED_QUERY = "Expires=999&Signature=secret-signature"


class FakeServices:
    """Stands in for HeyGen (status + file download) and Supabase Storage."""

    def __init__(self, statuses=None, upload_responses=None, bucket_public=True, existing=None, on_download=None):
        self.statuses = statuses or {}
        self.upload_responses = list(upload_responses or [])
        self.bucket_public = bucket_public
        self.stored: dict[str, bytes] = dict(existing or {})
        self.status_calls: list[str] = []
        self.downloads = 0
        self.on_download = on_download

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.url.host == "api.heygen.com":
            video_id = request.url.params["video_id"]
            self.status_calls.append(video_id)
            assert request.headers["X-Api-Key"] == "hg-key"
            status = self.statuses[video_id]
            if isinstance(status, httpx.Response):
                return status
            return httpx.Response(200, json={"code": 100, "data": status})
        if request.url.host == "files.heygen.test":
            self.downloads += 1
            if self.on_download:
                self.on_download()
            return httpx.Response(200, content=b"video-bytes-" + path.encode())
        if path == "/storage/v1/bucket":
            return httpx.Response(400, json={"error": "Duplicate", "message": "already exists"})
        if path == "/storage/v1/bucket/course-videos":
            return httpx.Response(200, json={"id": "course-videos", "public": self.bucket_public})
        if path.startswith("/storage/v1/object/public/course-videos/"):
            key = path.removeprefix("/storage/v1/object/public/course-videos/")
            if key not in self.stored:
                return httpx.Response(400, json={"error": "not_found"})
            return httpx.Response(200, headers={"content-length": str(len(self.stored[key]))})
        if path.startswith("/storage/v1/object/course-videos/"):
            assert request.headers["apikey"] == "sb-key"
            if self.upload_responses:
                status = self.upload_responses.pop(0)
                if status != 200:
                    body = {"statusCode": "413", "error": "Payload too large"} if status in (400, 413) else {"error": "boom"}
                    return httpx.Response(status, json=body)
            self.stored[path.removeprefix("/storage/v1/object/course-videos/")] = request.read()
            return httpx.Response(200, json={"Key": path})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


def _module(db, video_url: str, status: str = "generating") -> Module:
    course = Course(title="Curso", status="published")
    db.add(course)
    db.flush()
    module = Module(course_id=course.id, title="M1", order=1, video_url=video_url, generation_status=status)
    db.add(module)
    db.commit()
    return module


def _completed(video_id: str) -> dict:
    return {"status": "completed", "video_url": f"https://files.heygen.test/{video_id}.mp4?{SIGNED_QUERY}"}


def test_completed_video_is_copied_to_storage(db):
    module = _module(db, "heygen://video/abc")
    services = FakeServices({"abc": _completed("abc")})

    assert persist_all_heygen_videos(db, CONFIG, services.client()) == {"persisted": 1}

    db.refresh(module)
    assert module.video_url == f"{PUBLIC_PREFIX}/{module.id}/abc.mp4"
    assert module.generation_status == "completed"
    assert services.stored[f"modules/{module.id}/abc.mp4"] == b"video-bytes-/abc.mp4"


def test_pending_video_that_finished_is_copied(db):
    module = _module(db, "heygen://pending/p1")
    services = FakeServices({"p1": _completed("p1")})

    assert persist_all_heygen_videos(db, CONFIG, services.client()) == {"persisted": 1}
    db.refresh(module)
    assert module.video_url.startswith(PUBLIC_PREFIX)


def test_already_copied_video_is_reused_without_downloading(db):
    module = _module(db, "heygen://video/abc")
    services = FakeServices(existing={f"modules/{module.id}/abc.mp4": b"already-there"})

    assert persist_all_heygen_videos(db, CONFIG, services.client()) == {"persisted": 1}
    assert services.status_calls == []
    assert services.downloads == 0
    db.refresh(module)
    assert module.video_url == f"{PUBLIC_PREFIX}/{module.id}/abc.mp4"


def test_regenerate_during_copy_is_not_overwritten(db):
    module = _module(db, "heygen://video/old", status="completed")

    def admin_regenerates():
        db.execute(
            update(Module).where(Module.id == module.id).values(video_url="heygen://pending/new", generation_status="generating")
        )
        db.commit()

    services = FakeServices({"old": _completed("old")}, on_download=admin_regenerates)

    assert persist_all_heygen_videos(db, CONFIG, services.client()) == {"superseded": 1}
    db.expire_all()
    stored = db.get(Module, module.id)
    assert stored.video_url == "heygen://pending/new"
    assert stored.generation_status == "generating"


def test_failed_pending_render_marks_module_failed(db):
    module = _module(db, "heygen://pending/bad")
    services = FakeServices({"bad": {"status": "failed", "error": {"message": "boom"}}})

    assert persist_all_heygen_videos(db, CONFIG, services.client()) == {"failed": 1}
    db.refresh(module)
    assert module.video_url == ""
    assert module.generation_status == "failed"


def test_failed_status_keeps_a_stored_reference(db):
    module = _module(db, "heygen://video/kept", status="completed")
    services = FakeServices({"kept": {"status": "failed"}})

    assert persist_all_heygen_videos(db, CONFIG, services.client()) == {"failed": 1}
    db.refresh(module)
    assert module.video_url == "heygen://video/kept"


def test_video_missing_on_heygen_is_reported_and_kept(db):
    module = _module(db, "heygen://video/gone")
    services = FakeServices({"gone": httpx.Response(200, json={"code": 404, "data": None, "message": "not found"})})

    assert persist_all_heygen_videos(db, CONFIG, services.client()) == {"unavailable": 1}
    db.refresh(module)
    assert module.video_url == "heygen://video/gone"


def test_heygen_server_error_is_retried_later(db):
    module = _module(db, "heygen://video/abc")
    services = FakeServices({"abc": httpx.Response(500, json={"error": "down"})})

    assert persist_all_heygen_videos(db, CONFIG, services.client()) == {"error": 1}
    db.refresh(module)
    assert module.video_url == "heygen://video/abc"


def test_video_still_processing_is_left_untouched(db):
    module = _module(db, "heygen://pending/slow")
    services = FakeServices({"slow": {"status": "processing"}})

    assert persist_all_heygen_videos(db, CONFIG, services.client()) == {"processing": 1}
    db.refresh(module)
    assert module.video_url == "heygen://pending/slow"
    assert services.stored == {}


def test_modules_not_on_heygen_are_ignored(db):
    _module(db, "https://example.com/video.mp4")
    _module(db, "")
    services = FakeServices()

    assert persist_all_heygen_videos(db, CONFIG, services.client()) == {}
    assert services.status_calls == []


def test_invalid_heygen_reference_is_skipped(db):
    _module(db, "heygen://video/../../etc")
    services = FakeServices()

    assert persist_all_heygen_videos(db, CONFIG, services.client()) == {"skipped": 1}


def test_private_existing_bucket_aborts_without_touching_modules(db):
    module = _module(db, "heygen://video/abc")
    services = FakeServices({"abc": _completed("abc")}, bucket_public=False)

    assert persist_all_heygen_videos(db, CONFIG, services.client()) == {}
    db.refresh(module)
    assert module.video_url == "heygen://video/abc"
    assert services.downloads == 0


@pytest.mark.parametrize("rejection", [413, 400])
def test_size_rejection_is_compressed_and_retried(db, monkeypatch, rejection):
    module = _module(db, "heygen://video/big")
    services = FakeServices({"big": _completed("big")}, upload_responses=[rejection, 200])
    monkeypatch.setattr(heygen_persist, "_compress", lambda src, dest: dest.write_bytes(b"small"))

    assert persist_all_heygen_videos(db, CONFIG, services.client()) == {"persisted": 1}
    assert services.stored[f"modules/{module.id}/big.mp4"] == b"small"


def test_large_original_is_compressed_before_uploading(db, monkeypatch):
    module = _module(db, "heygen://video/big")
    services = FakeServices({"big": _completed("big")})
    monkeypatch.setattr(heygen_persist, "MAX_UPLOAD_BYTES", 5)
    monkeypatch.setattr(heygen_persist, "_compress", lambda src, dest: dest.write_bytes(b"tiny"))

    assert persist_all_heygen_videos(db, CONFIG, services.client()) == {"persisted": 1}
    assert services.stored[f"modules/{module.id}/big.mp4"] == b"tiny"


def test_still_too_large_after_compression_keeps_reference(db, monkeypatch):
    module = _module(db, "heygen://video/huge")
    services = FakeServices({"huge": _completed("huge")}, upload_responses=[413, 413])
    monkeypatch.setattr(heygen_persist, "_compress", lambda src, dest: dest.write_bytes(b"still-big"))

    assert persist_all_heygen_videos(db, CONFIG, services.client()) == {"error": 1}
    db.refresh(module)
    assert module.video_url == "heygen://video/huge"


def test_non_size_upload_error_does_not_compress(db, monkeypatch):
    module = _module(db, "heygen://video/abc")
    services = FakeServices({"abc": _completed("abc")}, upload_responses=[500])

    def fail_compress(src, dest):
        raise AssertionError("must not compress on a non-size error")

    monkeypatch.setattr(heygen_persist, "_compress", fail_compress)

    assert persist_all_heygen_videos(db, CONFIG, services.client()) == {"error": 1}
    db.refresh(module)
    assert module.video_url == "heygen://video/abc"


def test_download_error_keeps_reference_and_hides_signed_url(db, caplog):
    module = _module(db, "heygen://video/gone")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.heygen.com":
            return httpx.Response(200, json={"data": _completed("gone")})
        if request.url.host == "files.heygen.test":
            return httpx.Response(403)
        if request.url.path == "/storage/v1/bucket/course-videos":
            return httpx.Response(200, json={"public": True})
        return httpx.Response(400, json={})

    with caplog.at_level(logging.INFO):
        outcomes = persist_all_heygen_videos(db, CONFIG, httpx.Client(transport=httpx.MockTransport(handler)))

    assert outcomes == {"error": 1}
    db.refresh(module)
    assert module.video_url == "heygen://video/gone"
    ours = [record for record in caplog.records if record.name.startswith("app.")]
    logged = [str(value) for record in ours for value in vars(record).values()]
    assert ours
    assert not any("Signature=" in value or "hg-key" in value or "sb-key" in value for value in logged)


def test_logging_setup_silences_httpx_request_urls():
    from app.core.logging import configure_logging

    configure_logging()

    assert logging.getLogger("httpx").getEffectiveLevel() == logging.WARNING


def test_config_repr_hides_keys():
    assert "hg-key" not in repr(CONFIG)
    assert "sb-key" not in repr(CONFIG)


def test_missing_configuration_is_a_no_op(db, monkeypatch):
    _module(db, "heygen://video/abc")
    monkeypatch.delenv("HEYGEN_API_KEY", raising=False)

    assert persist_all_heygen_videos(db) == {}


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("heygen://video/abc", "abc"),
        ("heygen://pending/xyz_9-A", "xyz_9-A"),
        ("heygen://video/", None),
        ("heygen://video/a/b", None),
        ("https://x/y.mp4", None),
        (None, None),
    ],
)
def test_heygen_video_id(url, expected):
    assert heygen_persist.heygen_video_id(url) == expected


def test_background_run_releases_lock_after_a_crash(monkeypatch):
    def broken_session():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(heygen_persist, "SessionLocal", broken_session)

    heygen_persist._run()

    assert not heygen_persist._run_lock.locked()


def test_trigger_during_a_run_causes_one_more_pass(monkeypatch):
    calls = []

    def fake_persist(db):
        calls.append(db)
        if len(calls) == 1:
            heygen_persist._run()  # a status endpoint fires while this pass is running

    class FakeSession:
        def __enter__(self):
            return "session"

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(heygen_persist, "SessionLocal", FakeSession)
    monkeypatch.setattr(heygen_persist, "persist_all_heygen_videos", fake_persist)

    heygen_persist._run()

    assert calls == ["session", "session"]
    assert not heygen_persist._run_lock.locked()


def test_status_endpoint_triggers_a_copy_when_heygen_finishes(db, monkeypatch):
    import asyncio

    from app.api.routes import course_wizard

    module = _module(db, "heygen://pending/done")
    triggered = []

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": {"status": "completed", "video_url": "https://files.heygen.test/done.mp4"}}

    monkeypatch.setenv("HEYGEN_API_KEY", "hg-key")
    monkeypatch.setattr("httpx.get", lambda *args, **kwargs: FakeResponse())
    monkeypatch.setattr(course_wizard, "start_background_persist", lambda: triggered.append(True))

    result = asyncio.run(course_wizard.check_video_status(module.id, db))

    assert result["status"] == "completed"
    assert triggered == [True]


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_compress_produces_720p_h264_within_the_size_cap(tmp_path):
    src = tmp_path / "src.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30:duration=2",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-c:v", "libx264", "-crf", "10", "-c:a", "aac", "-shortest", str(src),
        ],
        check=True,
        capture_output=True,
    )
    dest = tmp_path / "dest.mp4"

    heygen_persist._compress(src, dest, max_bytes=400_000)

    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name,height",
         "-of", "csv=p=0", str(dest)],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert probe == "h264,720"
    assert dest.stat().st_size < src.stat().st_size
