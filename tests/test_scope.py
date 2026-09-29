"""The platform only covers courses: removed features must stay gone."""

from app.db.models import Course, Enrollment, User
from app.main import app
from tests.conftest import auth_headers

REMOVED_PREFIXES = (
    "/api/v1/communications",
    "/api/v1/documents",
    "/api/v1/certificates",
    "/api/v1/series",
    # Replaced by /courses, /learn, /dashboard and /users in the course studio API.
    "/api/v1/enrollments",
    "/api/v1/module-progress",
    "/api/v1/evaluations",
    "/api/v1/dashboards",
)
REMOVED_OPERATIONS = {
    ("get", "/api/v1/users/org-chart"),
    ("get", "/api/v1/users/{user_id}/profile"),
    ("put", "/api/v1/users/{user_id}/permissions"),
    ("put", "/api/v1/users/{user_id}/role"),
    ("post", "/api/v1/auth/register"),
    ("post", "/api/v1/courses/{course_id}/generate"),
    ("get", "/api/v1/modules/"),
    ("post", "/api/v1/modules/"),
}


def test_removed_features_are_not_routed():
    paths = app.openapi()["paths"]
    assert [path for path in paths if path.startswith(REMOVED_PREFIXES)] == []
    operations = {(method, path) for path, methods in paths.items() for method in methods}
    assert operations & REMOVED_OPERATIONS == set()


def test_org_chart_and_permissions_fields_are_ignored(client, db, admin):
    headers = auth_headers(client, admin.email)
    created = client.post(
        "/api/v1/users",
        headers=headers,
        json={
            "name": "Ana",
            "email": "ana@test.dev",
            "password": "Passw0rd!",
            "position": "Ventas",
            "reports_to": admin.id,
            "permissions": ["documents.generate"],
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert "reports_to" not in body and "permissions" not in body
    stored = db.get(User, body["id"])
    db.refresh(stored)
    assert stored.reports_to is None and stored.permissions_json == "[]"

    updated = client.patch(f"/api/v1/users/{body['id']}", headers=headers, json={"department": "Comercial"})
    assert updated.status_code == 200
    assert updated.json()["department"] == "Comercial"


def test_roles_are_validated(client, admin, collaborator):
    headers = auth_headers(client, admin.email)
    assert client.patch(f"/api/v1/users/{collaborator.id}", headers=headers, json={"role": "superadmin"}).status_code == 422
    created = client.post(
        "/api/v1/users", headers=headers, json={"name": "B", "email": "b@test.dev", "password": "Passw0rd!", "role": "Admin"}
    )
    assert created.status_code == 422


def test_dashboard_counts_completed_courses(client, db, admin, collaborator):
    course = Course(title="Curso", status="published")
    db.add(course)
    db.flush()
    db.add_all(
        [
            Enrollment(user_id=collaborator.id, course_id=course.id, status="completed"),
            Enrollment(user_id=admin.id, course_id=course.id, status="in_progress"),
        ]
    )
    db.commit()

    response = client.get("/api/v1/dashboard", headers=auth_headers(client, admin.email))
    assert response.status_code == 200
    assert response.json()["enrollments"] == {"total": 2, "completed": 1, "in_progress": 1}
