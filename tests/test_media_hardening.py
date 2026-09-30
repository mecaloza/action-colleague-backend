"""Uploads and processing under hostile or unusual input: Supabase contract, replays, big or odd files."""

import subprocess

import httpx
import pytest
from PIL import Image
from pypdf import PdfWriter

from app.db.models import Course, Job, MediaAsset, Module
from app.services import media as media_service
from app.services import media_processing as mp
from app.services.storage import LocalStorage, StorageError, SupabaseStorage
from app.worker import queue, runner
from tests.conftest import auth_headers
from tests.test_media import _make_video, _upload


@pytest.fixture
def admin_headers(client, admin):
    return auth_headers(client, admin.email)


@pytest.fixture
def module(db, admin):
    course = Course(title="Seguridad", status="draft", source="manual", settings={}, created_by=admin.id)
    db.add(course)
    db.flush()
    module = Module(course_id=course.id, title="Casco", order=1, content_text="", source="text")
    db.add(module)
    db.commit()
    return module


def _complete(client, headers, asset_id, **body):
    return client.post(f"/api/v1/media/{asset_id}/complete", headers=headers, json=body)


def _job(db, asset_id):
    db.expire_all()
    return db.query(Job).filter(Job.dedupe_key == f"media:{asset_id}").one()


def _fake_supabase(url="https://abcd.supabase.co"):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/bucket/course-media"):
            return httpx.Response(200, json={"id": "course-media", "public": False})
        if "/object/upload/sign/" in request.url.path:
            obj = request.url.path.split("/object/upload/sign/", 1)[1]
            return httpx.Response(200, json={"url": f"/object/upload/sign/{obj}?token=tok", "token": "tok"})
        return httpx.Response(200, json={})

    storage = SupabaseStorage(url, "sb_secret_x", "course-media", client=httpx.Client(transport=httpx.MockTransport(handler)))
    return storage, seen


@pytest.mark.parametrize(("url", "endpoint"), [
    ("https://abcd.supabase.co", "https://abcd.storage.supabase.co/storage/v1/upload/resumable/sign"),
    ("http://127.0.0.1:54321", "http://127.0.0.1:54321/storage/v1/upload/resumable/sign"),
])
def test_tus_target_uses_the_signed_route_and_no_upsert(url, endpoint):
    storage, seen = _fake_supabase(url)
    target = storage.upload_target("courses/1/a/clase.webm", "video/webm", 50 * 1024 * 1024)
    assert target.method == "TUS" and target.url == endpoint and target.headers == {"x-signature": "tok"}
    assert target.metadata["objectName"] == "courses/1/a/clase.webm" and target.chunk_size == 6 * 1024 * 1024
    sign = next(r for r in seen if "/object/upload/sign/" in r.url.path)
    assert "x-upsert" not in sign.headers and "authorization" not in sign.headers and sign.headers["apikey"] == "sb_secret_x"


def test_small_target_is_a_signed_put_without_upsert():
    storage, _ = _fake_supabase()
    target = storage.upload_target("library/a/doc.pdf", "application/pdf", 1000)
    assert target.method == "PUT" and target.url == "https://abcd.supabase.co/storage/v1/object/upload/sign/course-media/library/a/doc.pdf?token=tok"
    assert target.headers == {"content-type": "application/pdf"}


def test_poster_never_overwrites_an_upload_named_like_it(client, db, admin_headers, module, storage, worker, tmp_path):
    named = tmp_path / "poster.jpg"
    named.write_bytes(_make_video(tmp_path / "base.mp4", 640, 360).read_bytes())
    asset = _upload(client, admin_headers, named, "video/mp4", "video", module.course_id)
    _complete(client, admin_headers, asset["id"], module_id=module.id, purpose="module_video")
    worker()
    db.expire_all()
    stored = db.get(MediaAsset, asset["id"])
    assert storage.file_path(stored.path).read_bytes()[4:8] == b"ftyp" and stored.meta["poster_asset_id"] != stored.id


