from datetime import datetime, timedelta, timezone

from app.api.deps import get_current_user
from app.api.routes import auth as auth_routes
from app.core.security import hash_refresh_token
from app.db.models import RefreshToken
from tests.conftest import TEST_PASSWORD, auth_headers, login


def test_login_returns_tokens_and_me_works(client, admin):
    tokens = login(client, admin.email)
    assert tokens["role"] == "admin"
    me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {tokens['access_token']}"})
    assert me.status_code == 200
    assert me.json()["email"] == admin.email


def test_login_with_wrong_password_is_rejected(client, admin):
    response = client.post("/api/v1/auth/login", json={"email": admin.email, "password": "nope"})
    assert response.status_code == 401


def test_unknown_email_gets_the_same_error_as_a_wrong_password(client, admin):
    response = client.post("/api/v1/auth/login", json={"email": "nobody@test.dev", "password": TEST_PASSWORD})
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid email or password"


def test_deactivated_user_cannot_log_in(client, db, collaborator):
    collaborator.is_active = False
    db.commit()
    response = client.post("/api/v1/auth/login", json={"email": collaborator.email, "password": TEST_PASSWORD})
    assert response.status_code == 403


def test_refresh_tokens_are_stored_hashed(client, db, admin):
    tokens = login(client, admin.email)
    stored = db.query(RefreshToken).one()
    assert stored.token == hash_refresh_token(tokens["refresh_token"])
    assert stored.token != tokens["refresh_token"]


def test_refresh_rotates_and_old_token_cannot_be_reused(client, admin):
    first = login(client, admin.email)
    rotated = client.post("/api/v1/auth/refresh", json={"refresh_token": first["refresh_token"]})
    assert rotated.status_code == 200
    assert rotated.json()["refresh_token"] != first["refresh_token"]

    reused = client.post("/api/v1/auth/refresh", json={"refresh_token": first["refresh_token"]})
    assert reused.status_code == 401


def test_expired_refresh_token_is_rejected(client, db, admin):
    tokens = login(client, admin.email)
    stored = db.query(RefreshToken).one()
    stored.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db.commit()
    response = client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert response.status_code == 401


def test_concurrent_refreshes_with_the_same_token_only_succeed_once(client, session_factory, monkeypatch, admin):
    tokens = login(client, admin.email)
    find = auth_routes._find_refresh_token

    def find_then_lose_the_race(db, token):
        found = find(db, token)
        # Another request consumes the token between our read and our write.
        with session_factory() as other:
            other.query(RefreshToken).filter(RefreshToken.id == found.id).update({"revoked": True})
            other.commit()
        return found

    monkeypatch.setattr(auth_routes, "_find_refresh_token", find_then_lose_the_race)
    response = client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert response.status_code == 401
    with session_factory() as db:
        assert db.query(RefreshToken).filter(RefreshToken.revoked.is_not(True)).count() == 0


def test_logout_revokes_refresh_token(client, admin):
    tokens = login(client, admin.email)
    assert client.post("/api/v1/auth/logout", json={"refresh_token": tokens["refresh_token"]}).status_code == 200
    assert client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}).status_code == 401


def test_protected_route_requires_token(client):
    assert client.get("/api/v1/auth/me").status_code == 401
    assert client.get("/api/v1/auth/me", headers={"Authorization": "Bearer garbage"}).status_code == 401


def test_admin_only_route_rejects_collaborator(client, collaborator):
    response = client.get("/api/v1/users/", headers=auth_headers(client, collaborator.email))
    assert response.status_code == 403


def test_health_and_request_id_header(client):
    response = client.get("/health")
    assert response.json() == {"status": "ok"}
    assert response.headers["x-request-id"]


def test_client_request_ids_are_echoed_only_when_safe(client):
    assert client.get("/health", headers={"x-request-id": "abc-123.x_y"}).headers["x-request-id"] == "abc-123.x_y"
    for unsafe in ("a" * 65, "bad id", "<script>"):
        echoed = client.get("/health", headers={"x-request-id": unsafe}).headers["x-request-id"]
        assert echoed != unsafe
        assert len(echoed) == 12


def test_unhandled_errors_return_json_500_with_cors_headers(client):
    def boom():
        raise RuntimeError("kaboom")

    client.app.dependency_overrides[get_current_user] = boom  # the client fixture clears overrides on teardown
    response = client.get("/api/v1/courses/", headers={"Origin": "http://localhost:3001"})
    assert response.status_code == 500
    assert response.json()["detail"] == "Error interno del servidor"
    assert response.headers["access-control-allow-origin"] == "http://localhost:3001"
