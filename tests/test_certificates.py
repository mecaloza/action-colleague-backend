"""Certificates: issued on completion by the platform's rules, verified by their code, drawn as a PDF, and the admin's
registry (issue by hand, revoke, reissue)."""

import copy
import re
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from app.core.config import get_settings
from app.db.models import Activity, Certificate, Course, Enrollment, Evaluation, Job, Module, ModuleProgress
from app.schemas.settings import PlatformSettings
from app.services import certificate_pdf, certificates
from app.worker import queue
from tests.conftest import auth_headers
from tests.test_activities_learning import _choose, _start
from tests.test_activity_spec import DECISION
from tests.test_learning_api import QUESTIONS, _perfect_answers
from tests.test_paths import _assign, _complete, _course, _modules, _path, _person

API = "/api/v1"
CODE = re.compile(r"^RC-[0-9A-HJKMNP-TV-Z]{4}-[0-9A-HJKMNP-TV-Z]{4}$")
API_URL, APP_URL = "http://testserver/api/v1", "https://app.test"


@pytest.fixture(autouse=True)
def links(monkeypatch):
    """Absolute links the test client can follow (the PDF) and a known web app (the verification page)."""
    monkeypatch.setattr(get_settings(), "public_api_url", API_URL)
    monkeypatch.setattr(get_settings(), "public_app_url", APP_URL)


@pytest.fixture
def admin_headers(client, admin):
    return auth_headers(client, admin.email)


def _settings(client, headers, **sections) -> dict:
    response = client.patch(f"{API}/settings", headers=headers, json=sections)
    assert response.status_code == 200, response.text
    return response.json()


def _learner(db, email: str = "ana@test.dev"):
    return _person(db, email, can_log_in=True)


def _enroll(client, headers, course: Course, *users) -> None:
    response = client.post(f"{API}/courses/{course.id}/participants", headers=headers,
                           json={"user_ids": [user.id for user in users]})
    assert response.status_code == 200, response.text


def _issued(db) -> list[Certificate]:
    db.expire_all()
    return db.query(Certificate).order_by(Certificate.id).all()


def _relative(url: str) -> str:
    assert url.startswith(API_URL)
    return API + url.removeprefix(API_URL)


def _with_quiz(db, module: Module) -> Evaluation:
    evaluation = Evaluation(module_id=module.id, spec=QUESTIONS, max_attempts=3, passing_score=70)
    db.add(evaluation)
    db.commit()
    return evaluation


def _answer(client, headers, module: Module, evaluation: Evaluation, correct: int = 5):
    answers = _perfect_answers(evaluation.id)[:correct]  # the rest unanswered: wrong
    response = client.post(f"{API}/learn/modules/{module.id}/quiz/attempts", headers=headers, json={"answers": answers})
    assert response.status_code == 200, response.text
    return response.json()


# ── Issued on completion ──────────────────────────────────────────────