def test_h264_444_is_transcoded(client, db, admin_headers, module, storage, worker, tmp_path):
    src = tmp_path / "screen.mp4"
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=30",
                    "-t", "1", "-c:v", "libx264", "-pix_fmt", "yuv444p", str(src)], check=True)
    asset = _upload(client, admin_headers, src, "video/mp4", "video", module.course_id)
    _complete(client, admin_headers, asset["id"], module_id=module.id, purpose="module_video")
    worker()
    db.expire_all()
    stored = db.get(MediaAsset, asset["id"])
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=pix_fmt",
                          "-of", "csv=p=0", str(storage.file_path(stored.path))], capture_output=True, text=True).stdout.strip()
    assert stored.path.endswith("/video.mp4") and out == "yuv420p"


def test_audio_fails_at_once(client, db, admin_headers, storage, worker, tmp_path):
    audio = tmp_path / "voz.mp3"
    audio.write_bytes(b"ID3" + b"\0" * 5000)
    asset = _upload(client, admin_headers, audio, "audio/mpeg", "audio", None)
    _complete(client, admin_headers, asset["id"])
    worker()
    assert _job(db, asset["id"]).status == "failed"


def test_cleanup_failure_does_not_fail_the_job(client, db, admin_headers, module, storage, worker, tmp_path, monkeypatch):
    source = _make_video(tmp_path / "clase.webm", 640, 360)
    asset = _upload(client, admin_headers, source, "video/webm", "video", module.course_id)
    _complete(client, admin_headers, asset["id"], module_id=module.id, purpose="module_video")

    def broken_delete(self, paths):
        raise StorageError("Storage no pudo borrar archivos (503)")

    monkeypatch.setattr(LocalStorage, "delete", broken_delete)
    worker()
    assert _job(db, asset["id"]).status == "succeeded"
    assert client.get(f"/api/v1/courses/{module.course_id}", headers=admin_headers).json()["modules"][0]["video"]


def test_bomb_image_fails_permanently_with_a_clear_message(client, db, admin_headers, module, storage, worker, tmp_path):
    path = tmp_path / "bomba.png"
    Image.new("1", (20000, 10000)).save(path, optimize=True)
    asset = _upload(client, admin_headers, path, "image/png", "image", module.course_id)
    _complete(client, admin_headers, asset["id"], purpose="course_cover")
    worker()
    job = _job(db, asset["id"])
    assert job.status == "failed" and "imagen" in job.error


def test_local_upload_cannot_replace_a_checked_file(client, db, admin_headers, storage):
    created = client.post("/api/v1/media/uploads", headers=admin_headers,
                          json={"filename": "a.txt", "mime_type": "text/plain", "size_bytes": 4, "kind": "document"}).json()
    url = created["upload"]["url"].replace("http://testserver", "")
    assert client.put(url, content=b"hola").status_code == 200
    assert _complete(client, admin_headers, created["asset"]["id"]).status_code == 200
    assert client.put(url, content=b"x" * 5_000_000).status_code == 409


def test_encoder_timeout_fails_at_once_with_a_clear_message(client, db, admin_headers, module, storage, worker, tmp_path, monkeypatch):
    from app.services import media_processing as mp
    source = _make_video(tmp_path / "largo.webm", 1280, 720, seconds=4)
    asset = _upload(client, admin_headers, source, "video/webm", "video", module.course_id)
    _complete(client, admin_headers, asset["id"], module_id=module.id, purpose="module_video")
    real_run = mp.run
    monkeypatch.setattr(mp, "run", lambda cmd, timeout=mp.ENCODE_TIMEOUT_SECONDS: real_run(cmd, 0.05 if "libx264" in cmd else timeout))
    worker()
    job = _job(db, asset["id"])
    assert job.status == "failed" and "tardó demasiado" in job.error


def test_a_network_error_while_signing_hides_media_only(monkeypatch):
    def down(request):
        raise httpx.ConnectError("storage unreachable")

    storage = SupabaseStorage("https://abcd.supabase.co", "sb_secret_x", "course-media",
                              client=httpx.Client(transport=httpx.MockTransport(down)))
    monkeypatch.setattr(media_service, "get_storage", lambda: storage)
    assert media_service.sign_paths(["courses/1/a/video.mp4"], 60) == {}


