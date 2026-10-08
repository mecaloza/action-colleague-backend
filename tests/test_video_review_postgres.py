"""The videos' reviews on a real PostgreSQL: findings in JSONB, the newest review of each video, two asks at once, what
keeps a course unpublished meanwhile and a module's deletion."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from app.db.models import Course, Job, MediaAsset, Module, VideoReview
from tests.conftest import auth_headers


@pytest.fixture
def engine(pg_engine):
    return pg_engine


def _finding(issue_id: str, severity: str) -> dict:
    return {"id": issue_id, "source": "vision", "scene_id": "s1", "at_seconds": 2.0, "frame_index": 0,
            "category": "overlap", "severity": severity, "description": "Se encima.", "suggested_observation": "",
            "box": None, "status": "open"}


def test_the_newest_review_of_each_video_shows_and_goes_with_its_module(client, db, admin, storage):
    headers = auth_headers(client, admin.email)
    course = Course(title="Seguridad", status="draft", source="ai", settings={}, created_by=admin.id)
    db.add(course)
    db.flush()
    video = MediaAsset(kind="video", status="ready", bucket="course-media", path="courses/1/v/video.mp4",
                       mime_type="video/mp4", course_id=course.id)
    db.add(video)
    db.flush()
    module = Module(course_id=course.id, title="Casco", order=1, source="ai", video_asset_id=video.id,
                    content_text="Resumen", storyboard={"scenes": []})
    db.add(module)
    db.flush()
    older = VideoReview(module_id=module.id, video_asset_id=video.id, status="done", verdict="ok", issues=[])
    db.add(older)
    db.flush()
    newer = VideoReview(module_id=module.id, video_asset_id=video.id, status="done", verdict="problems",
                        issues=[_finding(f"{older.id + 1}-1", "critical"), _finding(f"{older.id + 1}-2", "minor")])
    db.add(newer)
    db.commit()

    def badge() -> dict:
        return client.get(f"/api/v1/courses/{course.id}", headers=headers).json()["modules"][0]["review"]

    assert newer.id == older.id + 1
    assert badge() == {"status": "done", "verdict": "problems", "open_issues": 2, "critical": 1}
    dismissed = client.post(f"/api/v1/reviews/{newer.id}/issues/{newer.id}-1/dismiss", headers=headers)
    assert dismissed.status_code == 200 and badge()["critical"] == 0

    # Two asks at once (a double click): the module is held, the second one gets the first one's review.
    url = f"/api/v1/modules/{module.id}/review"
    with ThreadPoolExecutor(2) as pool:
        responses = list(pool.map(lambda _: client.post(url, headers=headers), range(2)))
    assert [response.status_code for response in responses] == [202, 202]
    assert len({response.json()["review"]["id"] for response in responses}) == 1
    assert db.query(Job).filter(Job.type == "video.review").count() == 1

    # While that one waits, the newest one done still keeps the course unpublished (with the parameter).
    assert client.post(f"/api/v1/reviews/{newer.id}/issues/{newer.id}-1/reopen", headers=headers).status_code == 200
    assert client.patch("/api/v1/settings", headers=headers, json={"qa": {"block_publish": True}}).status_code == 200
    blocked = client.post(f"/api/v1/courses/{course.id}/publish", headers=headers)
    assert blocked.status_code == 409 and blocked.json()["detail"]["problems"] == [{"module_id": module.id, "message": (
        "La revisión del video de «Casco» encontró 1 problema crítico: corrígelo o ignóralo para publicar")}]
    assert badge() == {"status": "queued", "verdict": None, "open_issues": 0, "critical": 0}

    assert client.delete(f"/api/v1/modules/{module.id}", headers=headers).status_code == 204
    assert db.query(VideoReview).count() == 0  # its reviews go with it