def test_completing_a_course_issues_its_certificate(client, db, admin, admin_headers):
    _settings(client, admin_headers, general={"company_name": "Acme Logística"},
              certificates={"signer_name": "Laura Gómez", "signer_title": "Directora de Formación"})
    course = _course(db, admin, "Seguridad vial", modules=2)
    for module in _modules(db, course):
        module.duration_seconds = 2700  # two videos of 45 minutes
    db.commit()
    ana = _learner(db)
    _enroll(client, admin_headers, course, ana)
    learner = auth_headers(client, ana.email)
    first, last = _modules(db, course)

    halfway = client.post(f"{API}/learn/modules/{first.id}/complete", headers=learner).json()
    finished = client.post(f"{API}/learn/modules/{last.id}/complete", headers=learner).json()

    assert halfway["certificate"] is None
    (certificate,) = _issued(db)
    code = certificate.code
    assert CODE.match(code)
    ref = {"id": certificate.id, "code": code, "pdf_url": f"{API_URL}/public/certificates/{code}/pdf",
           "verify_url": f"{APP_URL}/verify/{code}"}
    assert finished["course_completed"] and finished["certificate"] == ref
    assert (certificate.kind, certificate.user_id, certificate.course_id, certificate.path_id) == (
        "course", ana.id, course.id, None)
    assert (certificate.recipient_name, certificate.title, certificate.score) == ("Ana", "Seguridad vial", None)
    assert certificate.expires_at is None and certificate.revoked_at is None
    completed_at = db.get(Enrollment, db.query(Enrollment.id).scalar()).completed_at
    assert certificate.details == {
        "hours": 1.5, "modules": 2, "average_score": None,
        "courses": [{"title": "Seguridad vial",
                     "completed_at": completed_at.replace(tzinfo=timezone.utc).isoformat()}],
        "company_name": "Acme Logística", "signer_name": "Laura Gómez", "signer_title": "Directora de Formación",
    }

    # The learner's views link it.
    (listed,) = client.get(f"{API}/learn/courses", headers=learner).json()
    assert listed["certificate"] == ref
    assert client.get(f"{API}/learn/courses/{course.id}", headers=learner).json()["certificate"] == ref
    (mine,) = client.get(f"{API}/learn/certificates", headers=learner).json()
    assert mine == {**ref, "kind": "course", "title": "Seguridad vial", "issued_at": mine["issued_at"],
                    "expires_at": None, "status": "valid", "course_id": course.id, "path_id": None, "score": None}
    by_admin = client.get(f"{API}/users/{ana.id}/courses", headers=admin_headers).json()
    assert by_admin[0]["certificate"] == ref

    # A snapshot: later changes to the course or the parameters never touch it.
    client.patch(f"{API}/courses/{course.id}", headers=admin_headers, json={"title": "Otro título"})
    _settings(client, admin_headers, certificates={"signer_name": "Otra persona"})
    db.expire_all()
    assert (certificate.title, certificate.details["signer_name"]) == ("Seguridad vial", "Laura Gómez")


def test_the_average_of_the_best_quiz_scores_goes_on_the_certificate(client, db, admin, admin_headers):
    course = _course(db, admin, "Llantas", modules=2)
    first, last = _modules(db, course)
    quizzes = [_with_quiz(db, first), _with_quiz(db, last)]
    ana = _learner(db)
    _enroll(client, admin_headers, course, ana)
    learner = auth_headers(client, ana.email)

    assert not _answer(client, learner, first, quizzes[0], correct=3)["passed"]  # 60: the best one counts below
    _answer(client, learner, first, quizzes[0], correct=4)  # 80
    result = _answer(client, learner, last, quizzes[1])  # 100, and the course is completed

    (certificate,) = _issued(db)
    assert result["course_completed"] and result["certificate"]["id"] == certificate.id
    assert certificate.score == 90.0 and certificate.details["average_score"] == 90.0


def test_the_last_move_of_the_courses_activity_issues_it(client, db, admin, admin_headers):
    course = _course(db, admin, "Atención al cliente")
    (module,) = _modules(db, course)
    activity = Activity(module_id=module.id, order=1, type="decision", title="Cliente", intro="",
                        spec=copy.deepcopy(DECISION), status="ready")
    db.add(activity)
    db.commit()
    ana = _learner(db)
    _enroll(client, admin_headers, course, ana)
    learner = auth_headers(client, ana.email)

    play = _start(client, learner, activity.id)
    last = _choose(client, learner, play, "Le doy").json()  # it ends the conversation

    (certificate,) = _issued(db)
    assert last["done"] and last["course_completed"] and last["certificate"]["code"] == certificate.code
    finished = client.post(f"{API}/learn/activity-attempts/{play['attempt_id']}/finish", headers=learner).json()
    assert finished["certificate"]["id"] == certificate.id  # told again, never issued twice


