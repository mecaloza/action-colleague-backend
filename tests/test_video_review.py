"""The automatic review of a module's AI video (real FFmpeg on tiny fake renders; the offline fake voice, presenter and
AI): frames where each scene is settled and at random, never while a scene arrives; what FFmpeg tells on its own; what
the AI sees in the frames and reads in the script; and what the admin does with each finding."""

import base64
import copy
import logging
import re
import shutil
import subprocess
from io import BytesIO
from pathlib import Path

import httpx
import openai
import pytest
from PIL import Image

from app.core.config import get_settings
from app.db.models import Course, Job, MediaAsset, Module, VideoReview
from app.services import video_reviews
from app.services.ai import designer
from app.services.ai.llm import LLMError, OpenAILLM
from app.services.storage import StorageError
from app.services.video import motion
from app.services.video import review as video_review
from app.worker.jobs import review as review_job
from tests.conftest import auth_headers
from tests.test_video import RENDER_CHOICES, SCENES, _run_until_done

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is not installed")

LONG_POINT = "Siempre puesto " * 19  # too long for its bullet: the layout lint finds it cut


def _scenes(title: str = "Reglas", narration: str = "", points: list[str] | None = None) -> list[dict]:
    """The module's two scenes, the second one written otherwise."""
    scenes = copy.deepcopy(SCENES)
    scenes[1]["slide"].update(title=title, **({"points": points} if points is not None else {}))
    scenes[1]["narration"] += narration
    return scenes


@pytest.fixture
def admin_headers(client, admin):
    return auth_headers(client, admin.email)


def _module(db, admin, scenes: list[dict], **fields) -> Module:
    course = Course(title="Seguridad", status="draft", source="ai", settings={}, created_by=admin.id)
    db.add(course)
    db.flush()
    module = Module(course_id=course.id, title="Casco", order=1, source="ai", content_text="Resumen",
                    storyboard={"scenes": scenes}, **fields)
    db.add(module)
    db.commit()
    return module


def _produce(client, headers, db, worker, module: Module, rounds: int = 5) -> Module:
    """The admin produces the module's video, and every job it queues runs."""
    response = client.post(f"/api/v1/courses/{module.course_id}/render", headers=headers, json=RENDER_CHOICES)
    assert response.status_code == 202, response.text
    _run_until_done(db, worker, rounds)
    db.expire_all()
    produced = db.get(Module, module.id)
    assert produced.generation_status == "completed", produced.generation_error
    return produced


