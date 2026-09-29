"""The video render under retries, deploys and admins working at the same time (fake voice and presenter)."""

import shutil

import pytest

from app.db.models import Course, Job, MediaAsset, Module
from app.services.ai.fake import FakeAvatar, FakeVoice
from app.services.video import captions, compose
from app.services.storage import StorageError
from app.services.video.avatar import AvatarError
from app.services.video.voice import VoiceError
from app.worker import queue
from tests.conftest import auth_headers
from tests.test_video import RENDER_CHOICES, SCENES, _run_until_done

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is not installed")

THREE_SCENES = [*SCENES, {**SCENES[1], "id": "s3", "narration": "Y una tercera escena para probar los reintentos."}]


@pytest.fixture
def admin_headers(client, admin):
    return auth_headers(client, admin.email)


@pytest.fixture
def ai_module(db, admin):
    course = Course(title="Seguridad", status="draft", source="ai", settings={}, created_by=admin.id)
    db.add(course)
    db.flush()
    module = Module(course_id=course.id, title="Casco", order=1, source="ai", storyboard={"scenes": THREE_SCENES})
    db.add(module)
    db.commit()
    return module


def _fast_forward(db) -> None:
    for job in db.query(Job).filter(Job.status == "queued").all():
        job.run_after = queue.utcnow()
    db.commit()


def _render_files(storage) -> list[str]:
    """The intermediates renders leave under .../render/{job}/ while they work."""
    return sorted(str(path) for path in (storage.root / storage.bucket).rglob("render/*/*"))


def _start_render(client, headers, module) -> None:
    response = client.post(f"/api/v1/courses/{module.course_id}/render", headers=headers, json=RENDER_CHOICES)
    assert response.status_code == 202, response.text


def test_a_worker_that_lost_the_job_while_uploading_does_not_publish(
    client, db, admin_headers, ai_module, storage, worker, session_factory, monkeypatch
):
    _start_render(client, admin_headers, ai_module)
    worker()  # narration done, presenter requested: the job waits
    _fast_forward(db)

    upload_file = type(storage).upload_file
    handed_back = []

    def upload(self, path, file_path, content_type):
        if path.endswith("/video.mp4") and not handed_back:
            with session_factory() as session:  # a deploy hands the job back while the video uploads
                job = session.query(Job).one()
                handed_back.append(queue.requeue(session, job.id, job.locked_by))
        return upload_file(self, path, file_path, content_type)

    monkeypatch.setattr(type(storage), "upload_file", upload)
    worker(max_jobs=1)  # only the run that loses the job
    db.expire_all()
    assert handed_back == [True]
    assert db.get(Module, ai_module.id).video_asset_id is None  # the stale worker did not attach anything
    assert db.query(Job).one().status == "queued"
    assert _render_files(storage)  # nor delete what the next owner needs

    monkeypatch.setattr(type(storage), "upload_file", upload_file)
    _run_until_done(db, worker)
    db.expire_all()
    module = db.get(Module, ai_module.id)
    assert module.generation_status == "completed" and module.video_asset_id
    assert db.query(Job).one().status == "succeeded"
    assert db.query(MediaAsset).filter(MediaAsset.kind == "video").count() == 1
    assert not _render_files(storage)
    videos = list((storage.root / storage.bucket).rglob("video.mp4"))
    assert len(videos) == 1  # the stale worker's copy was deleted


def test_a_rerun_after_publishing_does_nothing(client, db, admin_headers, ai_module, storage, worker):
    _start_render(client, admin_headers, ai_module)
    _run_until_done(db, worker)
    db.expire_all()
    job = db.query(Job).one()
    published = db.get(Module, ai_module.id).video_asset_id
    job.status, job.run_after = "queued", queue.utcnow()  # e.g. the process died right after the commit
    db.commit()

    worker()
    db.expire_all()
    assert db.query(Job).one().result == {"skipped": "already published"}
    assert db.get(Module, ai_module.id).video_asset_id == published


def test_modules_with_their_own_video_are_never_rendered(client, db, admin_headers, admin, storage, worker, tmp_path):
    course = Course(title="Mixto", status="draft", source="ai", settings={}, created_by=admin.id)
    db.add(course)
    db.flush()
    recording = tmp_path / "grabacion.mp4"
    recording.write_bytes(b"\0" * 64)
    path = f"courses/{course.id}/uploads/grabacion.mp4"
    storage.upload_file(path, recording, "video/mp4")
    upload = MediaAsset(kind="video", status="ready", bucket=storage.bucket, path=path, mime_type="video/mp4",
                        size_bytes=64, course_id=course.id)
    db.add(upload)
    db.flush()
    # Drafted with AI once, then the admin uploaded their own recording.
    module = Module(course_id=course.id, title="Grabado", order=1, source="upload", storyboard={"scenes": SCENES},
                    video_asset_id=upload.id, generation_status="completed")
    db.add(module)
    db.commit()

    assert client.post(f"/api/v1/courses/{course.id}/render", headers=admin_headers, json=RENDER_CHOICES).status_code == 422
    single = client.post(f"/api/v1/modules/{module.id}/render", headers=admin_headers)
    assert single.status_code == 409 and "su propio contenido" in single.json()["detail"]
    listed = client.post(
        f"/api/v1/courses/{course.id}/render", headers=admin_headers, json={**RENDER_CHOICES, "module_ids": [module.id]}
    )
    assert listed.status_code == 409
    assert db.query(Job).count() == 0 and storage.file_path(path).exists()