def test_an_admins_change_completing_many_courses_issues_all_their_certificates(client, db, admin, admin_headers):
    course = _course(db, admin, "Inducción", modules=2)
    done, pending = _modules(db, course)
    people = [_learner(db, f"p{i}@test.dev") for i in range(3)]
    _enroll(client, admin_headers, course, *people)
    for person in people[:2]:
        client.post(f"{API}/learn/modules/{done.id}/complete", headers=auth_headers(client, person.email))

    assert client.delete(f"{API}/modules/{pending.id}", headers=admin_headers).status_code == 204

    assert sorted(certificate.user_id for certificate in _issued(db)) == [people[0].id, people[1].id]
    assert all(certificate.details["modules"] == 1 for certificate in _issued(db))


def test_a_reopened_course_keeps_its_certificate_and_never_gets_another(client, db, admin, admin_headers):
    course = _course(db, admin, "Seguridad")
    ana = _learner(db)
    _enroll(client, admin_headers, course, ana)
    learner = auth_headers(client, ana.email)
    _complete(client, learner, db, course)
    (first,) = _issued(db)

    def add_module_and_complete():
        client.post(f"{API}/courses/{course.id}/modules", headers=admin_headers, json={"title": "Nuevo",
                                                                                        "content_text": "x"})
        db.expire_all()
        assert db.query(Enrollment).one().status == "in_progress"  # reopened...
        return _complete(client, learner, db, course)

    again = add_module_and_complete()
    assert [c.id for c in _issued(db)] == [first.id] and _issued(db)[0].revoked_at is None  # ...never revoked
    assert again["certificate"]["id"] == first.id

    revoked = client.post(f"{API}/certificates/{first.id}/revoke", headers=admin_headers, json={"reason": "Error"})
    assert revoked.status_code == 200
    third = add_module_and_complete()
    assert [c.id for c in _issued(db)] == [first.id] and third["certificate"] is None  # revoked: none by itself


def test_a_minimum_average_score_holds_back_course_certificates(client, db, admin, admin_headers, worker, storage):
    _settings(client, admin_headers, certificates={"min_score": 90})
    quizzed, plain = _course(db, admin, "Con evaluación"), _course(db, admin, "Sin evaluación")
    (module,) = _modules(db, quizzed)
    evaluation = _with_quiz(db, module)
    ana = _learner(db)
    _enroll(client, admin_headers, quizzed, ana)
    _enroll(client, admin_headers, plain, ana)
    learner = auth_headers(client, ana.email)

    passed = _answer(client, learner, module, evaluation, correct=4)  # 80: passes the quiz (70), not the minimum
    _complete(client, learner, db, plain)

    assert passed["course_completed"] and passed["certificate"] is None
    assert [c.course_id for c in _issued(db)] == [plain.id]  # no quiz in it: nothing to fall short of
    # Lowered: the maintenance pass after the next deploy issues the missing one, by the same rules.
    _settings(client, admin_headers, certificates={"min_score": 80})
    queue.enqueue(db, "legacy.migrate", dedupe_key="legacy-migrate")
    worker()
    assert sorted(c.course_id for c in _issued(db)) == sorted([plain.id, quizzed.id])


@pytest.mark.parametrize(("enabled", "chosen", "issued"), [
    (True, None, True), (True, False, False), (True, True, True),
    (False, None, False), (False, True, True), (False, False, False),
])
def test_a_course_can_choose_whether_it_gives_one(client, db, admin, admin_headers, enabled, chosen, issued):
    _settings(client, admin_headers, certificates={"enabled": enabled})
    course = _course(db, admin, "Curso")
    patched = client.patch(f"{API}/courses/{course.id}", headers=admin_headers,
                           json={"settings": {"certificate": chosen}})
    assert patched.json()["settings"]["certificate"] is chosen
    ana = _learner(db)
    _enroll(client, admin_headers, course, ana)

    finished = _complete(client, auth_headers(client, ana.email), db, course)

    assert bool(_issued(db)) is issued and (finished["certificate"] is not None) is issued