def test_workers_only_claim_the_types_they_run(session_factory, db, monkeypatch):
    from app.db import session as db_session

    monkeypatch.setattr(db_session, "SessionLocal", session_factory)
    monkeypatch.setattr(runner, "_handlers", {"media.process": lambda ctx: {}})
    job = queue.enqueue(db, "course.generate")
    assert runner.run_pending(worker_id="old-container") == 0
    db.expire_all()
    assert db.get(Job, job.id).status == "queued" and db.get(Job, job.id).attempts == 0


def test_protected_or_corrupt_decks_fail_at_once_with_a_clear_message(client, db, admin, module, storage, worker, tmp_path):
    headers = auth_headers(client, admin.email)
    locked = tmp_path / "clave.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=960, height=540)
    writer.encrypt("secreta")
    writer.write(locked)
    corrupt = tmp_path / "roto.pdf"
    corrupt.write_bytes(b"%PDF-1.7\n" + b"\x00" * 3000)
    for path in (locked, corrupt):
        asset = _upload(client, headers, path, "application/pdf", "deck", module.course_id)
        client.post(f"/api/v1/media/{asset['id']}/complete", headers=headers, json={"purpose": "deck"})
        worker()
        db.expire_all()
        job = db.query(Job).filter(Job.dedupe_key == f"media:{asset['id']}").one()
        assert job.status == "failed" and "presentación" in job.error


def _encode(path, *args: str) -> None:
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", *args, str(path)], check=True, capture_output=True)


def _stream(path, entries: str) -> str:
    return subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", f"stream={entries}", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def test_hdr_phone_videos_come_out_as_sdr(client, db, admin_headers, module, storage, worker, tmp_path):
    source = tmp_path / "iphone.mp4"  # HLG-tagged, as phones record by default
    _encode(source, "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=30", "-t", "1",
            "-vf", "setparams=color_primaries=bt2020:color_trc=arib-std-b67:colorspace=bt2020nc",
            "-c:v", "libx264", "-pix_fmt", "yuv420p")
    asset = _upload(client, admin_headers, source, "video/mp4", "video", module.course_id)
    _complete(client, admin_headers, asset["id"], module_id=module.id, purpose="module_video")
    worker()

    db.expire_all()
    stored = db.get(MediaAsset, asset["id"])
    assert stored.path.endswith("/derived/video.mp4")
    assert _stream(storage.file_path(stored.path), "color_transfer") == "bt709"


@pytest.mark.parametrize(("rate", "expected"), [(25, "25/1"), (60, "30/1")])
def test_frame_rate_is_capped_not_forced(client, db, admin_headers, module, storage, worker, tmp_path, rate, expected):
    source = tmp_path / f"clase-{rate}.webm"
    _encode(source, "-f", "lavfi", "-i", f"testsrc=size=640x360:rate={rate}", "-t", "1", "-c:v", "libvpx", "-b:v", "300k")
    asset = _upload(client, admin_headers, source, "video/webm", "video", module.course_id)
    _complete(client, admin_headers, asset["id"], module_id=module.id, purpose="module_video")
    worker()

    db.expire_all()
    assert _stream(storage.file_path(db.get(MediaAsset, asset["id"]).path), "avg_frame_rate") == expected


def test_a_new_video_deletes_the_one_it_replaces(client, db, admin_headers, module, storage, worker, tmp_path):
    first = _upload(client, admin_headers, _make_video(tmp_path / "uno.mp4", 640, 360), "video/mp4", "video", module.course_id)
    _complete(client, admin_headers, first["id"], module_id=module.id, purpose="module_video")
    worker()
    db.expire_all()
    old = db.get(MediaAsset, first["id"])
    old_files = [storage.file_path(old.path), storage.file_path(db.get(MediaAsset, old.meta["poster_asset_id"]).path)]
    old_poster = old.meta["poster_asset_id"]

    second = _upload(client, admin_headers, _make_video(tmp_path / "dos.mp4", 640, 360), "video/mp4", "video", module.course_id)
    _complete(client, admin_headers, second["id"], module_id=module.id, purpose="module_video")
    worker()

    db.expire_all()
    assert db.get(MediaAsset, first["id"]) is None and db.get(MediaAsset, old_poster) is None
    assert not any(path.exists() for path in old_files)
    assert db.get(Module, module.id).video_asset_id == second["id"]


