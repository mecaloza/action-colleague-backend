"""Direct uploads, processing (real FFmpeg) and attaching media to modules and courses."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from app.db.models import Course, MediaAsset, Module
from tests.conftest import auth_headers

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is not installed")


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


def _make_video(path: Path, width: int, height: int, seconds: float = 1.5, audio: bool = True) -> Path:
    cmd = ["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i", f"testsrc=size={width}x{height}:rate=25"]
    if audio:
        cmd += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000"]
    cmd += ["-t", str(seconds)]
    cmd += ["-c:v", "libvpx", "-b:v", "300k"] if path.suffix == ".webm" else ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
    if audio:
        cmd += ["-c:a", "libopus" if path.suffix == ".webm" else "aac"]
    subprocess.run([*cmd, str(path)], check=True, capture_output=True)
    return path


def _probe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        check=True, capture_output=True,
    ).stdout
    return json.loads(out)


def _upload(client, headers, file: Path, mime: str, kind: str, course_id: int | None) -> dict:
    created = client.post(
        "/api/v1/media/uploads",
        headers=headers,
        json={"filename": file.name, "mime_type": mime, "size_bytes": file.stat().st_size, "kind": kind, "course_id": course_id},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    target = body["upload"]
    assert target["method"] == "PUT"
    put = client.put(target["url"].replace("http://testserver", ""), content=file.read_bytes(), headers=target["headers"])
    assert put.status_code == 200, put.text
    return body["asset"]


def _download(client, url: str, dest: Path) -> Path:
    response = client.get(url.replace("http://testserver", ""))
    assert response.status_code == 200
    dest.write_bytes(response.content)
    return dest


@needs_ffmpeg
@pytest.mark.parametrize(
    ("size", "expected"),
    [
        ((640, 480), (640, 480)),  # 4:3 camera: never stretched to 16:9
        ((360, 640), (360, 640)),  # vertical phone video
        ((720, 1280), (720, 1280)),  # portrait HD keeps its resolution (portrait box is 1080x1920)
        ((2560, 1440), (1920, 1080)),  # bigger than 1080p: scaled down, same aspect ratio
    ],
)
def test_uploaded_video_becomes_the_modules_web_video(
    client, db, admin_headers, module, storage, worker, tmp_path, size, expected
):
    source = _make_video(tmp_path / "clase.webm", *size)
    asset = _upload(client, admin_headers, source, "video/webm", "video", module.course_id)

    done = client.post(
        f"/api/v1/media/{asset['id']}/complete", headers=admin_headers,
        json={"module_id": module.id, "purpose": "module_video"},
    )
    assert done.status_code == 200 and done.json()["status"] == "uploaded"
    assert client.get(f"/api/v1/courses/{module.course_id}", headers=admin_headers).json()["modules"][0]["generation_status"] == "queued"

    assert worker() >= 1  # processing (then the captions job)

    course = client.get(f"/api/v1/courses/{module.course_id}", headers=admin_headers).json()
    [detail] = course["modules"]
    assert detail["source"] == "upload" and detail["generation_status"] == "completed"
    assert detail["video"]["mime_type"] == "video/mp4" and detail["poster_url"]
    assert 1.3 < detail["duration_seconds"] < 1.8
    info = _probe(_download(client, detail["video"]["url"], tmp_path / "out.mp4"))
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    audio = next(s for s in info["streams"] if s["codec_type"] == "audio")
    assert (video["codec_name"], audio["codec_name"]) == ("h264", "aac")
    assert (video["width"], video["height"]) == expected
    # The original upload was replaced by the web version.
    db.expire_all()
    stored = db.get(MediaAsset, asset["id"])
    assert stored.path.endswith("/video.mp4") and not storage.file_path(f"{Path(stored.path).parent}/clase.webm").exists()


@needs_ffmpeg
def test_the_module_shows_its_video_is_being_processed(
    client, db, admin_headers, module, storage, worker, tmp_path, monkeypatch
):
    from app.worker.jobs import media as media_job

    seen = []
    process_file = media_job._process_file

    def spy(ctx, asset):
        db.expire_all()
        seen.append(db.get(Module, module.id).generation_status)
        return process_file(ctx, asset)

    monkeypatch.setattr(media_job, "_process_file", spy)
    asset = _upload(client, admin_headers, _make_video(tmp_path / "clase.mp4", 640, 360), "video/mp4", "video", module.course_id)
    client.post(f"/api/v1/media/{asset['id']}/complete", headers=admin_headers, json={"module_id": module.id, "purpose": "module_video"})

    assert worker() == 1
    assert seen == ["generating"]  # "Procesando" in the editor while FFmpeg runs, not "En cola"
    db.expire_all()
    assert db.get(Module, module.id).generation_status == "completed"


@needs_ffmpeg
def test_video_without_audio_is_accepted(client, admin_headers, module, storage, worker, tmp_path):
    source = _make_video(tmp_path / "mudo.mp4", 640, 360, audio=False)
    asset = _upload(client, admin_headers, source, "video/mp4", "video", module.course_id)
    client.post(f"/api/v1/media/{asset['id']}/complete", headers=admin_headers, json={"module_id": module.id, "purpose": "module_video"})
    worker()
    assert client.get(f"/api/v1/media/{asset['id']}", headers=admin_headers).json()["status"] == "ready"


def test_corrupt_video_fails_with_a_clear_message(client, db, admin_headers, module, storage, worker, tmp_path):
    broken = tmp_path / "roto.mp4"
    broken.write_bytes(b"not a video at all" * 100)
    asset = _upload(client, admin_headers, broken, "video/mp4", "video", module.course_id)
    client.post(f"/api/v1/media/{asset['id']}/complete", headers=admin_headers, json={"module_id": module.id, "purpose": "module_video"})

    worker()  # an unreadable file fails at once: retrying can't fix it

    failed = client.get(f"/api/v1/media/{asset['id']}", headers=admin_headers).json()
    assert failed["status"] == "failed" and "No pudimos leer el archivo" in failed["error"]
    [detail] = client.get(f"/api/v1/courses/{module.course_id}", headers=admin_headers).json()["modules"]
    assert detail["generation_status"] == "failed" and "No pudimos leer" in detail["generation_error"]


def test_document_text_is_extracted_and_attached(client, admin_headers, module, storage, worker, tmp_path):
    import docx

    document = docx.Document()
    document.add_paragraph("Usa siempre el casco en la planta.")
    path = tmp_path / "politica.docx"
    document.save(path)
    mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    asset = _upload(client, admin_headers, path, mime, "document", module.course_id)
    client.post(f"/api/v1/media/{asset['id']}/complete", headers=admin_headers, json={"module_id": module.id, "purpose": "module_document"})

    worker()

    body = client.get(f"/api/v1/media/{asset['id']}", headers=admin_headers).json()
    assert body["status"] == "ready" and body["text_chars"] == len("Usa siempre el casco en la planta.")
    [detail] = client.get(f"/api/v1/courses/{module.course_id}", headers=admin_headers).json()["modules"]
    assert detail["document"]["url"] and detail["source"] == "document"
    materials = client.get(f"/api/v1/courses/{module.course_id}/materials", headers=admin_headers).json()
    assert [m["id"] for m in materials] == [asset["id"]]


def test_pdf_deck_is_rendered_page_by_page(client, admin_headers, module, storage, worker, tmp_path):
    pages = [Image.new("RGB", (1280, 720), color) for color in ("#ff4c01", "#111111")]
    path = tmp_path / "slides.pdf"
    pages[0].save(path, save_all=True, append_images=pages[1:])
    asset = _upload(client, admin_headers, path, "application/pdf", "deck", module.course_id)
    client.post(f"/api/v1/media/{asset['id']}/complete", headers=admin_headers, json={"purpose": "deck"})

    worker()

    body = client.get(f"/api/v1/media/{asset['id']}", headers=admin_headers).json()
    assert body["status"] == "ready" and len(body["pages"]) == 2
    first = client.get(body["pages"][0].replace("http://testserver", ""))
    assert first.status_code == 200 and first.content[:4] == b"\x89PNG"


def test_cover_image_is_resized_and_set(client, admin_headers, module, storage, worker, tmp_path):
    path = tmp_path / "cover.png"
    Image.new("RGB", (3000, 1500), "#ff4c01").save(path)
    asset = _upload(client, admin_headers, path, "image/png", "image", module.course_id)
    client.post(f"/api/v1/media/{asset['id']}/complete", headers=admin_headers, json={"purpose": "course_cover"})

    worker()

    body = client.get(f"/api/v1/media/{asset['id']}", headers=admin_headers).json()
    assert (body["width"], body["height"]) == (1920, 960) and body["mime_type"] == "image/jpeg"
    course = client.get(f"/api/v1/courses/{module.course_id}", headers=admin_headers).json()
    assert course["cover_url"]


def test_upload_validation(client, admin_headers, module, storage):
    def create(**overrides):
        payload = {"filename": "a.mp4", "mime_type": "video/mp4", "size_bytes": 10, "kind": "video", **overrides}
        return client.post("/api/v1/media/uploads", headers=admin_headers, json=payload)

    assert create(mime_type="application/x-msdownload", filename="a.exe").status_code == 422
    assert create(size_bytes=3 * 1024**3).status_code == 413
    assert create(course_id=999).status_code == 404

    asset = create(course_id=module.course_id).json()["asset"]
    early = client.post(f"/api/v1/media/{asset['id']}/complete", headers=admin_headers, json={})
    assert early.status_code == 409  # nothing uploaded yet
    wrong_use = client.post(f"/api/v1/media/{asset['id']}/complete", headers=admin_headers, json={"purpose": "course_cover"})
    assert wrong_use.status_code == 422


def test_media_endpoints_are_admin_only(client, collaborator, storage):
    headers = auth_headers(client, collaborator.email)
    payload = {"filename": "a.mp4", "mime_type": "video/mp4", "size_bytes": 10, "kind": "video"}
    assert client.post("/api/v1/media/uploads", headers=headers, json=payload).status_code == 403
    assert client.get("/api/v1/jobs", headers=headers).status_code == 403


def test_local_links_are_signed_for_one_purpose(client, admin_headers, storage, tmp_path):
    source = tmp_path / "doc.txt"
    source.write_text("hola")
    asset = _upload(client, admin_headers, source, "text/plain", "document", None)
    upload_token_url = client.post(
        "/api/v1/media/uploads", headers=admin_headers,
        json={"filename": "x.txt", "mime_type": "text/plain", "size_bytes": 4, "kind": "document"},
    ).json()["upload"]["url"]
    token = upload_token_url.rsplit("/", 1)[1]
    assert client.get(f"/api/v1/media/local/file/{token}").status_code == 403  # upload token can't download
    assert client.get("/api/v1/media/local/file/not-a-token").status_code == 403
    assert asset["status"] == "pending"


@needs_ffmpeg
def test_removing_a_modules_video_deletes_its_files(client, db, admin_headers, module, storage, worker, tmp_path):
    source = _make_video(tmp_path / "clase.mp4", 640, 360)
    asset = _upload(client, admin_headers, source, "video/mp4", "video", module.course_id)
    client.post(f"/api/v1/media/{asset['id']}/complete", headers=admin_headers, json={"module_id": module.id, "purpose": "module_video"})
    worker()
    stored = db.get(MediaAsset, asset["id"])
    video_file = storage.file_path(stored.path)
    assert video_file.exists()

    response = client.delete(f"/api/v1/modules/{module.id}/video", headers=admin_headers)

    assert response.status_code == 200 and response.json()["video"] is None
    assert not video_file.exists()
    db.expire_all()
    assert db.query(MediaAsset).count() == 0


def test_jobs_can_be_listed_by_module(client, admin_headers, module, storage, tmp_path):
    source = tmp_path / "doc.txt"
    source.write_text("hola")
    asset = _upload(client, admin_headers, source, "text/plain", "document", module.course_id)
    client.post(f"/api/v1/media/{asset['id']}/complete", headers=admin_headers, json={"module_id": module.id, "purpose": "module_document"})

    jobs = client.get("/api/v1/jobs", headers=admin_headers, params={"module_id": module.id, "active": True}).json()

    assert [(job["type"], job["status"]) for job in jobs] == [("media.process", "queued")]
    assert client.get(f"/api/v1/jobs/{jobs[0]['id']}", headers=admin_headers).json()["id"] == jobs[0]["id"]


def test_the_real_size_is_checked_when_the_upload_completes(client, db, admin_headers, module, storage, tmp_path, monkeypatch):
    from app.api.routes import media as media_routes

    doc = tmp_path / "grande.txt"
    doc.write_text("x" * 2000)
    asset = _upload(client, admin_headers, doc, "text/plain", "document", module.course_id)
    small = media_routes.LIMITS["document"]._replace(max_bytes=1000)
    monkeypatch.setitem(media_routes.LIMITS, "document", small)

    response = client.post(f"/api/v1/media/{asset['id']}/complete", headers=admin_headers, json={})

    assert response.status_code == 413
    stored = db.get(MediaAsset, asset["id"])
    db.refresh(stored)
    assert stored.status == "failed" and not storage.file_path(stored.path).exists()


def test_mime_types_are_matched_exactly(client, admin_headers, storage):
    payload = {"filename": "x.pdf", "mime_type": "application/pdfx", "size_bytes": 10, "kind": "document"}
    assert client.post("/api/v1/media/uploads", headers=admin_headers, json=payload).status_code == 422


def test_a_failed_upload_can_be_processed_again(client, db, admin_headers, module, storage, worker, tmp_path):
    doc = tmp_path / "doc.txt"
    doc.write_text("Contenido del documento")
    asset = _upload(client, admin_headers, doc, "text/plain", "document", module.course_id)
    stored = db.get(MediaAsset, asset["id"])
    stored.status = "failed"
    db.commit()

    again = client.post(f"/api/v1/media/{asset['id']}/complete", headers=admin_headers, json={})
    assert again.status_code == 200
    worker()
    assert client.get(f"/api/v1/media/{asset['id']}", headers=admin_headers).json()["status"] == "ready"