def test_completing_a_path_issues_its_certificate(client, db, admin, admin_headers):
    first, second = (_course(db, admin, title, modules=2) for title in ("Curso 1", "Curso 2"))
    path = _path(client, admin_headers, [first, second], title="Ruta de asesores")
    no_certificate = _path(client, admin_headers, [first], title="Sin certificado", certificate=False)
    ana = _learner(db)
    _assign(client, admin_headers, path, ana)
    _assign(client, admin_headers, no_certificate, ana)
    learner = auth_headers(client, ana.email)

    after_first = _complete(client, learner, db, first)
    after_second = _complete(client, learner, db, second)

    path_certificate = next(c for c in _issued(db) if c.kind == "path")
    kinds = sorted((c.kind, c.course_id or c.path_id) for c in _issued(db))
    assert kinds == sorted([("course", first.id), ("course", second.id), ("path", path["id"])])
    # The first course completed the path without a certificate: none for it.
    assert after_first["path_completed"] == {"id": no_certificate["id"], "title": "Sin certificado",
                                             "certificate": None}
    assert after_second["path_completed"]["certificate"]["code"] == path_certificate.code
    assert after_second["certificate"]["code"] != path_certificate.code  # the course's own
    assert (path_certificate.title, path_certificate.recipient_name) == ("Ruta de asesores", "Ana")
    details = path_certificate.details
    assert [course["title"] for course in details["courses"]] == ["Curso 1", "Curso 2"]
    assert all(course["completed_at"] for course in details["courses"]) and details["modules"] == 4
    paths = {p["id"]: p for p in client.get(f"{API}/learn/paths", headers=learner).json()}
    assert paths[path["id"]]["certificate"]["code"] == path_certificate.code
    assert paths[no_certificate["id"]]["certificate"] is None


def test_certificates_expire_after_the_validity(client, db, admin, admin_headers):
    _settings(client, admin_headers, certificates={"validity_months": 12})
    course = _course(db, admin, "Curso")
    ana = _learner(db)
    _enroll(client, admin_headers, course, ana)
    learner = auth_headers(client, ana.email)
    _complete(client, learner, db, course)
    (certificate,) = _issued(db)
    issued_at = certificate.issued_at.replace(tzinfo=timezone.utc)
    assert certificate.expires_at.replace(tzinfo=timezone.utc) == certificates.add_months(issued_at, 12)

    certificate.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
    db.commit()

    assert client.get(f"{API}/learn/certificates", headers=learner).json()[0]["status"] == "expired"
    public = client.get(f"{API}/public/certificates/{certificate.code}").json()
    assert public["status"] == "expired"
    assert client.get(f"{API}/public/certificates/{certificate.code}/pdf").status_code == 200  # still its record
    # Still in force (not revoked): the learner's course links it, and no second one is issued.
    assert client.get(f"{API}/learn/courses", headers=learner).json()[0]["certificate"]["id"] == certificate.id


def test_months_are_added_on_the_calendar():
    moment = datetime(2026, 1, 31, 12, tzinfo=timezone.utc)
    assert certificates.add_months(moment, 1) == datetime(2026, 2, 28, 12, tzinfo=timezone.utc)
    assert certificates.add_months(moment, 13) == datetime(2027, 2, 28, 12, tzinfo=timezone.utc)
    assert certificates.add_months(datetime(2027, 12, 15, tzinfo=timezone.utc), 1).date().isoformat() == "2028-01-15"
    assert certificates.add_months(datetime(2028, 1, 31, tzinfo=timezone.utc), 1).day == 29  # a leap year


# ── Verification and PDF ──────────────────────────────────────────────


def _completed_certificate(client, db, admin, admin_headers, email="ana@test.dev") -> Certificate:
    course = db.query(Course).filter(Course.title == "Seguridad vial").first() or _course(db, admin, "Seguridad vial")
    person = _learner(db, email)
    _enroll(client, admin_headers, course, person)
    _complete(client, auth_headers(client, person.email), db, course)
    return next(c for c in _issued(db) if c.user_id == person.id)