def test_a_video_the_admin_uploads_during_the_render_wins(client, db, admin_headers, ai_module, storage, worker, tmp_path):
    _start_render(client, admin_headers, ai_module)
    worker()  # waiting for the presenter
    own = tmp_path / "propio.mp4"
    own.write_bytes(b"\0" * 64)
    storage.upload_file("uploads/propio.mp4", own, "video/mp4")
    upload = MediaAsset(kind="video", status="ready", bucket=storage.bucket, path="uploads/propio.mp4",
                        mime_type="video/mp4", size_bytes=64, course_id=ai_module.course_id)
    db.add(upload)
    db.flush()
    module = db.get(Module, ai_module.id)
    module.source, module.video_asset_id, module.generation_status = "upload", upload.id, "completed"
    db.commit()

    _run_until_done(db, worker)
    db.expire_all()
    module = db.get(Module, ai_module.id)
    assert module.video_asset_id == upload.id and module.generation_status == "completed"
    assert db.query(Job).one().result == {"skipped": "module has its own video"}
    assert db.query(MediaAsset).filter(MediaAsset.kind == "video").count() == 1
    assert not _render_files(storage)


def test_a_retried_render_keeps_its_voice_and_never_pays_twice(
    client, db, admin_headers, ai_module, storage, worker, monkeypatch
):
    spoken, uploads, starts = [], [], []
    speak, upload_audio, start = FakeVoice.speak, FakeAvatar.upload_audio, FakeAvatar.start
    fail = {"voice": 1, "start": 1}

    def flaky_speak(self, text, voice_id, previous_text="", next_text=""):
        if "tercera" in text and fail["voice"]:
            fail["voice"] -= 1
            raise VoiceError("ElevenLabs respondió 500 al generar la narración.")
        spoken.append((text[:12], voice_id))
        return speak(self, text, voice_id, previous_text, next_text)

    def counted_upload(self, audio):
        uploads.append(audio.name)
        return upload_audio(self, audio)

    def lost_answer(self, avatar_id, background, **request):
        starts.append(request)
        video_id = start(self, avatar_id, background, **request)
        if fail["start"]:  # HeyGen made the video but the answer never arrived
            fail["start"] -= 1
            raise AvatarError("No pudimos conectar con HeyGen al crear el video del presentador.", retryable=True)
        return video_id

    monkeypatch.setattr(FakeVoice, "speak", flaky_speak)
    monkeypatch.setattr(FakeAvatar, "upload_audio", counted_upload)
    monkeypatch.setattr(FakeAvatar, "start", lost_answer)
    _start_render(client, admin_headers, ai_module)
    worker()  # scenes 1-2 narrated, scene 3 fails: the job retries later
    changed = client.patch(
        f"/api/v1/courses/{ai_module.course_id}", headers=admin_headers, json={"settings": {"voice_id": "otra-voz"}}
    )
    assert changed.status_code == 200
    for _ in range(8):
        _fast_forward(db)
        worker()

    db.expire_all()
    assert db.query(Job).one().status == "succeeded"
    assert [voice for _, voice in spoken] == ["fake-voice-es"] * 3  # each scene narrated once, in one voice
    assert len(uploads) == 1 and len(starts) == 2 and starts[0] == starts[1]  # the very same request again
    assert db.get(Module, ai_module.id).storyboard["render"]["voice_id"] == "fake-voice-es"


def test_the_script_and_its_video_wait_for_each_other(client, db, admin_headers, ai_module, storage, worker):
    _start_render(client, admin_headers, ai_module)
    worker()  # the render waits for the presenter
    rewrite = client.post(
        f"/api/v1/modules/{ai_module.id}/storyboard/regenerate", headers=admin_headers, json={"feedback": "más corto"}
    )
    assert rewrite.status_code == 409 and "se está produciendo" in rewrite.json()["detail"]
    edit = client.put(f"/api/v1/modules/{ai_module.id}/storyboard", headers=admin_headers, json={"scenes": SCENES})
    assert edit.status_code == 409 and "video se está produciendo" in edit.json()["detail"]
    other_voice = client.post(
        f"/api/v1/courses/{ai_module.course_id}/render", headers=admin_headers, json={**RENDER_CHOICES, "voice_id": "otra"}
    )
    assert other_voice.status_code == 409  # a different request while this one runs

    _run_until_done(db, worker)
    rewrite = client.post(
        f"/api/v1/modules/{ai_module.id}/storyboard/regenerate", headers=admin_headers, json={"feedback": "más corto"}
    )
    assert rewrite.status_code == 202
    render = client.post(f"/api/v1/modules/{ai_module.id}/render", headers=admin_headers)
    assert render.status_code == 409 and "se está escribiendo" in render.json()["detail"]