def test_a_cover_must_belong_to_the_course(client, db, admin_headers, module, storage, worker, tmp_path):
    other = client.post("/api/v1/courses", headers=admin_headers, json={"title": "Otro"}).json()
    image = tmp_path / "portada.png"
    Image.new("RGB", (800, 450), "orange").save(image)
    asset = _upload(client, admin_headers, image, "image/png", "image", other["id"])
    _complete(client, admin_headers, asset["id"], purpose="course_material")
    worker()

    response = client.put(f"/api/v1/courses/{module.course_id}/cover", headers=admin_headers, json={"asset_id": asset["id"]})

    assert response.status_code == 422


@pytest.mark.parametrize(("name", "stored"), [
    ("图片.png", "archivo.png"),
    ("Clase 1 (final).MP4", "Clase-1-final.MP4"),
    ("sin extension", "sin-extension"),
])
def test_storage_names_keep_the_extension(name, stored):
    from app.api.routes.media import _safe_filename

    assert _safe_filename(name) == stored


def test_an_upload_in_progress_can_be_signed_again(client, admin_headers, storage):
    created = client.post(
        "/api/v1/media/uploads", headers=admin_headers,
        json={"filename": "clase.mp4", "mime_type": "video/mp4", "size_bytes": 4, "kind": "video"},
    ).json()
    asset_id = created["asset"]["id"]

    renewed = client.post(f"/api/v1/media/{asset_id}/upload-target", headers=admin_headers)

    assert renewed.status_code == 200 and renewed.json()["asset"]["id"] == asset_id
    assert client.put(renewed.json()["upload"]["url"].replace("http://testserver", ""), content=b"hola").status_code == 200
    _complete(client, admin_headers, asset_id)
    assert client.post(f"/api/v1/media/{asset_id}/upload-target", headers=admin_headers).status_code == 409


def test_shutting_down_hands_running_jobs_back_without_counting_an_attempt(session_factory, db, monkeypatch):
    from app.db import session as db_session

    monkeypatch.setattr(db_session, "SessionLocal", session_factory)
    job = queue.enqueue(db, "media.process", {"asset_id": "x"})
    claimed = queue.claim(db, "host:1:0", lease_seconds=60)
    pool = runner.WorkerPool(concurrency=1)
    pool._running[claimed.id] = "host:1:0"

    pool.stop()

    db.expire_all()
    handed_back = db.get(Job, job.id)
    assert handed_back.status == "queued" and handed_back.attempts == 0 and handed_back.locked_by is None
    # The handler finishing later can't record anything: the lease is gone.
    assert queue.succeed(db, job.id, "host:1:0", {}) is False


def test_deleting_a_module_or_a_course_deletes_its_media(client, db, admin_headers, module, storage, worker, tmp_path):
    course_id, module_id = module.course_id, module.id
    video = _upload(client, admin_headers, _make_video(tmp_path / "clase.mp4", 640, 360), "video/mp4", "video", course_id)
    _complete(client, admin_headers, video["id"], module_id=module_id, purpose="module_video")
    material = tmp_path / "guia.txt"
    material.write_text("Guía del curso")
    guide = _upload(client, admin_headers, material, "text/plain", "document", course_id)
    _complete(client, admin_headers, guide["id"], purpose="course_material")
    worker()
    db.expire_all()
    files = [storage.file_path(asset.path) for asset in db.query(MediaAsset).all()]
    assert len(files) == 4 and all(path.exists() for path in files)  # video, poster, captions and the guide

    client.post(f"/api/v1/courses/{course_id}/modules", headers=admin_headers, json={"title": "Otro"})
    assert client.delete(f"/api/v1/modules/{module_id}", headers=admin_headers).status_code == 204
    db.expire_all()
    assert [asset.id for asset in db.query(MediaAsset).all()] == [guide["id"]]  # the module's video and poster went

    assert client.delete(f"/api/v1/courses/{course_id}", headers=admin_headers).status_code == 204
    db.expire_all()
    assert db.query(MediaAsset).count() == 0 and not any(path.exists() for path in files)
    assert db.query(Job).filter(Job.status != "running").count() == 0


