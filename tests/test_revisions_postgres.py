"""Revisions on a real PostgreSQL: applies and saves at the same time (row locks), what a save leaves free for learners
and what deleting a module or course takes."""

import copy
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.db.models import Activity, Course, Enrollment, Evaluation, Job, MediaAsset, Module, ModuleRevision
from app.services import activity_revisions, quiz_revisions, revisions
from tests.conftest import auth_headers
from tests.test_activity_spec import DECISION
from tests.test_learning_api import QUESTIONS
from tests.test_revisions import OUTLINE, READING, RENDER, SCENES


@pytest.fixture
def engine(pg_engine):  # conftest's db, session_factory, client and worker then run on Postgres
    return pg_engine


def _module(db, admin, *, video: bool = False, course: Course | None = None) -> Module:
    if course is None:
        course = Course(title="Llantas", status="draft", source="ai", settings={}, created_by=admin.id)
        db.add(course)
        db.flush()
    module = Module(course_id=course.id, title="Presión", order=len(course.modules) + 1, source="ai",
                    content_text=READING, storyboard={"outline": OUTLINE, "scenes": copy.deepcopy(SCENES),
                                                      "render": RENDER})
    if video:
        asset = MediaAsset(kind="video", status="ready", bucket="b", path="v.mp4", course_id=course.id)
        db.add(asset)
        db.flush()
        module.video_asset_id = asset.id
    db.add(module)
    db.commit()
    return module


def _proposed(client, headers, worker, module_id, observation="Haz el título más claro") -> int:
    response = client.post(f"/api/v1/modules/{module_id}/revisions", headers=headers,
                           json={"observation": observation, "scene_ids": ["s2"]})
    assert response.status_code == 202, response.text
    worker()
    return response.json()["revision"]["id"]


@pytest.fixture
def slow_merge(monkeypatch):
    """Widen the window between reading the script and committing the change."""
    three_way = revisions.three_way

    def slow(*args):
        merged = three_way(*args)
        time.sleep(0.3)
        return merged

    monkeypatch.setattr(revisions, "three_way", slow)


def _apply_at_once(client, headers, revision_ids: list[int], **payload) -> list:
    with ThreadPoolExecutor(len(revision_ids)) as pool:
        return list(pool.map(
            lambda revision_id: client.post(f"/api/v1/revisions/{revision_id}/apply", headers=headers, json=payload),
            revision_ids,
        ))


def test_two_proposals_for_the_same_scene_applied_at_once(client, db, admin, worker, slow_merge):
    headers = auth_headers(client, admin.email)
    module = _module(db, admin)
    first = db.get(ModuleRevision, _proposed(client, headers, worker, module.id))
    # Another proposal made from the same script for the same scene (asked from another tab, say).
    after = copy.deepcopy(first.after)
    after["scenes"][1]["slide"]["title"] = "Otro título"
    second = ModuleRevision(
        module_id=module.id, target="script", source="observation", status="proposed", observation="Otro",
        scope=first.scope, base_hash=first.base_hash, before=first.before, after=after, changes=first.changes,
        created_by=admin.id,
    )
    db.add(second)
    db.commit()

    responses = _apply_at_once(client, headers, [first.id, second.id])

    assert sorted(response.status_code for response in responses) == [200, 409]
    [winner] = [response.json()["revision"] for response in responses if response.status_code == 200]
    db.expire_all()
    title = db.get(Module, module.id).storyboard["scenes"][1]["slide"]["title"]
    assert title == winner["changes"]["scenes"][0]["fields"]["title"]["after"]
    assert db.query(ModuleRevision).filter(ModuleRevision.status == "applied").count() == 1


def test_applying_twice_at_once_applies_once_and_produces_one_video(client, db, admin, worker, slow_merge, caplog):
    headers = auth_headers(client, admin.email)
    module = _module(db, admin, video=True)
    revision_id = _proposed(client, headers, worker, module.id)

    with caplog.at_level(logging.INFO, logger="app.api.routes.revisions"):
        responses = _apply_at_once(client, headers, [revision_id, revision_id], render=True)

    assert [response.status_code for response in responses] == [200, 200]
    assert [record.getMessage() for record in caplog.records].count("revision_applied") == 1  # the other one waited
    assert len({response.json()["render_job"]["id"] for response in responses}) == 1
    assert db.query(Job).filter(Job.type == "video.render").count() == 1
    db.expire_all()
    assert db.get(Module, module.id).storyboard["scenes"][1]["slide"]["title"] == "Revisado: Cómo medir"


def test_deleting_a_module_or_its_course_takes_its_revisions(client, db, admin, worker):
    headers = auth_headers(client, admin.email)
    first = _module(db, admin)
    second = _module(db, admin, course=db.get(Course, first.course_id))
    for module in (first, second):
        assert client.post(f"/api/v1/revisions/{_proposed(client, headers, worker, module.id)}/apply",
                           headers=headers, json={}).status_code == 200

    assert client.delete(f"/api/v1/modules/{first.id}", headers=headers).status_code == 204
    assert {module_id for (module_id,) in db.query(ModuleRevision.module_id)} == {second.id}
    assert client.delete(f"/api/v1/courses/{first.course_id}", headers=headers).status_code == 204
    assert db.query(ModuleRevision).count() == 0