def test_a_failed_render_leaves_no_files(client, db, admin_headers, ai_module, storage, worker, monkeypatch):
    def broken(*args, **kwargs):
        raise compose.ComposeError("FFmpeg falló: boom")

    monkeypatch.setattr(compose, "compose_video", broken)
    _start_render(client, admin_headers, ai_module)
    for _ in range(10):
        _fast_forward(db)
        worker()
    db.expire_all()
    assert db.query(Job).one().status == "failed"
    module = db.get(Module, ai_module.id)
    assert module.generation_status == "failed" and "No pudimos procesar" in module.generation_error
    assert not _render_files(storage)


def test_deleting_a_module_while_its_video_waits_deletes_its_files(client, db, admin_headers, ai_module, storage, worker):
    _start_render(client, admin_headers, ai_module)
    worker()
    assert _render_files(storage)
    assert client.delete(f"/api/v1/modules/{ai_module.id}", headers=admin_headers).status_code == 204
    assert db.query(Job).count() == 0 and not _render_files(storage)


def test_any_presenter_failure_still_produces_the_video(client, db, admin_headers, ai_module, storage, worker, monkeypatch):
    def unreadable(self, video_id):
        raise TypeError("'NoneType' object is not subscriptable")  # e.g. an answer shaped unlike the docs

    monkeypatch.setattr(FakeAvatar, "status", unreadable)
    _start_render(client, admin_headers, ai_module)
    _run_until_done(db, worker)

    db.expire_all()
    module = db.get(Module, ai_module.id)
    assert module.generation_status == "completed" and module.video_asset_id
    [detail] = client.get(f"/api/v1/courses/{ai_module.course_id}", headers=admin_headers).json()["modules"]
    assert "sin presentador" in detail["video_warning"]


def test_captions_escape_markup():
    words = [captions.TimedWord("Si", 0.0, 0.2), captions.TimedWord("x<y", 0.2, 0.5), captions.TimedWord("&", 0.5, 0.7)]
    vtt = captions.to_vtt(words)
    assert "x&lt;y &amp;" in vtt and "x<y" not in vtt


def _module_render(db, module_id: int) -> dict:
    db.expire_all()
    return db.get(Module, module_id).storyboard["render"]


def test_a_storage_failure_after_heygen_finished_keeps_the_presenter(
    client, db, admin_headers, ai_module, storage, worker, monkeypatch
):
    upload_file, failed = type(storage).upload_file, []

    def flaky_upload(self, path, file_path, content_type):
        if path.endswith("/presenter.mp4") and not failed:
            failed.append(path)
            raise StorageError("Storage no pudo subir el archivo (sin conexión)")
        return upload_file(self, path, file_path, content_type)

    monkeypatch.setattr(type(storage), "upload_file", flaky_upload)
    _start_render(client, admin_headers, ai_module)
    for _ in range(6):
        _fast_forward(db)
        worker()
    assert failed and db.query(Job).one().status == "succeeded"
    render = _module_render(db, ai_module.id)
    assert render["avatar_id"] == "fake-avatar" and render["warning"] is None  # retried, presenter kept


def test_a_database_hiccup_while_fetching_the_presenter_keeps_it(
    client, db, admin_headers, ai_module, storage, worker, monkeypatch
):
    from sqlalchemy.exc import OperationalError

    from app.worker.runner import JobContext

    progress, failed = JobContext.progress, []

    def flaky_progress(self, percent, step=""):
        if percent == 70 and not failed:
            failed.append(percent)
            raise OperationalError("UPDATE jobs", {}, Exception("server closed the connection"))
        return progress(self, percent, step)

    monkeypatch.setattr(JobContext, "progress", flaky_progress)
    _start_render(client, admin_headers, ai_module)
    for _ in range(6):
        _fast_forward(db)
        worker()
    assert failed and db.query(Job).one().status == "succeeded"
    render = _module_render(db, ai_module.id)
    assert render["avatar_id"] == "fake-avatar" and render["warning"] is None


