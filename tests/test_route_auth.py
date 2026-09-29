"""Every route of the API: without a session -> 401; a collaborator only reaches /learn and their profile."""

import re

from app.main import app
from tests.conftest import auth_headers

PUBLIC = {
    ("post", "/api/v1/auth/login"),
    ("post", "/api/v1/auth/refresh"),
    ("post", "/api/v1/auth/logout"),
    ("get", "/"),
    ("get", "/health"),
    # Development-only local storage: each URL carries its own signed token instead of a session.
    ("put", "/api/v1/media/local/upload/{token}"),
    ("get", "/api/v1/media/local/file/{token}"),
}
LEARNER_PREFIXES = ("/api/v1/learn/", "/api/v1/auth/")


def _operations():
    for path, methods in app.openapi()["paths"].items():
        for method in methods:
            if (method, path) not in PUBLIC:
                yield method.upper(), path, re.sub(r"\{[^}]+\}", "1", path)


def test_every_route_needs_a_session(client):
    codes = {f"{method} {path}": client.request(method, url).status_code for method, path, url in _operations()}
    assert codes and all(code == 401 for code in codes.values()), {op: c for op, c in codes.items() if c != 401}


def test_collaborators_only_reach_learning_routes(client, collaborator):
    headers = auth_headers(client, collaborator.email)
    codes = {
        f"{method} {path}": client.request(method, url, headers=headers).status_code
        for method, path, url in _operations()
        if not path.startswith(LEARNER_PREFIXES)
    }
    assert codes and all(code == 403 for code in codes.values()), {op: c for op, c in codes.items() if c != 403}
