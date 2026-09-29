"""The AI studio on a real PostgreSQL (row locks)."""

import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.api.routes import studio as studio_routes
from app.db.models import Course, Module
from tests.conftest import auth_headers

OUTLINE = {
    "title": "Seguridad en planta", "description": "D", "audience": "Operarios", "objectives": ["Usar el casco"],
    "modules": [
        {"title": f"Módulo {i}", "summary": "S", "objectives": ["o"], "key_points": ["k"], "estimated_minutes": 3,
         "include_quiz": True}
        for i in range(1, 4)
    ],
}


@pytest.fixture
def engine(pg_engine):
    return pg_engine


def test_approving_the_outline_twice_at_once_creates_the_modules_once(client, db, admin, monkeypatch):
    headers = auth_headers(client, admin.email)
    course = Course(title="Borrador", status="draft", source="ai", settings={}, created_by=admin.id)
    db.add(course)
    db.commit()
    refresh = studio_routes.refresh_course_enrollments

    def slow_refresh(session, course_):  # widen the window between creating the modules and committing
        refresh(session, course_)
        time.sleep(0.3)

    monkeypatch.setattr(studio_routes, "refresh_course_enrollments", slow_refresh)
    url = f"/api/v1/courses/{course.id}/outline"
    with ThreadPoolExecutor(2) as pool:
        responses = list(pool.map(lambda _: client.put(url, headers=headers, json=OUTLINE), range(2)))

    assert [response.status_code for response in responses] == [200, 200]
    assert db.query(Module).filter(Module.course_id == course.id).count() == 3
