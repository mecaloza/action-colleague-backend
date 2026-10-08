"""Rankings: points by period in the platform's time zone, ties, who is ranked, a path's ranking, and what learners
see of it (or don't)."""

from datetime import datetime, timedelta, timezone

import pytest

from app.db.models import LearningPath, PointsEntry, User
from app.services import ranking
from tests.conftest import auth_headers
from tests.test_certificates import _enroll, _learner, _settings
from tests.test_paths import _assign, _complete, _course, _path, _profile

API = "/api/v1"
NOW = datetime(2026, 10, 7, 3, 30, tzinfo=timezone.utc)  # Tuesday 6 October, 22:30 in Bogotá


@pytest.fixture
def admin_headers(client, admin):
    return auth_headers(client, admin.email)


@pytest.fixture
def frozen(monkeypatch):
    monkeypatch.setattr(ranking, "utcnow", lambda: NOW)


def _people(db, *names: str, role: str = "collaborator", active: bool = True) -> list[User]:
    users = [User(name=name, email=f"{name.split()[0].lower()}{i}@x.test", role=role, is_active=active,
                  password_hash="x") for i, name in enumerate(names)]
    db.add_all(users)
    db.commit()
    return users


def _earn(db, user: User, points: int, at: datetime | None = None, *, course=None, source: str = "activity",
          path_id: int | None = None) -> None:
    """A row of the ledger, earned `at` (by default now, as the ranking's clock says)."""
    target = path_id if source == "path" else (course.id if course is not None else 0)
    db.add(PointsEntry(user_id=user.id, course_id=course.id if course is not None else None, path_id=path_id,
                       source=source, source_key=f"{source}:{target}", points=points,
                       earned_at=at or ranking.utcnow()))
    db.commit()


def _ranked(body: dict) -> list[tuple[int, str, int]]:
    return [(item["rank"], item["user"]["name"], item["points"]) for item in body["items"]]


# ── Periods ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(("zone", "period", "start"), [
    ("America/Bogota", "today", "2026-10-06T00:00:00-05:00"),
    ("America/Bogota", "week", "2026-10-05T00:00:00-05:00"),  # its Monday
    ("America/Bogota", "month", "2026-10-01T00:00:00-05:00"),
    ("Asia/Tokyo", "today", "2026-10-07T00:00:00+09:00"),  # already Wednesday there
    ("Asia/Tokyo", "week", "2026-10-05T00:00:00+09:00"),
    ("Europe/Madrid", "month", "2026-10-01T00:00:00+02:00"),
])
def test_periods_start_at_midnight_in_the_platforms_time_zone(zone, period, start):
    assert ranking.period_start(period, zone, NOW).isoformat() == start
    assert ranking.period_start("all", zone, NOW) is None


def test_points_count_in_the_period_they_were_earned(client, db, admin_headers, frozen):
    _settings(client, admin_headers, general={"timezone": "America/Bogota"})
    ana, beto = _people(db, "Ana", "Beto")
    midnight = datetime(2026, 10, 6, 5, tzinfo=timezone.utc)  # today's, in Bogotá
    _earn(db, ana, 10, midnight)
    _earn(db, ana, 5, midnight - timedelta(seconds=1))  # yesterday, Monday: this week
    _earn(db, beto, 7, datetime(2026, 10, 5, 5, tzinfo=timezone.utc))  # Monday at midnight: this week
    _earn(db, beto, 3, datetime(2026, 10, 5, 4, 59, tzinfo=timezone.utc))  # Sunday night: last week, this month
    _earn(db, beto, 100, datetime(2026, 9, 30, 23, tzinfo=timezone.utc))  # 30 September, 18:00: last month

    def ranked(period: str) -> list[tuple[int, str, int]]:
        response = client.get(f"{API}/ranking", headers=admin_headers, params={"period": period})
        assert response.status_code == 200, response.text
        return _ranked(response.json())

    assert ranked("today") == [(1, "Ana", 10)]
    assert ranked("week") == [(1, "Ana", 15), (2, "Beto", 7)]
    assert ranked("month") == [(1, "Ana", 15), (2, "Beto", 10)]
    assert ranked("all") == [(1, "Beto", 110), (2, "Ana", 15)]
    body = client.get(f"{API}/ranking", headers=admin_headers).json()  # today, by default
    assert (body["period"], body["starts_at"], body["path"], body["total_people"]) == (
        "today", "2026-10-06T00:00:00-05:00", None, 1)
    assert client.get(f"{API}/ranking?period=all", headers=admin_headers).json()["starts_at"] is None
    assert client.get(f"{API}/ranking?period=year", headers=admin_headers).status_code == 422