def _review(client, headers, module_id: int) -> dict:
    response = client.get(f"/api/v1/modules/{module_id}/review", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _badge(client, headers, module: Module) -> dict | None:
    [entry] = client.get(f"/api/v1/courses/{module.course_id}", headers=headers).json()["modules"]
    return entry["review"]


def _job(db, job_type: str) -> Job:
    return db.query(Job).filter(Job.type == job_type).order_by(Job.created_at, Job.id).all()[-1]


def _settings(client, headers, **qa) -> None:
    assert client.patch("/api/v1/settings", headers=headers, json={"qa": qa}).status_code == 200


def _files(storage, folder: str) -> list[str]:
    return sorted(str(path.relative_to(storage.root / storage.bucket))
                  for path in (storage.root / storage.bucket / folder).rglob("*") if path.is_file())


# ── A video reviewed as it is produced ────────────────────────────────


def test_a_produced_video_is_reviewed_at_once_from_the_frames_its_render_took(
    client, db, admin, admin_headers, storage, worker
):
    module = _produce(client, admin_headers, db, worker, _module(db, admin, _scenes()))

    review = _review(client, admin_headers, module.id)
    render = _job(db, "video.render")
    assert (review["status"], review["verdict"], review["issues"], review["error"]) == ("done", "ok", [], None)
    assert review["video_asset_id"] == module.video_asset_id and _job(db, "video.review").status == "succeeded"
    kinds = [frame["kind"] for frame in review["frames"]]
    assert (kinds.count("settled"), kinds.count("scene"), kinds.count("random")) == (2, 2, video_review.RANDOM_FRAMES)
    assert [frame["index"] for frame in review["frames"]] == list(range(len(kinds)))
    # Never while a scene arrives or a part of its slide slides in; the settled ones right after the last part.
    timeline = video_review.timeline_of(render.state, render.state["input"]["scenes"])
    times = [frame["at_seconds"] for frame in review["frames"]]
    assert times == sorted(times) and all(0 < at < render.state["total"] for at in times)
    assert not [at for at in times for low, high in timeline.busy() if low + 0.01 < at < high - 0.01]
    settled = {frame["scene_id"]: frame["at_seconds"] for frame in review["frames"] if frame["kind"] == "settled"}
    assert settled["s2"] == pytest.approx(timeline.beats[1][-1] + motion.ENTRANCE_SECONDS + 2 / motion.FPS, abs=0.01)
    # JPEGs 960 px wide beside the video, which names them: they go when it goes.
    image = client.get(review["frames"][0]["url"].replace("http://testserver", ""))
    assert image.status_code == 200 and Image.open(BytesIO(image.content)).size == (960, 540)
    video = db.get(MediaAsset, module.video_asset_id)
    folder = video.path.rsplit("/", 1)[0]
    assert video.meta["review_frames"] == [f"{folder}/review/{index:02d}.jpg" for index in range(len(kinds))]
    assert review["checks"] == {"lint": {"warnings": []}, "black": [], "silence": [], "frozen": [],
                                "presenter_seconds": pytest.approx(render.state["total"], abs=0.3)}
    stored = db.query(VideoReview).one()
    assert (stored.render_job_id, stored.model, stored.created_by) == (render.id, "fake", admin.id)
    assert _badge(client, admin_headers, module) == {"status": "done", "verdict": "ok", "open_issues": 0, "critical": 0}

    assert client.delete(f"/api/v1/modules/{module.id}/video", headers=admin_headers).status_code == 200
    assert _files(storage, folder) == [] and _badge(client, admin_headers, module) is None  # its frames went with it


def test_what_the_ai_finds_in_the_frames_and_the_script_is_told_where_it_shows(
    client, db, admin, admin_headers, storage, worker
):
    scripted = _scenes(title="Reglas [qa:cut_text:critical]", narration=" [qa:content]")
    module = _produce(client, admin_headers, db, worker, _module(db, admin, scripted))

    review = _review(client, admin_headers, module.id)
    render = _job(db, "video.render")
    first_of_s2 = next(frame for frame in review["frames"] if frame["scene_id"] == "s2")
    assert review["verdict"] == "problems" and review["summary"]
    assert review["issues"] == [
        {"id": f"{review['id']}-1", "source": "vision", "scene_id": "s2", "at_seconds": first_of_s2["at_seconds"],
         "frame_index": first_of_s2["index"], "category": "cut_text", "severity": "critical",
         "description": "Revisión de prueba: «cut_text» en la escena 2.",
         "suggested_observation": "En la escena 2, corrige lo que se ve mal (cut_text).",
         "box": [0.0, 0.0, 0.5, 0.5], "status": "open"},
        {"id": f"{review['id']}-2", "source": "content", "scene_id": "s2",
         "at_seconds": round(render.state["starts"][1], 2), "frame_index": None, "category": "content",
         "severity": "major",
         "description": "Revisión de prueba: la narración de la escena 2 no dice lo mismo que su diapositiva.",
         "suggested_observation": "En la escena 2, haz que la narración diga lo mismo que la diapositiva.",
         "box": None, "status": "open"},
    ]
    assert _badge(client, admin_headers, module) == {
        "status": "done", "verdict": "problems", "open_issues": 2, "critical": 1,
    }


def test_open_critical_findings_keep_the_course_unpublished_only_when_the_parameters_say_so(
    client, db, admin, admin_headers, storage, worker, caplog
):
    scripted = _scenes(title="Reglas [qa:empty:critical]")
    module = _produce(client, admin_headers, db, worker, _module(db, admin, scripted))
    review = _review(client, admin_headers, module.id)
    issue = review["issues"][0]["id"]
    url = f"/api/v1/reviews/{review['id']}/issues"

    with caplog.at_level(logging.INFO, logger="app.api.routes.reviews"):
        dismissed = client.post(f"{url}/{issue}/dismiss", headers=admin_headers)
        assert dismissed.status_code == 200 and dismissed.json()["issues"][0]["status"] == "dismissed"
        assert _badge(client, admin_headers, module) == {"status": "done", "verdict": "problems", "open_issues": 0,
                                                         "critical": 0}
        reopened = client.post(f"{url}/{issue}/reopen", headers=admin_headers)
    assert reopened.json()["issues"][0]["status"] == "open" and _badge(client, admin_headers, module)["critical"] == 1
    # Who ignored a finding (what lets a course through with block_publish) is on record.
    assert [(record.issue_id, record.status, record.admin_id) for record in caplog.records
            if record.getMessage() == "review_issue_set"] == [(issue, "dismissed", admin.id), (issue, "open", admin.id)]
    assert client.post(f"{url}/9-9/dismiss", headers=admin_headers).status_code == 404
    assert client.post(f"/api/v1/reviews/999/issues/{issue}/dismiss", headers=admin_headers).status_code == 404

    _settings(client, admin_headers, block_publish=True)
    blocked = client.post(f"/api/v1/courses/{module.course_id}/publish", headers=admin_headers)
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["problems"] == [{"module_id": module.id, "message": (
        "La revisión del video de «Casco» encontró 1 problema crítico: corrígelo o ignóralo para publicar")}]
    client.post(f"{url}/{issue}/dismiss", headers=admin_headers)
    assert client.post(f"/api/v1/courses/{module.course_id}/publish", headers=admin_headers).status_code == 200


def test_asking_for_a_review_again_or_one_that_fails_never_lets_a_critical_finding_through(
    client, db, admin, admin_headers, storage, worker, monkeypatch
):
    module = _produce(client, admin_headers, db, worker,
                      _module(db, admin, _scenes(title="Reglas [qa:empty:critical]")))
    _settings(client, admin_headers, block_publish=True)

    def publish():
        return client.post(f"/api/v1/courses/{module.course_id}/publish", headers=admin_headers)

    # Asked again: while the new one waits (the badge shows it), the last one done stands for the video.
    assert client.post(f"/api/v1/modules/{module.id}/review", headers=admin_headers).status_code == 202
    assert _badge(client, admin_headers, module)["critical"] == 0 and publish().status_code == 409

    def refused(llm, **request):
        raise LLMError("El servicio de IA rechazó las imágenes.", permanent=True)

    monkeypatch.setattr(designer, "review_frames", refused)
    worker()
    assert _review(client, admin_headers, module.id)["status"] == "error"  # nor when it fails
    blocked = publish()
    assert blocked.status_code == 409 and blocked.json()["detail"]["problems"] == [{"module_id": module.id, "message": (
        "La revisión del video de «Casco» encontró 1 problema crítico: corrígelo o ignóralo para publicar")}]

    # A review done that no longer finds it stands for the video now.
    monkeypatch.setattr(designer, "review_frames",
                        lambda llm, **request: designer.FrameReview(verdict="ok", summary="", issues=[]))
    assert client.post(f"/api/v1/modules/{module.id}/review", headers=admin_headers).status_code == 202
    worker()
    assert _review(client, admin_headers, module.id)["verdict"] == "ok" and publish().status_code == 200


def test_a_review_the_ai_cannot_make_says_why_and_keeps_what_the_checks_found(
    client, db, admin, admin_headers, storage, worker
):
    scripted = _scenes(title="Reglas [qa:error]", narration=" [qa:content]", points=[LONG_POINT, "Revisarlo"])
    module = _produce(client, admin_headers, db, worker, _module(db, admin, scripted))

    review = _review(client, admin_headers, module.id)
    settled = next(frame for frame in review["frames"] if frame["scene_id"] == "s2" and frame["kind"] == "settled")
    assert (review["status"], review["verdict"], review["error"]) == (
        "error", None, "El servicio de IA rechazó las imágenes.")
    message = "En la escena 2: La viñeta 1 no cabe y se corta. Usa un texto más corto."
    # The script is read all the same: that needs no pictures.
    assert review["issues"] == [{
        "id": f"{review['id']}-1", "source": "lint", "scene_id": "s2", "at_seconds": settled["at_seconds"],
        "frame_index": settled["index"], "category": "cut_text", "severity": "major", "description": message,
        "suggested_observation": message, "box": None, "status": "open",
    }, {
        "id": f"{review['id']}-2", "source": "content", "scene_id": "s2",
        "at_seconds": round(_job(db, "video.render").state["starts"][1], 2), "frame_index": None,
        "category": "content", "severity": "major",
        "description": "Revisión de prueba: la narración de la escena 2 no dice lo mismo que su diapositiva.",
        "suggested_observation": "En la escena 2, haz que la narración diga lo mismo que la diapositiva.",
        "box": None, "status": "open",
    }]
    assert review["summary"] == "1 problema(s) de contenido." and "content_error" not in review["checks"]
    # The video is never touched: produced, published and attached as always.
    assert module.video_asset_id == review["video_asset_id"] and module.generation_status == "completed"
    assert _job(db, "video.render").status == "succeeded" and _job(db, "video.review").status == "succeeded"
    assert _badge(client, admin_headers, module) == {"status": "error", "verdict": None, "open_issues": 2,
                                                     "critical": 0}


@pytest.mark.parametrize("failing", ["taken", "stored"])
def test_frames_the_render_cannot_take_or_store_never_cost_the_video(
    failing, client, db, admin, admin_headers, storage, worker, monkeypatch
):
    if failing == "taken":  # FFmpeg fails on the render's frames, not on those its review takes
        extract, calls = video_review.extract_frames, []

        def extract_frames(video, frames, folder):
            calls.append(folder)
            if len(calls) == 1:
                raise video_review.ReviewError("No pudimos tomar el fotograma del segundo 1.0 del video.")
            return extract(video, frames, folder)

        monkeypatch.setattr(video_review, "extract_frames", extract_frames)
    else:  # the storage fails on the render's third frame (the first two are stored)
        upload = storage.upload_file

        def upload_file(path, file, content_type):
            if path.endswith("/review/02.jpg"):
                raise StorageError("Storage no pudo subir el archivo (503)")
            upload(path, file, content_type)

        monkeypatch.setattr(storage, "upload_file", upload_file)

    module = _produce(client, admin_headers, db, worker, _module(db, admin, _scenes()))

    render = _job(db, "video.render")
    assert (render.status, render.attempts) == ("succeeded", 0)  # published at once, as always
    review = _review(client, admin_headers, module.id)
    assert (review["status"], review["verdict"]) == ("done", "ok")
    # Its review took its own frames from the published video; none of the render's stays.
    video = db.get(MediaAsset, module.video_asset_id)
    folder = f"{video.path.rsplit('/', 1)[0]}/review"
    assert video.meta["review_frames"] == [
        f"{folder}/{review['id']}/{index:02d}.jpg" for index in range(len(review["frames"]))]
    assert _files(storage, folder) == video.meta["review_frames"]


def test_a_video_that_cannot_be_read_says_why_and_keeps_its_lint(client, db, admin, admin_headers, storage, worker):
    _settings(client, admin_headers, auto_review=False)
    module = _produce(client, admin_headers, db, worker,
                      _module(db, admin, _scenes(points=[LONG_POINT, "Revisarlo"])))
    storage.file_path(db.get(MediaAsset, module.video_asset_id).path).write_bytes(b"no es un video")  # damaged

    assert client.post(f"/api/v1/modules/{module.id}/review", headers=admin_headers).status_code == 202
    worker()

    review = _review(client, admin_headers, module.id)
    assert (review["status"], review["verdict"], review["frames"]) == ("error", None, [])
    assert re.fullmatch(r"No pudimos tomar el fotograma del segundo \d+\.\d del video\.", review["error"])
    assert review["checks"] == {"lint": _job(db, "video.render").state["lint"]}  # known without the video
    assert [(issue["source"], issue["description"]) for issue in review["issues"]] == [
        ("lint", "En la escena 2: La viñeta 1 no cabe y se corta. Usa un texto más corto.")]
    assert _job(db, "video.review").status == "succeeded"  # trying again wouldn't help


def test_a_script_the_ai_cannot_read_leaves_the_frames_review_standing(
    client, db, admin, admin_headers, storage, worker, monkeypatch
):
    def busy(llm, **request):
        raise LLMError("El servicio de IA no respondió. Intenta de nuevo en unos minutos.")

    monkeypatch.setattr(designer, "review_content", busy)
    module = _produce(client, admin_headers, db, worker,
                      _module(db, admin, _scenes(title="Reglas [qa:cut_text]")))

    review = _review(client, admin_headers, module.id)
    assert (review["status"], review["verdict"], review["error"]) == ("done", "problems", None)
    assert [issue["source"] for issue in review["issues"]] == ["vision"]
    assert review["checks"]["content_error"] == "El servicio de IA no respondió. Intenta de nuevo en unos minutos."


def test_a_model_that_does_not_answer_is_asked_again_and_a_review_can_be_asked_again(
    client, db, admin, admin_headers, storage, worker, monkeypatch
):
    review_frames, calls = designer.review_frames, []

    def busy(llm, **request):
        calls.append(request["frames"])
        raise LLMError("El servicio de IA no respondió. Intenta de nuevo en unos minutos.")

    monkeypatch.setattr(designer, "review_frames", busy)
    module = _produce(client, admin_headers, db, worker, _module(db, admin, _scenes()), rounds=8)

    failed = _review(client, admin_headers, module.id)
    assert len(calls) == video_reviews.JOB_ATTEMPTS  # asked again, until the job gave up
    assert (failed["status"], failed["error"]) == ("error", "El servicio de IA no respondió. Intenta de nuevo en unos "
                                                             "minutos.")
    assert len(failed["frames"]) == 2 * 2 + video_review.RANDOM_FRAMES and _job(db, "video.review").status == "failed"

    monkeypatch.setattr(designer, "review_frames", review_frames)
    asked = client.post(f"/api/v1/modules/{module.id}/review", headers=admin_headers)
    assert asked.status_code == 202 and asked.json()["review"]["id"] != failed["id"]
    _run_until_done(db, worker)
    again = _review(client, admin_headers, module.id)
    assert again["id"] == asked.json()["review"]["id"] and again["status"] == "done" and again["verdict"] == "ok"


def test_without_the_automatic_review_a_video_is_reviewed_when_the_admin_asks(
    client, db, admin, admin_headers, storage, worker
):
    _settings(client, admin_headers, auto_review=False)
    module = _produce(client, admin_headers, db, worker, _module(db, admin, _scenes()))
    assert db.query(VideoReview).count() == 0 and _badge(client, admin_headers, module) is None
    missing = client.get(f"/api/v1/modules/{module.id}/review", headers=admin_headers)
    assert missing.status_code == 404 and missing.json()["detail"] == "Este video aún no tiene revisión"

    asked = client.post(f"/api/v1/modules/{module.id}/review", headers=admin_headers)
    assert asked.status_code == 202, asked.text
    body = asked.json()
    assert (body["review"]["status"], body["review"]["frames"], body["job"]["type"]) == ("queued", [], "video.review")
    twice = client.post(f"/api/v1/modules/{module.id}/review", headers=admin_headers).json()
    assert (twice["review"]["id"], twice["job"]["id"]) == (body["review"]["id"], body["job"]["id"])
    assert _badge(client, admin_headers, module) == {"status": "queued", "verdict": None, "open_issues": 0,
                                                     "critical": 0}
    worker()

    review = _review(client, admin_headers, module.id)
    db.expire_all()
    video = db.get(MediaAsset, module.video_asset_id)
    folder = f"{video.path.rsplit('/', 1)[0]}/review/{review['id']}"
    assert review["status"] == "done" and [frame["kind"] for frame in review["frames"]].count("settled") == 2
    assert video.meta["review_frames"] == [f"{folder}/{index:02d}.jpg" for index in range(len(review["frames"]))]
    assert _files(storage, folder) == video.meta["review_frames"]  # taken from the published video
    assert review["checks"]["frozen"] == [] and review["checks"]["presenter_seconds"] is None

    # Asked again: other random frames (a seed of its own), the same settled ones.
    client.post(f"/api/v1/modules/{module.id}/review", headers=admin_headers)
    worker()
    newer = _review(client, admin_headers, module.id)
    assert newer["id"] != review["id"]
    assert [frame["at_seconds"] for frame in newer["frames"] if frame["kind"] == "random"] != [
        frame["at_seconds"] for frame in review["frames"] if frame["kind"] == "random"]
    assert [frame["at_seconds"] for frame in newer["frames"] if frame["kind"] == "settled"] == [
        frame["at_seconds"] for frame in review["frames"] if frame["kind"] == "settled"]

    # A new video: the previous one's frames go with it, and it has no review yet.
    old_folder, old_video = video.path.rsplit("/", 1)[0], video.id
    _produce(client, admin_headers, db, worker, module)
    assert _files(storage, old_folder) == [] and not db.query(MediaAsset).filter(MediaAsset.id == old_video).count()
    assert client.get(f"/api/v1/modules/{module.id}/review", headers=admin_headers).status_code == 404


def _video_file(path: Path, seconds: int = 4) -> Path:
    subprocess.run(
        ["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i", f"testsrc2=s=1920x1080:r=30:d={seconds}",
         "-f", "lavfi", "-i", f"sine=frequency=220:duration={seconds}:sample_rate=48000", "-c:v", "libx264",
         "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)],
        check=True, capture_output=True,
    )
    return path


def _video_asset(db, storage, tmp_path: Path, course_id: int, name: str) -> MediaAsset:
    path = f"courses/{course_id}/modules/legacy-{name}/video.mp4"
    storage.upload_file(path, _video_file(tmp_path / f"{name}.mp4"), "video/mp4")
    asset = MediaAsset(kind="video", status="ready", bucket=storage.bucket, path=path, mime_type="video/mp4",
                       size_bytes=1, duration_seconds=4.0, width=1920, height=1080, course_id=course_id)
    db.add(asset)
    db.flush()
    return asset


def test_a_video_made_before_reviews_is_reviewed_from_frames_taken_at_random(
    client, db, admin, admin_headers, storage, worker, tmp_path, monkeypatch
):
    read = []
    monkeypatch.setattr(designer, "review_content", lambda llm, **request: read.append(request))
    module = _module(db, admin, _scenes())
    module.video_asset_id = _video_asset(db, storage, tmp_path, module.course_id, "a").id
    db.commit()

    assert client.post(f"/api/v1/modules/{module.id}/review", headers=admin_headers).status_code == 202
    worker()

    review = _review(client, admin_headers, module.id)  # its render is unknown: when each scene shows too
    assert review["status"] == "done" and 1 < len(review["frames"]) <= video_review.UNTIMED_FRAMES
    assert {(frame["kind"], frame["scene_id"]) for frame in review["frames"]} == {("random", None)}
    assert review["checks"] == {"lint": None, "black": [], "silence": [], "frozen": None, "presenter_seconds": None,
                                "content_error": review_job.NO_SCRIPT}
    assert read == []  # nor the script it was made from: the module's now may be another one

    # A review whose video is replaced before it runs is left aside.
    assert client.post(f"/api/v1/modules/{module.id}/review", headers=admin_headers).status_code == 202
    module.video_asset_id = _video_asset(db, storage, tmp_path, module.course_id, "b").id
    db.commit()
    worker()
    db.expire_all()
    left = db.query(VideoReview).order_by(VideoReview.id.desc()).first()
    assert (left.status, left.error, left.frames) == ("error", review_job.REPLACED, [])


def test_asking_for_a_review_needs_an_ai_video_and_an_ai(client, db, admin, admin_headers, monkeypatch):
    without_video = _module(db, admin, _scenes())
    own = Module(course_id=without_video.course_id, title="Propio", order=2, source="upload")
    db.add(own)
    db.commit()

    assert client.post(f"/api/v1/modules/{own.id}/review", headers=admin_headers).status_code == 409
    missing = client.post(f"/api/v1/modules/{without_video.id}/review", headers=admin_headers)
    assert missing.status_code == 422 and missing.json()["detail"] == "«Casco» aún no tiene video"
    assert client.post("/api/v1/modules/999/review", headers=admin_headers).status_code == 404
    assert client.get(f"/api/v1/modules/{without_video.id}/review", headers=admin_headers).status_code == 404
    monkeypatch.setattr(get_settings(), "use_fake_providers", False)
    assert client.post(f"/api/v1/modules/{without_video.id}/review", headers=admin_headers).status_code == 503


def test_a_change_asked_for_a_finding_fixes_it_and_undoing_the_change_opens_it_again(
    client, db, admin, admin_headers, storage, worker
):
    module = _produce(client, admin_headers, db, worker, _module(db, admin, _scenes(title="Reglas [qa:cut_text]")))
    review = _review(client, admin_headers, module.id)
    [finding] = review["issues"]

    def status() -> str:
        return _review(client, admin_headers, module.id)["issues"][0]["status"]

    asked = client.post(f"/api/v1/modules/{module.id}/revisions", headers=admin_headers, json={
        "observation": finding["suggested_observation"], "scene_ids": [finding["scene_id"]],
        "qa_issue_id": finding["id"],
    })
    assert asked.status_code == 202, asked.text
    worker()
    revision_id = asked.json()["revision"]["id"]
    assert client.post(f"/api/v1/revisions/{revision_id}/apply", headers=admin_headers, json={}).status_code == 200
    assert status() == "fixed" and _badge(client, admin_headers, module)["open_issues"] == 0

    undo = client.post(f"/api/v1/revisions/{revision_id}/revert", headers=admin_headers)
    assert undo.status_code == 200 and status() == "open"
    assert client.post(f"/api/v1/revisions/{undo.json()['id']}/revert", headers=admin_headers).status_code == 200
    assert status() == "fixed"  # the undo undone: the change stands again


# ── On their own ──────────────────────────────────────────────────────


TIMELINE = video_review.Timeline(
    starts=[0.4, 10.0, 20.0], lengths=[9.0, 9.3, 8.0], total=29.0, beats=[[1.0, 4.0], [], [21.0, 22.5, 26.0]],
)


def test_frames_are_taken_where_each_scene_is_settled_and_at_random_never_while_a_scene_arrives():
    frames = video_review.sample(TIMELINE, 29.0, seed="job-1")
    other = video_review.sample(TIMELINE, 29.0, seed="job-2")

    assert frames == video_review.sample(TIMELINE, 29.0, seed="job-1")  # seeded
    assert [frame.index for frame in frames] == list(range(12))
    assert [frame.at for frame in frames] == sorted(frame.at for frame in frames)
    settle = motion.ENTRANCE_SECONDS + 2 / motion.FPS
    settled = {frame.scene: frame.at for frame in frames if frame.kind == "settled"}
    assert settled == pytest.approx({0: 4.0 + settle, 1: 10.0 + settle, 2: 26.0 + settle}, abs=0.001)
    assert {frame.scene: frame.at for frame in other if frame.kind == "settled"} == settled
    randoms = [frame.at for frame in frames if frame.kind == "random"]
    assert [frame.at for frame in other if frame.kind == "random"] != randoms
    for frame in frames:
        assert 0 <= frame.at <= 29.0 - video_review.LAST_SCENE_END
        assert not any(low < frame.at < high for low, high in TIMELINE.busy()), frame
        assert frame.scene == TIMELINE.scene_at(frame.at)
        if frame.kind == "scene":
            start = TIMELINE.starts[frame.scene]
            assert start <= frame.at <= start + TIMELINE.lengths[frame.scene]
        if frame.kind != "settled":  # nothing new right beside another frame
            gaps = [abs(frame.at - near.at) for near in frames if near != frame]
            assert min(gaps) >= video_review.MIN_GAP_SECONDS - 1e-6
    assert [frame.kind for frame in frames].count("random") == video_review.RANDOM_FRAMES

    untimed = video_review.sample(None, 20.0, seed="job-1")
    assert len(untimed) == video_review.UNTIMED_FRAMES and {frame.scene for frame in untimed} == {None}
    assert min(frame.at for frame in untimed) > motion.TRANSITION_SECONDS  # never in the fade in from black


def test_a_long_module_keeps_its_settled_frames_within_one_look():
    starts = [0.4 + 5 * index for index in range(30)]
    timeline = video_review.Timeline(starts, [4.0] * 30, starts[-1] + 5.0, [[]] * 30)

    frames = video_review.sample(timeline, timeline.total, seed="x")

    kinds = [frame.kind for frame in frames]
    assert len(frames) == video_review.MAX_FRAMES and kinds.count("settled") == 30 and kinds.count("random") == 0


def _checked_video(path: Path) -> Path:
    """9 s at 1080p: moving for 3 s, black for 2 s, then still; heard for 2 s, silent for 4 s, then heard again."""
    subprocess.run(
        ["ffmpeg", "-nostdin", "-y", "-v", "error",
         "-f", "lavfi", "-i", "testsrc2=s=1920x1080:r=30:d=3",
         "-f", "lavfi", "-i", "color=c=black:s=1920x1080:r=30:d=2",
         "-f", "lavfi", "-i", "color=c=0x404040:s=1920x1080:r=30:d=4",
         "-f", "lavfi", "-i", "sine=frequency=220:duration=2:sample_rate=48000",
         "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono:d=4",
         "-f", "lavfi", "-i", "sine=frequency=330:duration=3:sample_rate=48000",
         "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v];[3:a][4:a][5:a]concat=n=3:v=0:a=1[a]",
         "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac",
         "-t", "9", str(path)],
        check=True, capture_output=True,
    )
    return path


def test_ffmpeg_tells_black_screens_long_silences_and_a_presenter_standing_still(tmp_path):
    video = _checked_video(tmp_path / "checked.mp4")

    checks = video_review.video_checks(video, 9.0, [(0.0, 9.0)])
    assert checks["black"] == [{"start": 3.0, "end": 5.0, "seconds": 2.0}]
    assert checks["silence"] == [
        {"start": 2.0, "end": pytest.approx(6.0, abs=0.05), "seconds": pytest.approx(4.0, abs=0.05)}]
    assert checks["frozen"] == [{"start": 5.0, "end": 9.0, "seconds": 4.0}]  # until the end: never closed
    # Only while the presenter is in its bubble (large on the cover and the closing, the bubble's place is a slide).
    assert video_review.video_checks(video, 9.0, [(0.0, 6.0)])["frozen"] == []
    assert video_review.video_checks(video, 9.0, None)["frozen"] is None

    timeline = video_review.Timeline([0.4, 5.0], [4.0, 3.5], 9.0, [[], []])
    found = video_reviews.check_issues(
        video_review.Video(copy.deepcopy(SCENES), timeline, 9.0, True), [],
        {**checks, "presenter_seconds": 3.0},
    )
    assert [(issue["category"], issue["scene_id"], issue["at_seconds"], issue["description"]) for issue in found] == [
        ("empty", "s1", 3.0, "La pantalla queda en negro 2.0 s desde el 0:03."),
        ("other", "s1", 2.0, "No se oye la narración durante 4.0 s desde el 0:02."),
        ("other", "s2", 5.0, "El presentador se queda inmóvil 4.0 s desde el 0:05."),
        ("other", "s1", 3.0, "El video del presentador dura 3 s y la narración 9 s: al final el presentador se queda "
                             "inmóvil y luego desaparece."),
    ]
    assert {(issue["source"], issue["severity"], issue["suggested_observation"]) for issue in found} == {
        ("video", "major", "")}  # made by the render: no change to the script fixes them


def test_each_frame_tells_the_model_what_it_should_show():
    scenes = [*copy.deepcopy(SCENES), {
        "id": "s3", "slide": {"layout": "visual", "title": "Presión y desgaste"}, "narration": "Mira la infografía.",
        "visual": {"kind": "infographic", "query": "", "prompt": "Infografía: «Presión». Tres huellas.", "variant": 0},
    }]
    timeline = video_review.Timeline([0.4, 5.0, 9.0], [4.0, 3.5, 3.0], 13.0, [[0.6, 1.5], [6.0, 7.0], []])
    video = video_review.Video(scenes, timeline, 13.0, presenter=True, pictures=frozenset({2}))

    assert video.context(video_review.Frame(0, 1.2, 0, "random")) == {
        "frame_index": 0, "at_seconds": 1.2, "scene_id": "s1", "scene_number": 1, "layout": "cover",
        "presenter": "large", "texts": ["Seguridad en planta", "Lo esencial"], "revealed": "1 de 2",
    }
    assert video.context(video_review.Frame(1, 7.6, 1, "settled")) == {
        "frame_index": 1, "at_seconds": 7.6, "scene_id": "s2", "scene_number": 2,
        "layout": "bullets", "presenter": "bubble", "texts": ["Reglas", "Siempre puesto", "Revisarlo"],
        "revealed": "2 de 2",
    }
    assert video.context(video_review.Frame(2, 10.0, 2, "scene")) == {
        "frame_index": 2, "at_seconds": 10.0, "scene_id": "s3", "scene_number": 3, "layout": "visual",
        "presenter": "bubble", "texts": [], "infographic": "Infografía: «Presión». Tres huellas.",
    }
    settled = video.context(video_review.Frame(1, 7.6, 1, "settled"))
    prompt = designer.frame_review_prompt("Seguridad", "Casco", [settled],
                                          ["En la escena 2: algo ya detectado."])
    assert '"texts": ["Reglas", "Siempre puesto", "Revisarlo"]' in prompt and "algo ya detectado" in prompt


def test_what_the_model_sees_is_kept_where_the_frame_shows_it():
    video = video_review.Video(copy.deepcopy(SCENES), None, 9.0, presenter=False)
    frames = [{"index": 0, "at_seconds": 2.0, "scene_id": "s1", "kind": "settled"}]
    answer = designer.FrameReview(verdict="problems", summary="", issues=[
        designer.FrameIssueDraft(frame_index=0, scene_id="s2", category="overlap", severity="minor", description="A",
                                 suggested_observation="", box=[96, 54, 1200, 270]),
        designer.FrameIssueDraft(frame_index=7, scene_id="s2", category="other", severity="major", description="B",
                                 suggested_observation="", box=[1, 2, 3, 4]),
        designer.FrameIssueDraft(frame_index=9, scene_id="s9", category="other", severity="major", description="C",
                                 suggested_observation="", box=[]),
    ])

    found = video_reviews.vision_issues(answer, frames, video)

    # The frame says the scene (not the model); a box past the frame's edge stops at it; no frame, no box.
    assert [(issue["scene_id"], issue["frame_index"], issue["box"]) for issue in found] == [
        ("s1", 0, [0.1, 0.1, 1.0, 0.5]), ("s2", None, None), (None, None, None)]
    numbered = video_reviews.numbered(7, found, [{"id": "7-2", "status": "dismissed"}])
    assert [(issue["id"], issue["status"]) for issue in numbered] == [("7-1", "open"), ("7-2", "dismissed"),
                                                                       ("7-3", "open")]
    assert (video_reviews.verdict(found), video_reviews.verdict(found[:1]), video_reviews.verdict([])) == (
        "problems", "warnings", "ok")


class _Completions:
    def __init__(self, error: Exception | None = None):
        self.sent, self.error = {}, error

    def parse(self, **request):
        self.sent.update(request)
        if self.error is not None:
            raise self.error
        parsed = designer.FrameReview(verdict="ok", summary="", issues=[])
        message = type("Message", (), {"refusal": None, "parsed": parsed})()
        usage = type("Usage", (), {"prompt_tokens": 10, "completion_tokens": 2})()
        return type("Completion", (), {"choices": [type("Choice", (), {"message": message})()], "usage": usage})()


def _openai(completions: _Completions) -> OpenAILLM:
    llm = OpenAILLM("sk-test", "gpt-5.5")
    llm.client = type("Client", (), {"chat": type("Chat", (), {"completions": completions})()})()
    return llm


def test_the_frames_reach_the_model_each_after_its_number():
    completions = _Completions()

    _openai(completions).structured("s", "u", designer.FrameReview, images=[b"one", b"two"])

    content = completions.sent["messages"][1]["content"]
    assert content[0] == {"type": "text", "text": "u"} and len(content) == 5
    assert content[1:3] == [
        {"type": "text", "text": "Imagen 0:"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(b"one").decode(),
                                            "detail": "high"}},
    ]
    assert content[3] == {"type": "text", "text": "Imagen 1:"}
    _openai(completions).structured("s", "u", designer.FrameReview)
    assert completions.sent["messages"][1]["content"] == "u"  # without pictures, as before


def test_a_model_that_refuses_pictures_is_told_apart():
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    refused = openai.BadRequestError("image_url is only supported by certain models",
                                     response=httpx.Response(400, request=request), body=None)

    with pytest.raises(LLMError) as raised:
        _openai(_Completions(refused)).structured("s", "u", designer.FrameReview, images=[b"one"])
    assert str(raised.value) == "El servicio de IA rechazó las imágenes." and raised.value.permanent
    with pytest.raises(LLMError, match="rechazó la solicitud"):
        _openai(_Completions(refused)).structured("s", "u", designer.FrameReview)