# ── The quiz and the activities ───────────────────────────────────────


def _content_module(db, admin) -> tuple[Module, Activity]:
    """A published course's module with a quiz and a published conversation (no script)."""
    course = Course(title="Seguridad", status="published", source="manual", settings={}, created_by=admin.id)
    db.add(course)
    db.flush()
    module = Module(course_id=course.id, title="EPP", order=1, source="text", content_text="Usa el casco.")
    db.add(module)
    db.flush()
    activity = Activity(module_id=module.id, order=1, type="decision", title="Cliente", intro="Escucha.",
                        spec=copy.deepcopy(DECISION), status="ready")
    db.add_all([Evaluation(module_id=module.id, spec=copy.deepcopy(QUESTIONS), max_attempts=3, passing_score=80),
                activity])
    db.commit()
    return module, activity


def test_a_quiz_or_activity_change_applied_twice_at_once_is_applied_once(client, db, admin, worker, monkeypatch,
                                                                        caplog):
    headers = auth_headers(client, admin.email)
    module, activity = _content_module(db, admin)
    for owner in (quiz_revisions, activity_revisions):  # widen the window between reading and committing
        merge = owner.three_way
        monkeypatch.setattr(owner, "three_way", lambda *args, merge=merge: (time.sleep(0.3), merge(*args))[1])
    proposals = []
    for body in ({"target": "quiz", "question_ids": ["q2"]}, {"target": "activity", "activity_id": activity.id}):
        response = client.post(f"/api/v1/modules/{module.id}/revisions", headers=headers,
                               json={"observation": "Más claro", **body})
        assert response.status_code == 202, response.text
        worker()
        proposals.append(response.json()["revision"]["id"])

    with caplog.at_level(logging.INFO, logger="app.api.routes.revisions"):
        responses = [_apply_at_once(client, headers, [revision_id, revision_id]) for revision_id in proposals]

    assert [[response.status_code for response in pair] for pair in responses] == [[200, 200], [200, 200]]
    assert [record.getMessage() for record in caplog.records].count("revision_applied") == 2  # the others waited
    db.expire_all()
    prompts = [question["prompt"] for question in quiz_revisions.content(db.get(Module, module.id))["questions"]]
    assert prompts.count("Revisado: El EPP es opcional") == 1 and len(prompts) == len(QUESTIONS)
    assert db.get(Activity, activity.id).version == 2
    assert db.query(Job).filter(Job.type == "activities.voice").count() == 1


def test_two_saves_of_an_activity_at_once_are_both_kept(client, db, admin, monkeypatch):
    headers = auth_headers(client, admin.email)
    _, activity = _content_module(db, admin)
    write = activity_revisions.write
    monkeypatch.setattr(activity_revisions, "write", lambda *args: (write(*args), time.sleep(0.3))[0])

    with ThreadPoolExecutor(2) as pool:
        responses = list(pool.map(
            lambda body: client.put(f"/api/v1/activities/{activity.id}", headers=headers, json=body),
            [{"title": "Uno"}, {"intro": "Dos."}],
        ))

    assert [response.status_code for response in responses] == [200, 200]
    db.expire_all()
    stored = db.get(Activity, activity.id)
    assert (stored.title, stored.intro, stored.version) == ("Uno", "Dos.", 3)  # a version each (2, without the lock)
    first, second = db.query(ModuleRevision).filter(ModuleRevision.activity_id == activity.id).order_by(
        ModuleRevision.id).all()
    assert second.before == first.after  # one after the other: each undoes exactly its own


def test_saving_the_quiz_never_waits_for_a_learner_answering_it(client, db, admin, collaborator, monkeypatch):
    """The save holds the module (NO KEY UPDATE) while it writes the quiz: a learner's attempt, which only references
    the module, goes on meanwhile (FOR UPDATE would make it wait for the save)."""
    headers = auth_headers(client, admin.email)
    module, _ = _content_module(db, admin)
    db.add(Enrollment(user_id=collaborator.id, course_id=module.course_id, status="assigned", progress_pct=0))
    db.commit()
    learner = auth_headers(client, collaborator.email)
    version = client.get(f"/api/v1/learn/modules/{module.id}/quiz", headers=learner).json()["version"]
    paused, release, saved = threading.Event(), threading.Event(), []
    write = quiz_revisions.write

    def slow_write(*args):
        paused.set()  # the save holds the module
        release.wait(5)
        return write(*args)

    monkeypatch.setattr(quiz_revisions, "write", slow_write)
    edited = [{**question, "explanation": "Editada."} for question in QUESTIONS]
    saving = threading.Thread(target=lambda: saved.append(client.put(
        f"/api/v1/modules/{module.id}/evaluation", headers=headers, json={"questions": edited})))
    saving.start()
    assert paused.wait(5)
    started = time.monotonic()
    answered = client.post(f"/api/v1/learn/modules/{module.id}/quiz/attempts", headers=learner,
                           json={"answers": [], "version": version})
    waited = time.monotonic() - started
    release.set()
    saving.join(15)

    assert answered.status_code == 200 and waited < 2
    assert saved[0].status_code == 200