def test_ties_share_their_place_then_go_by_name(client, db, admin_headers):
    dora, beto, ana, carla, eva = _people(db, "Dora", "Beto", "ana", "Carla", "Eva")
    for person, points in ((dora, 50), (beto, 80), (ana, 80), (carla, 100), (eva, 50)):
        _earn(db, person, points)

    body = client.get(f"{API}/ranking", headers=admin_headers).json()

    assert _ranked(body) == [(1, "Carla", 100), (2, "ana", 80), (2, "Beto", 80), (4, "Dora", 50), (4, "Eva", 50)]
    limited = client.get(f"{API}/ranking?limit=2", headers=admin_headers).json()
    assert _ranked(limited) == [(1, "Carla", 100), (2, "ana", 80)] and limited["total_people"] == 5


def test_only_active_collaborators_who_earned_points_are_ranked(client, db, admin, admin_headers):
    (ana,) = _people(db, "Ana")
    (gone,) = _people(db, "Gone", active=False)
    (zero,) = _people(db, "Zero")
    _earn(db, admin, 500)  # admins take courses too: never ranked
    _earn(db, gone, 300)
    _earn(db, zero, 0, source="course")  # a course completed while it gave no points
    _earn(db, ana, 20)

    body = client.get(f"{API}/ranking", headers=admin_headers).json()

    assert _ranked(body) == [(1, "Ana", 20)] and body["total_people"] == 1


def test_a_paths_ranking_counts_its_people_in_its_courses(client, db, admin, admin_headers):
    inside, outside = _course(db, admin, "Dentro"), _course(db, admin, "Fuera")
    path = _path(client, admin_headers, [inside], title="Ruta de asesores")
    advisor = _profile(client, admin_headers, "Asesor de flotas")
    ana, beto, carla = _people(db, "Ana", "Beto", "Carla")
    ana.profile_id = advisor["id"]
    db.commit()
    _assign(client, admin_headers, path, ana, beto)  # Carla doesn't have the path
    _earn(db, ana, 30, course=inside)
    _earn(db, ana, 500, course=outside)  # another course: not this path's
    _earn(db, ana, 200, source="path", path_id=path["id"])
    _earn(db, beto, 40, course=inside)
    _earn(db, beto, 100, course=inside, source="course")
    _earn(db, carla, 1000, course=inside)

    body = client.get(f"{API}/ranking", headers=admin_headers, params={"path_id": path["id"], "period": "all"}).json()

    assert body["path"] == {"id": path["id"], "title": "Ruta de asesores"} and body["total_people"] == 2
    assert body["items"] == [
        {"rank": 1, "user": {"id": ana.id, "name": "Ana", "profile": {"id": advisor["id"], "name": "Asesor de flotas"}},
         "points": 230, "completed_courses": 0},
        {"rank": 2, "user": {"id": beto.id, "name": "Beto", "profile": None}, "points": 140, "completed_courses": 1},
    ]
    everywhere = client.get(f"{API}/ranking?period=all", headers=admin_headers).json()
    assert _ranked(everywhere) == [(1, "Carla", 1000), (2, "Ana", 730), (3, "Beto", 140)]
    assert client.get(f"{API}/ranking?path_id=999", headers=admin_headers).status_code == 404


def test_points_earned_by_learning_show_up_right_away(client, db, admin, admin_headers):
    course = _course(db, admin, "Curso", modules=2)
    ana = _learner(db)
    _enroll(client, admin_headers, course, ana)
    learner = auth_headers(client, ana.email)

    _complete(client, learner, db, course)

    body = client.get(f"{API}/ranking", headers=admin_headers).json()
    assert body["items"] == [{"rank": 1, "user": {"id": ana.id, "name": "Ana", "profile": None},
                              "points": 2 * 20 + 100, "completed_courses": 1}]
    mine = client.get(f"{API}/learn/ranking", headers=learner).json()
    assert mine["me"] == {"rank": 1, "points": 140} and mine["items"][0]["is_me"]


# ── What learners see ─────────────────────────────────────────────────