def test_anyone_with_the_code_verifies_it(client, db, admin, admin_headers):
    certificate = _completed_certificate(client, db, admin, admin_headers)
    code = certificate.code

    public = client.get(f"{API}/public/certificates/{code}")  # no session

    assert public.status_code == 200
    body = public.json()
    assert body == {**body, "code": code, "status": "valid", "kind": "course", "recipient_name": "Ana",
                    "title": "Seguridad vial", "expires_at": None, "issuer_name": "Rubinstein Colleague",
                    "pdf_url": f"{API_URL}/public/certificates/{code}/pdf"}
    assert body["details"]["modules"] == 1 and body["details"]["courses"][0]["title"] == "Seguridad vial"
    # Typed by hand: any case, without dashes, O for 0 and I or L for 1.
    typed = code.lower().replace("-", "").replace("0", "o").replace("1", "l")
    assert client.get(f"{API}/public/certificates/{typed}").json()["code"] == code
    for unknown in ("RC-0000-0000", "nada", "RC-UUUU-UUUU"):
        assert client.get(f"{API}/public/certificates/{unknown}").status_code == 404

    client.post(f"{API}/certificates/{certificate.id}/revoke", headers=admin_headers, json={})
    assert client.get(f"{API}/public/certificates/{code}").json()["status"] == "revoked"


def test_the_pdf(client, db, admin, admin_headers):
    _settings(client, admin_headers, general={"company_name": "Acme"},
              certificates={"signer_name": "Laura Gómez", "signer_title": "Directora"})
    certificate = _completed_certificate(client, db, admin, admin_headers)
    pdf_url = client.get(f"{API}/public/certificates/{certificate.code}").json()["pdf_url"]

    response = client.get(_relative(pdf_url))  # no session

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"] == f'inline; filename="certificado-{certificate.code}.pdf"'
    assert response.content.startswith(b"%PDF") and len(response.content) < 2_000_000
    started = time.perf_counter()
    certificate_pdf.render(certificate, PlatformSettings())
    assert time.perf_counter() - started < 2

    client.post(f"{API}/certificates/{certificate.id}/revoke", headers=admin_headers)
    gone = client.get(_relative(pdf_url))
    assert gone.status_code == 410 and gone.json() == {"detail": "Este certificado fue revocado."}
    assert client.get(f"{API}/public/certificates/RC-0000-0000/pdf").status_code == 404


def test_too_many_pdfs_at_once_ask_to_try_again(client, db, admin, admin_headers, monkeypatch):
    from app.api.routes import certificates as routes

    certificate = _completed_certificate(client, db, admin, admin_headers)
    busy = threading.BoundedSemaphore(1)
    busy.acquire()  # every slot drawing another one
    monkeypatch.setattr(routes, "_PDF_SLOTS", busy)
    monkeypatch.setattr(routes, "PDF_WAIT_SECONDS", 0.05)

    response = client.get(f"{API}/public/certificates/{certificate.code}/pdf")

    assert response.status_code == 429 and "intenta de nuevo" in response.json()["detail"]


def test_the_page_says_what_was_completed_in_spanish():
    certificate = Certificate(
        code="RC-AB12-CD34", kind="path", user_id=1, recipient_name="Ana", title="Ruta de asesores",
        issued_at=datetime(2026, 10, 8, 3, 0, tzinfo=timezone.utc),  # still the 7th in Mexico City
        expires_at=datetime(2027, 10, 8, 3, 0, tzinfo=timezone.utc),
        details={"hours": 0.2, "modules": 3, "average_score": 86.66, "company_name": "",
                 "courses": [{"title": f"Curso {i}"} for i in range(1, 4)]},
    )
    page = certificate_pdf.texts(certificate, PlatformSettings())

    assert page.eyebrow == "CERTIFICADO DE RUTA COMPLETADA" and page.lead == "completó la ruta de aprendizaje"
    assert page.issuer == "Rubinstein Colleague"
    assert page.facts == "Emitido el 7 de octubre de 2026 · 12 minutos · 3 módulos · Calificación promedio: 86,7 %"
    assert page.courses == "Cursos: Curso 1 · Curso 2 · Curso 3"
    assert page.validity == "Vigente hasta el 7 de octubre de 2027"
    assert page.verify_url == f"{APP_URL}/verify/RC-AB12-CD34"
    assert certificate_pdf.course_list([f"Curso con un nombre largo {i}" for i in range(30)]).endswith(" más")
    assert certificate_pdf.duration(1) == "1 hora" and certificate_pdf.duration(2.25) == "2,2 horas"
    assert certificate_pdf.duration(0) is None