def test_a_new_render_waits_for_a_job_that_already_published(client, db, admin_headers, ai_module, storage, worker):
    _start_render(client, admin_headers, ai_module)
    _run_until_done(db, worker)
    job = db.query(Job).one()
    job.status, job.run_after = "queued", queue.utcnow()  # e.g. handed back by a deploy right after publishing
    db.commit()

    again = client.post(f"/api/v1/modules/{ai_module.id}/render", headers=admin_headers)
    assert again.status_code == 409 and "terminando de guardar" in again.json()["detail"]
    worker()
    db.expire_all()
    assert db.get(Module, ai_module.id).generation_status == "completed"
    assert client.post(f"/api/v1/modules/{ai_module.id}/render", headers=admin_headers).status_code == 202


def test_a_retry_renders_the_script_it_started_with(client, db, admin_headers, ai_module, storage, worker, monkeypatch):
    speak, spoken, fail = FakeVoice.speak, [], [True]

    def flaky_speak(self, text, voice_id, previous_text="", next_text=""):
        if "Hay dos reglas" in text and fail:
            fail.pop()
            raise VoiceError("ElevenLabs respondió 500 al generar la narración.")
        spoken.append(text)
        return speak(self, text, voice_id, previous_text, next_text)

    monkeypatch.setattr(FakeVoice, "speak", flaky_speak)
    _start_render(client, admin_headers, ai_module)
    worker()  # scene 1 narrated, scene 2 fails
    module = db.get(Module, ai_module.id)
    extra = {**THREE_SCENES[0], "id": "s4", "narration": "Una escena agregada mientras tanto."}
    module.storyboard = {**module.storyboard, "scenes": [*THREE_SCENES, extra]}  # e.g. a script saved by another tab
    db.commit()
    for _ in range(8):
        _fast_forward(db)
        worker()

    assert db.query(Job).one().status == "succeeded"
    assert len(spoken) == 3 and not any("agregada" in text for text in spoken)


def test_a_video_the_admin_uploads_while_composing_wins(
    client, db, admin_headers, ai_module, storage, worker, session_factory, monkeypatch, tmp_path
):
    own = tmp_path / "propio.mp4"
    own.write_bytes(b"\0" * 64)
    storage.upload_file("uploads/propio.mp4", own, "video/mp4")
    upload_file, uploaded = type(storage).upload_file, []

    def upload_during_publish(self, path, file_path, content_type):
        if path.endswith("/video.mp4") and not uploaded:
            with session_factory() as session:  # the admin's own video lands just now
                asset = MediaAsset(kind="video", status="ready", bucket=storage.bucket, path="uploads/propio.mp4",
                                   mime_type="video/mp4", size_bytes=64, course_id=ai_module.course_id)
                session.add(asset)
                session.flush()
                module = session.get(Module, ai_module.id)
                module.source, module.video_asset_id = "upload", asset.id
                session.commit()
                uploaded.append(asset.id)
        return upload_file(self, path, file_path, content_type)

    monkeypatch.setattr(type(storage), "upload_file", upload_during_publish)
    _start_render(client, admin_headers, ai_module)
    _run_until_done(db, worker)

    db.expire_all()
    assert db.query(Job).one().result == {"skipped": "module has its own video"}
    assert db.get(Module, ai_module.id).video_asset_id == uploaded[0]
    assert not list((storage.root / storage.bucket).rglob("video-*/*"))  # the render's copy is gone


def test_a_presenter_clip_ffmpeg_cannot_use_drops_only_the_bubble(
    client, db, admin_headers, ai_module, storage, worker, monkeypatch
):
    compose_video = compose.compose_video

    def no_presenter_please(slides, narration, avatar, output, work):
        if avatar is not None:
            raise compose.ComposeError("FFmpeg falló: presenter.mp4: Invalid data found when processing input")
        return compose_video(slides, narration, avatar, output, work)

    monkeypatch.setattr(compose, "compose_video", no_presenter_please)
    _start_render(client, admin_headers, ai_module)
    _run_until_done(db, worker)

    render = _module_render(db, ai_module.id)
    assert db.get(Module, ai_module.id).generation_status == "completed"
    assert render["avatar_id"] == "" and "sin presentador" in render["warning"]


def test_a_copy_left_by_a_failed_publish_is_deleted_by_the_next_run(
    client, db, admin_headers, ai_module, storage, worker, monkeypatch
):
    upload_file, failed = type(storage).upload_file, []

    def poster_fails_once(self, path, file_path, content_type):
        if path.endswith("/poster.jpg") and not failed:
            failed.append(path)
            raise StorageError("Storage no pudo subir el archivo (sin conexión)")
        return upload_file(self, path, file_path, content_type)

    monkeypatch.setattr(type(storage), "upload_file", poster_fails_once)
    _start_render(client, admin_headers, ai_module)
    for _ in range(8):
        _fast_forward(db)
        worker()

    assert failed and db.query(Job).one().status == "succeeded"
    assert len(list((storage.root / storage.bucket).rglob("video.mp4"))) == 1  # only the published one