def test_hdr_tagged_only_by_its_transfer_is_named_bt2020_before_tone_mapping(monkeypatch):
    monkeypatch.setattr(mp, "can_tone_map", lambda: True)
    base = {"duration": 1, "width": 1920, "height": 1080, "video_codec": "hevc", "audio_codec": None,
            "pix_fmt": "yuv420p10le"}
    exported = mp.VideoInfo(**base, color_transfer="arib-std-b67", color_primaries="unknown")
    phone = mp.VideoInfo(**base, color_transfer="smpte2084", color_primaries="bt2020", color_space="bt2020nc")

    assert "setparams=color_primaries=bt2020:colorspace=bt2020nc,zscale=t=linear" in mp.web_video_filter(exported)
    assert "setparams=color_primaries=bt2020" not in mp.web_video_filter(phone)


@pytest.mark.skipif(not mp.can_tone_map(), reason="FFmpeg without zscale: only the tagging fallback runs")
def test_hdr_tagged_only_by_its_transfer_is_tone_mapped(tmp_path):
    source = tmp_path / "exportado.mp4"
    _encode(source, "-f", "lavfi", "-i", "testsrc=size=640x360:rate=30", "-t", "1",
            "-vf", "setparams=color_trc=arib-std-b67", "-c:v", "libx264", "-pix_fmt", "yuv420p")

    mp.to_web_mp4(source, tmp_path / "web.mp4", mp.probe(source))

    assert _stream(tmp_path / "web.mp4", "color_transfer") == "bt709"


def test_a_storage_timeout_while_discarding_is_logged_not_raised(
    client, db, admin_headers, module, storage, worker, tmp_path, monkeypatch
):
    guide = tmp_path / "guia.txt"
    guide.write_text("Guía")
    asset = _upload(client, admin_headers, guide, "text/plain", "document", module.course_id)
    _complete(client, admin_headers, asset["id"], module_id=module.id, purpose="module_document")
    worker()

    def timeout(request):
        raise httpx.ReadTimeout("timed out", request=request)

    supabase = SupabaseStorage("https://abcd.supabase.co", "sb_secret_x", "course-media",
                               client=httpx.Client(transport=httpx.MockTransport(timeout)))
    monkeypatch.setattr(media_service, "get_storage", lambda: supabase)
    response = client.delete(f"/api/v1/modules/{module.id}/document", headers=admin_headers)

    db.expire_all()
    assert response.status_code == 200 and db.get(MediaAsset, asset["id"]) is None


def test_a_file_two_rows_share_goes_with_the_last_of_them(db, storage, tmp_path):
    frame = tmp_path / "poster.jpg"
    Image.new("RGB", (64, 36)).save(frame)
    path = "courses/1/v/derived/poster.jpg"
    storage.upload_file(path, frame, "image/jpeg")
    first, second = (
        MediaAsset(kind="image", status="ready", bucket=storage.bucket, path=path, mime_type="image/jpeg") for _ in "ab"
    )
    db.add_all([first, second])
    db.commit()

    media_service.discard_assets(db, [first.id])
    assert storage.file_path(path).exists()
    media_service.discard_assets(db, [second.id])
    assert not storage.file_path(path).exists()