def test_learners_only_see_their_own_certificates(client, db, admin, admin_headers):
    theirs = {email: _completed_certificate(client, db, admin, admin_headers, email).code
              for email in ("ana@test.dev", "beto@test.dev")}

    for email, code in theirs.items():
        mine = client.get(f"{API}/learn/certificates", headers=auth_headers(client, email)).json()
        assert [certificate["code"] for certificate in mine] == [code]
    beto = auth_headers(client, "beto@test.dev")
    assert client.get(f"{API}/certificates", headers=beto).status_code == 403


# ── The admin's registry ──────────────────────────────────────────────


def test_the_registry_filters_and_pages(client, db, admin, admin_headers):
    first, second = _course(db, admin, "Curso 1"), _course(db, admin, "Curso 2")
    path = _path(client, admin_headers, [first], title="Ruta", sequential=False)
    ana, beto = _learner(db, "ana@test.dev"), _learner(db, "beto@test.dev")
    _assign(client, admin_headers, path, ana)
    _enroll(client, admin_headers, first, beto)
    _enroll(client, admin_headers, second, ana)
    for person, course in ((ana, first), (ana, second), (beto, first)):
        _complete(client, auth_headers(client, person.email), db, course)
    issued = {(c.user_id, c.kind, c.course_id or c.path_id): c for c in _issued(db)}
    assert len(issued) == 4  # ana: two courses and the path; beto: one course
    revoked, expired = issued[(beto.id, "course", first.id)], issued[(ana.id, "course", second.id)]
    client.post(f"{API}/certificates/{revoked.id}/revoke", headers=admin_headers, json={"reason": "Duplicado"})
    expired.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
    db.commit()

    def codes(**params) -> list[str]:
        page = client.get(f"{API}/certificates", headers=admin_headers, params=params).json()
        assert page["total"] == len(page["items"]) or "limit" in params
        return sorted(item["code"] for item in page["items"])

    def of(*certs) -> list[str]:
        return sorted(c.code for c in certs)

    assert codes(user_id=beto.id) == of(revoked)
    assert codes(course_id=first.id) == of(issued[(ana.id, "course", first.id)], revoked)
    assert codes(path_id=path["id"]) == of(issued[(ana.id, "path", path["id"])])
    assert codes(status="revoked") == of(revoked)
    assert codes(status="expired") == of(expired)
    assert len(codes(status="valid")) == 2
    assert codes(q="beto@TEST") == of(revoked)  # the person's email, any case
    assert codes(q=expired.code.lower()) == of(expired)
    assert codes(q="Ruta") == of(issued[(ana.id, "path", path["id"])])
    page = client.get(f"{API}/certificates", headers=admin_headers, params={"limit": 2, "offset": 1}).json()
    assert page["total"] == 4 and len(page["items"]) == 2
    item = next(i for i in client.get(f"{API}/certificates", headers=admin_headers).json()["items"]
                if i["id"] == revoked.id)
    assert item == {**item, "status": "revoked", "recipient_name": "Beto", "user_id": beto.id,
                    "revoked_reason": "Duplicado"} and item["revoked_at"]
    assert client.get(f"{API}/certificates", headers=admin_headers, params={"status": "x"}).status_code == 422