def _crowd(db) -> list[User]:
    """Eleven people with 200, 190 ... 100 points today."""
    names = ["Ana Pérez", "Beto Ruiz", "Carla Díaz", "Dora Gil", "Eva Luna", "Fede Mora", "Gina Paz", "Hugo Sol",
             "Iris Vega", "Juan Toro", "Kika Mar"]
    crowd = _people(db, *names)
    for place, person in enumerate(crowd):
        _earn(db, person, 200 - 10 * place)
    return crowd


def test_learners_see_the_top_ten_and_their_own_place(client, db, admin_headers):
    _crowd(db)
    me = _learner(db, "yo@test.dev")
    me.name = "Abril (Abi) Ruiz"
    db.commit()
    _earn(db, me, 50)
    learner = auth_headers(client, me.email)

    body = client.get(f"{API}/learn/ranking", headers=learner).json()

    assert (body["visible"], body["period"], body["path"], body["total_people"]) == (True, "today", None, 12)
    assert body["me"] == {"rank": 12, "points": 50}
    assert len(body["items"]) == 10 and not any(item["is_me"] for item in body["items"])
    assert body["items"][0] == {"rank": 1, "name": "Ana Pérez", "initials": "AP", "points": 200, "is_me": False}

    _earn(db, me, 60)  # 110: tied with the tenth, and first of the two by name
    _settings(client, admin_headers, ranking={"show_full_names": False})
    body = client.get(f"{API}/learn/ranking", headers=learner).json()
    assert body["me"] == {"rank": 10, "points": 110}
    assert body["items"][0] == {"rank": 1, "name": "Ana P.", "initials": "AP", "points": 200, "is_me": False}
    assert body["items"][-1] == {"rank": 10, "name": "Abril A.", "initials": "AA", "points": 110, "is_me": True}


def test_names_are_shortened_to_the_first_and_an_initial():
    assert ranking.short_name("Ana María Pérez") == "Ana M."
    assert ranking.short_name("  ana   pérez ") == "ana P."
    assert ranking.short_name("Cher") == "Cher"
    assert ranking.short_name("Luis - Ortega") == "Luis O."  # punctuation is no word
    assert [ranking.initials(name) for name in ("ana pérez", "Cher", "¡Hola! Mundo", "", "-")] == [
        "AP", "C", "HM", "?", "?"]


def test_a_hidden_ranking_shows_nothing(client, db, admin_headers):
    _crowd(db)
    me = _learner(db, "yo@test.dev")
    _earn(db, me, 50)
    _settings(client, admin_headers, ranking={"visible_to_learners": False})

    body = client.get(f"{API}/learn/ranking?period=week", headers=auth_headers(client, me.email)).json()

    assert body == {"visible": False, "period": "week", "starts_at": body["starts_at"], "path": None, "items": [],
                    "me": None, "total_people": 0}
    assert client.get(f"{API}/ranking", headers=admin_headers).json()["total_people"] == 12  # admins still see it


def test_learners_only_see_the_ranking_of_their_published_paths(client, db, admin, admin_headers):
    course = _course(db, admin, "Curso")
    mine = _path(client, admin_headers, [course], title="Mía")
    others = _path(client, admin_headers, [course], title="De otros")
    draft = _path(client, admin_headers, [course], title="Borrador", publish=False)
    me, ana = _learner(db, "yo@test.dev"), _learner(db, "ana@test.dev")
    _assign(client, admin_headers, mine, me, ana)
    _assign(client, admin_headers, others, ana)
    _assign(client, admin_headers, draft, me)
    _earn(db, ana, 30, course=course)
    learner = auth_headers(client, me.email)

    allowed = client.get(f"{API}/learn/ranking", headers=learner, params={"path_id": mine["id"]})

    assert allowed.status_code == 200 and allowed.json()["path"] == {"id": mine["id"], "title": "Mía"}
    assert [(item["name"], item["points"]) for item in allowed.json()["items"]] == [("Ana", 30)]
    for path_id in (others["id"], draft["id"], 999):
        refused = client.get(f"{API}/learn/ranking", headers=learner, params={"path_id": path_id})
        assert refused.status_code == 403 and refused.json() == {"detail": "Solo puedes ver el ranking de tus rutas"}
    db.query(LearningPath).filter(LearningPath.id == mine["id"]).update({"status": "archived"})
    db.commit()
    assert client.get(f"{API}/learn/ranking", headers=learner, params={"path_id": mine["id"]}).status_code == 403