def test_a_job_handed_back_at_shutdown_saves_nothing(
    client, db, admin_headers, module, storage, worker, tmp_path, monkeypatch
):
    from app.worker.jobs import media as media_jobs

    source = _make_video(tmp_path / "clase.mp4", 640, 360)
    video = _upload(client, admin_headers, source, "video/mp4", "video", module.course_id)
    _complete(client, admin_headers, video["id"], module_id=module.id, purpose="module_video")
    process = media_jobs._process_file

    def process_while_shutting_down(ctx, asset):
        changes = process(ctx, asset)
        with runner.open_session() as session:  # WorkerPool.stop() hands the job back meanwhile
            queue.requeue(session, ctx.job_id, ctx.worker_id)
        return changes

    monkeypatch.setattr(media_jobs, "_process_file", process_while_shutting_down)
    worker(max_jobs=1, worker_id="old-container:1:0")
    db.expire_all()
    assert db.get(Module, module.id).video_asset_id is None
    assert (_job(db, video["id"]).status, _job(db, video["id"]).attempts) == ("queued", 0)

    monkeypatch.setattr(media_jobs, "_process_file", process)
    worker(worker_id="new-container:1:0")
    db.expire_all()
    assert db.get(Module, module.id).video_asset_id == video["id"]
    assert _job(db, video["id"]).result == {"asset_id": video["id"]}


def test_deleting_the_course_while_its_video_processes_leaves_nothing_behind(
    client, db, admin_headers, module, storage, worker, tmp_path, monkeypatch
):
    source = _make_video(tmp_path / "clase.webm", 640, 360)
    video = _upload(client, admin_headers, source, "video/webm", "video", module.course_id)
    _complete(client, admin_headers, video["id"], module_id=module.id, purpose="module_video")
    grab_poster = mp.poster

    def poster_then_delete_the_course(*args, **kwargs):
        grab_poster(*args, **kwargs)
        assert client.delete(f"/api/v1/courses/{module.course_id}", headers=admin_headers).status_code == 204

    monkeypatch.setattr(mp, "poster", poster_then_delete_the_course)
    worker()

    db.expire_all()
    assert db.query(MediaAsset).count() == 0  # the poster row made after the delete went too
    assert not [path for path in storage.root.rglob("*") if path.is_file()]  # and the transcoded copy


def test_deleting_a_module_discards_its_queued_upload_not_the_courses_materials(
    client, db, admin_headers, module, storage, worker, tmp_path
):
    for name in ("guia.txt", "anexo.txt"):
        (tmp_path / name).write_text(name)
    guide = _upload(client, admin_headers, tmp_path / "guia.txt", "text/plain", "document", module.course_id)
    _complete(client, admin_headers, guide["id"], module_id=module.id, purpose="module_document")
    annex = _upload(client, admin_headers, tmp_path / "anexo.txt", "text/plain", "document", module.course_id)
    _complete(client, admin_headers, annex["id"], module_id=module.id, purpose="course_material")
    guide_file = storage.file_path(db.get(MediaAsset, guide["id"]).path)

    assert client.delete(f"/api/v1/modules/{module.id}", headers=admin_headers).status_code == 204
    worker()

    materials = client.get(f"/api/v1/courses/{module.course_id}/materials", headers=admin_headers).json()
    assert [(material["id"], material["status"]) for material in materials] == [(annex["id"], "ready")]
    assert not guide_file.exists()


def test_a_job_claimed_while_stopping_is_handed_back_without_running(session_factory, db, monkeypatch):
    from app.db import session as db_session

    monkeypatch.setattr(db_session, "SessionLocal", session_factory)
    ran = []
    monkeypatch.setattr(runner, "_handlers", {"demo": lambda ctx: ran.append(ctx.job_id)})
    job = queue.enqueue(db, "demo")
    pool = runner.WorkerPool(concurrency=1)
    claim = runner._claim_next

    def claim_as_the_deploy_stops_us(worker_id):
        claimed = claim(worker_id)
        pool.stop()  # SIGTERM right after the claim, before the loop registers the job
        return claimed

    monkeypatch.setattr(runner, "_claim_next", claim_as_the_deploy_stops_us)
    pool._loop(1)

    db.expire_all()
    stored = db.get(Job, job.id)
    assert ran == [] and (stored.status, stored.attempts, stored.locked_by) == ("queued", 0, None)