def test_issuing_by_hand(client, db, admin, admin_headers):
    _settings(client, admin_headers, certificates={"enabled": False})  # none by themselves
    course, other = _course(db, admin, "Curso"), _course(db, admin, "Otro")
    path = _path(client, admin_headers, [course], title="Ruta")
    ana = _learner(db)
    _assign(client, admin_headers, path, ana)
    _enroll(client, admin_headers, other, ana)

    def issue(**body):
        return client.post(f"{API}/certificates", headers=admin_headers, json={"user_id": ana.id, **body})

    for body, message in (
        ({"course_id": course.id}, "La persona aún no completa el curso."),
        ({"path_id": path["id"]}, "La persona aún no completa la ruta."),
        ({}, "Indica un curso o una ruta (solo uno de los dos)."),
        ({"course_id": course.id, "path_id": path["id"]}, "Indica un curso o una ruta (solo uno de los dos)."),
        ({"course_id": 999}, "El curso no existe."),
        ({"path_id": 999}, "La ruta no existe."),
    ):
        response = issue(**body)
        assert response.status_code == 422 and response.json() == {"detail": message}, body
    assert client.post(f"{API}/certificates", headers=admin_headers,
                       json={"user_id": 999, "course_id": course.id}).json() == {"detail": "La persona no existe."}

    _complete(client, auth_headers(client, ana.email), db, course)
    assert _issued(db) == []
    created = issue(course_id=course.id)
    assert created.status_code == 201
    assert created.json() == {**created.json(), "kind": "course", "status": "valid", "user_id": ana.id,
                              "course_id": course.id, "recipient_name": "Ana", "revoked_at": None}
    again = issue(course_id=course.id)
    assert again.status_code == 200 and again.json()["id"] == created.json()["id"]  # never a second one
    by_path = issue(path_id=path["id"])
    assert by_path.status_code == 201 and by_path.json()["path_id"] == path["id"]
    assert len(_issued(db)) == 2


def test_revoking_and_reissuing(client, db, admin, admin_headers):
    _settings(client, admin_headers, certificates={"signer_name": "Laura"})
    certificate = _completed_certificate(client, db, admin, admin_headers)
    learner = auth_headers(client, "ana@test.dev")
    url = f"{API}/certificates/{certificate.id}"

    revoked = client.post(f"{url}/revoke", headers=admin_headers, json={"reason": "  Emitido por error "}).json()
    assert revoked == {**revoked, "status": "revoked", "revoked_reason": "Emitido por error"} and revoked["revoked_at"]
    assert client.get(f"{API}/learn/courses", headers=learner).json()[0]["certificate"] is None
    assert client.post(f"{url}/revoke", headers=admin_headers).json()["revoked_at"] == revoked["revoked_at"]

    _settings(client, admin_headers, certificates={"signer_name": "Marta"})
    reissued = client.post(f"{url}/reissue", headers=admin_headers)
    assert reissued.status_code == 201
    new = reissued.json()
    assert new["status"] == "valid" and new["code"] != certificate.code and new["course_id"] == certificate.course_id
    db.expire_all()
    assert db.get(Certificate, new["id"]).details["signer_name"] == "Marta"  # a fresh snapshot
    assert db.get(Certificate, certificate.id).revoked_reason == "Emitido por error"  # revoked already: as it was
    assert client.get(f"{API}/learn/courses", headers=learner).json()[0]["certificate"]["id"] == new["id"]

    # The old one again, while the new one is in force: reissue that one instead.
    conflict = client.post(f"{url}/reissue", headers=admin_headers)
    assert conflict.status_code == 409 and new["code"] in conflict.json()["detail"]
    newest = client.post(f"{API}/certificates/{new['id']}/reissue", headers=admin_headers).json()
    db.expire_all()
    assert db.get(Certificate, new["id"]).revoked_reason == f"Reemitido como {newest['code']}"
    assert client.post(f"{API}/certificates/999/reissue", headers=admin_headers).status_code == 404

    # Its course deleted, it stays verifiable but can't be reissued.
    client.delete(f"{API}/courses/{certificate.course_id}", headers=admin_headers)
    db.expire_all()
    assert db.get(Certificate, newest["id"]).course_id is None
    assert client.get(f"{API}/public/certificates/{newest['code']}").json()["status"] == "valid"
    gone = client.post(f"{API}/certificates/{newest['id']}/reissue", headers=admin_headers)
    assert gone.status_code == 409 and gone.json() == {
        "detail": "El curso de este certificado ya no existe: no se puede reemitir."}


def test_deleting_a_path_keeps_its_certificates(client, db, admin, admin_headers):
    course = _course(db, admin, "Curso")
    path = _path(client, admin_headers, [course], title="Ruta")
    ana = _learner(db)
    _assign(client, admin_headers, path, ana)
    _complete(client, auth_headers(client, ana.email), db, course)
    path_certificate = next(c for c in _issued(db) if c.kind == "path")

    assert client.delete(f"{API}/paths/{path['id']}", headers=admin_headers).status_code == 204

    db.expire_all()
    assert db.get(Certificate, path_certificate.id).path_id is None
    assert client.get(f"{API}/public/certificates/{path_certificate.code}").json()["title"] == "Ruta"


def test_the_maintenance_pass_issues_what_is_missing_once(client, db, admin, admin_headers, worker, storage):
    _settings(client, admin_headers, certificates={"enabled": False})
    course = _course(db, admin, "Curso")
    path = _path(client, admin_headers, [course], title="Ruta")
    ana, beto = _learner(db, "ana@test.dev"), _learner(db, "beto@test.dev")
    _assign(client, admin_headers, path, ana, beto)
    for person in (ana, beto):
        _complete(client, auth_headers(client, person.email), db, course)
    assert _issued(db) == []  # completed while certificates were off
    _settings(client, admin_headers, certificates={"enabled": True})

    queue.enqueue(db, "legacy.migrate", dedupe_key="legacy-migrate")
    worker()

    db.expire_all()
    job = db.query(Job).filter(Job.type == "legacy.migrate").one()
    assert job.status == "succeeded" and job.result["certificates_issued"] == 4
    assert sorted((c.user_id, c.kind) for c in _issued(db)) == sorted(
        [(ana.id, "course"), (ana.id, "path"), (beto.id, "course"), (beto.id, "path")])
    revoked = next(c for c in _issued(db) if c.user_id == beto.id and c.kind == "path")
    client.post(f"{API}/certificates/{revoked.id}/revoke", headers=admin_headers)

    assert certificates.backfill(db) == 0  # idempotent: nothing again, not even the revoked one
    assert len(_issued(db)) == 4


def test_codes_typed_by_hand_are_normalized():
    assert certificates.normalize_code(" rc-7k2m-9qxd ") == "RC-7K2M-9QXD"
    assert certificates.normalize_code("RC7K2M9QXD") == "RC-7K2M-9QXD"
    assert certificates.normalize_code("rc-o1il-2345") == "RC-0111-2345"
    assert certificates.normalize_code("7K2M9QXD") == "RC-7K2M-9QXD"
    for wrong in ("", "RC-7K2M", "RC-7K2M-9QXDX", "RC-UUUU-0000", "XX-7K2M-9QXD"):
        assert certificates.normalize_code(wrong) is None
    assert all(CODE.match(certificates.new_code()) for _ in range(200))


def test_each_course_certificate_averages_only_its_own_scores(db, admin, collaborator):
    """Two courses of one learner: each certificate averages the scores of its own course."""
    one, two = _course(db, admin, "Uno"), _course(db, admin, "Dos")
    enrollments = [Enrollment(user_id=collaborator.id, course_id=c.id, status="completed",
                              completed_at=datetime.now(timezone.utc)) for c in (one, two)]
    db.add_all(enrollments)
    db.flush()
    for enrollment, course, score in ((enrollments[0], one, 70.0), (enrollments[1], two, 100.0)):
        (module,) = _modules(db, course)
        db.add(ModuleProgress(enrollment_id=enrollment.id, module_id=module.id, completed=True, passed=True,
                              score=score, attempts=1))
    db.commit()

    assert certificates.backfill(db) == 2

    assert {c.course_id: c.score for c in _issued(db)} == {one.id: 70.0, two.id: 100.0}
