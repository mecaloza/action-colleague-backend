"""Changing a module's script by an observation (offline fake LLM): the AI proposes a patch, the server merges it
(untouched scenes byte for byte), the admin applies or discards it, and every change of the script can be undone."""

import copy
import logging
import os
import re
from datetime import timedelta

import pytest
from pydantic import Field

from app.api.routes import studio as studio_routes
from app.core.config import get_settings
from app.db.models import Course, Evaluation, Job, MediaAsset, Module, ModuleRevision
from app.schemas.studio import StoryboardScene
from app.services import revisions
from app.services.ai import designer
from app.services.ai.fake import FakeLLM
from app.services.ai.llm import LLMError, OpenAILLM
from app.services.revisions import Production
from app.services.slides.spec import SceneVisual, Slide
from app.services.video import cache as video_cache
from app.worker import queue
from tests.conftest import auth_headers

NONE = {"kind": "none", "query": "", "prompt": "", "variant": 0}
SCENES = [
    {"id": "s1", "slide": {"layout": "cover", "title": "Presión de inflado", "subtitle": "Lo esencial"},
     "narration": "En este módulo aprendes a medir la presión de inflado.", "visual": NONE},
    {"id": "s2", "slide": {"layout": "bullets", "eyebrow": "Paso 1", "title": "Cómo medir",
                           "points": ["En frío", "Con calibrador"], "icons": ["clock", "check"]},
     "narration": "Mide siempre en frío y con un calibrador revisado.", "visual": NONE},
    {"id": "s3", "slide": {"layout": "visual", "title": "Presión y desgaste"},
     "narration": "Mira las tres huellas de la infografía.",
     "visual": {"kind": "infographic", "query": "", "prompt": "Infografía: tres huellas", "variant": 0}},
    {"id": "s4", "slide": {"layout": "closing", "title": "Para recordar", "points": ["Medir en frío", "Calibrar"]},
     "narration": "Repasemos lo que aprendiste hoy.", "visual": NONE},
]
READING = "### Presión\n\n- Mide en frío"
OUTLINE = {"title": "Presión", "summary": "Medir la presión", "objectives": ["Medir bien"], "key_points": ["En frío"],
           "estimated_minutes": 3, "include_quiz": True}


def _video_cache(scenes: list[dict], production: Production) -> dict:
    """What a video of `scenes` leaves for the next one (`storyboard.render.cache`): each scene's speech and the
    presenter's takes over it."""
    keys = production.narration_keys(scenes)
    digests = [f"clip-{key}" for key in keys]
    return video_cache.record(None, dict(zip(keys, digests)), list(filter(None, production.take_keys(scenes, digests))))


def _after_video(production: Production, scenes: list[dict]) -> Production:
    """The same production once a video of `scenes` was made."""
    return Production(production.presenters, production.has_video, production.voices, production.avatars,
                      production.engine, video_cache.Kept(_video_cache(scenes, production)))


# The course's voice (none chosen: the server's), without a presenter: as the module's last video was made.
RENDER = {"job_id": "render-anterior", "voice_id": "fake-voice-es", "avatar_id": "", "warning": None,
          "cache": _video_cache(SCENES, Production(0, False, (get_settings().elevenlabs_voice_id,)))}
EMPTY_IMPACT = {"narration_scenes": [], "narration_chars": 0, "presenter_takes": 0, "reassigns_presenters": False,
                "visuals_to_make": 0, "needs_render": False}


@pytest.fixture
def admin_headers(client, admin):
    return auth_headers(client, admin.email)


@pytest.fixture
def module(db, admin):
    course = Course(title="Llantas", status="draft", source="ai", settings={}, created_by=admin.id)
    db.add(course)
    db.flush()
    module = Module(course_id=course.id, title="Presión", order=1, source="ai", content_text=READING,
                    storyboard={"outline": OUTLINE, "scenes": copy.deepcopy(SCENES), "render": RENDER})
    db.add(module)
    db.commit()
    return module


def _ask(client, headers, module_id, observation="Haz el título más claro", **extra):
    response = client.post(f"/api/v1/modules/{module_id}/revisions", headers=headers,
                           json={"observation": observation, **extra})
    assert response.status_code == 202, response.text
    return response.json()


def _revision(client, headers, revision_id):
    return client.get(f"/api/v1/revisions/{revision_id}", headers=headers).json()


def _proposed(client, headers, worker, module_id, observation="Haz el título más claro", **extra):
    asked = _ask(client, headers, module_id, observation, **extra)
    worker()
    revision = _revision(client, headers, asked["revision"]["id"])
    assert revision["status"] == "proposed", revision
    return revision


def _apply(client, headers, revision_id, **payload):
    return client.post(f"/api/v1/revisions/{revision_id}/apply", headers=headers, json=payload)


def _stored(db, module) -> Module:
    db.expire_all()
    return db.get(Module, module.id)


def _save(client, headers, module_id, edit) -> None:
    """The admin edits the script in the editor and saves it."""
    storyboard = client.get(f"/api/v1/modules/{module_id}/storyboard", headers=headers).json()
    edit(storyboard["scenes"])
    saved = client.put(f"/api/v1/modules/{module_id}/storyboard", headers=headers, json={"scenes": storyboard["scenes"]})
    assert saved.status_code == 200, saved.text


def _regenerate(client, headers, module_id, feedback="") -> None:
    """The admin asks for the whole script again ("Reescribir con IA")."""
    asked = client.post(f"/api/v1/modules/{module_id}/storyboard/regenerate", headers=headers,
                        json={"feedback": feedback})
    assert asked.status_code == 202, asked.text


def _revert(client, headers, revision_id):
    return client.post(f"/api/v1/revisions/{revision_id}/revert", headers=headers)


def _module_entry(client, headers, module) -> dict:
    """The module as the course's detail shows it to the admin."""
    entries = client.get(f"/api/v1/courses/{module.course_id}", headers=headers).json()["modules"]
    return next(entry for entry in entries if entry["id"] == module.id)


# ── The flow ──────────────────────────────────────────────────────────


def test_a_proposal_changes_only_what_it_names_and_nothing_until_applied(client, db, admin_headers, module, worker):
    asked = _ask(client, admin_headers, module.id, "Cambia el título y la narración", scene_ids=["s2"])
    assert asked["revision"]["status"] == "proposing" and asked["revision"]["changes"] is None
    assert asked["job"]["type"] == "ai.revise" and asked["revision"]["source"] == "observation"
    worker()

    revision = _revision(client, admin_headers, asked["revision"]["id"])
    assert revision["status"] == "proposed" and revision["summary"] and revision["unresolved"] == ""
    assert revision["scope"] == {"scene_ids": ["s2"], "question_ids": [], "qa_issue_id": None, "reverted_id": None}
    [change] = revision["changes"]["scenes"]
    assert (change["scene_id"], change["action"], change["position"]) == ("s2", "update", {"before": 2, "after": 2})
    assert set(change["fields"]) == {"title", "narration"} and change["reason"]
    assert change["fields"]["title"] == {"before": "Cómo medir", "after": "Revisado: Cómo medir"}
    narration = change["after"]["narration"]
    assert [part["op"] for part in change["narration_diff"]] == ["equal", "insert"]
    assert "".join(part["text"] for part in change["narration_diff"] if part["op"] != "delete") == narration
    assert change["before"] == SCENES[1]  # the whole scene, for its preview
    assert revision["changes"]["impact"] == {**EMPTY_IMPACT, "narration_scenes": ["s2"],
                                             "narration_chars": len(narration)}
    assert revision["changes"]["reading"] is None and revision["changes"]["ignored"] == []
    assert _stored(db, module).storyboard["scenes"] == SCENES  # nothing changes until it is applied

    applied = _apply(client, admin_headers, revision["id"])

    assert applied.status_code == 200, applied.text
    assert applied.json()["revision"]["status"] == "applied" and applied.json()["render_job"] is None
    stored = _stored(db, module)
    scenes = stored.storyboard["scenes"]
    assert [scene["id"] for scene in scenes] == ["s1", "s2", "s3", "s4"]
    assert scenes[0] == SCENES[0] and scenes[2:] == SCENES[2:]  # as stored: not even a default written in
    assert scenes[1]["slide"] == {**SCENES[1]["slide"], "title": "Revisado: Cómo medir"}  # its eyebrow and icons too
    assert scenes[1]["narration"] == narration and scenes[1]["visual"] == NONE
    assert stored.storyboard["outline"] == OUTLINE and stored.storyboard["render"] == RENDER
    assert stored.content_text == READING

    history = client.get(f"/api/v1/modules/{module.id}/revisions", headers=admin_headers).json()
    assert [(item["source"], item["status"]) for item in history] == [("observation", "applied")]
    assert history[0]["created_by_name"] == "admin" and history[0]["applied_at"]
    # A double click on "Aplicar" gets what the first one did.
    again = _apply(client, admin_headers, revision["id"])
    assert again.status_code == 200 and _stored(db, module).storyboard["scenes"] == scenes


def test_the_reading_and_a_new_version_of_the_infographic(client, db, admin_headers, module, worker, storage):
    revision = _proposed(client, admin_headers, worker, module.id, "Otra infografía y actualiza la lectura",
                         scene_ids=["s3"])

    [change] = revision["changes"]["scenes"]
    assert change["fields"]["visual"]["after"] == {**SCENES[2]["visual"], "variant": 1}
    assert revision["changes"]["reading"] == {"before": READING, "after": f"{READING}\n- Revisado."}
    assert revision["changes"]["impact"]["visuals_to_make"] == 1
    assert _apply(client, admin_headers, revision["id"]).status_code == 200
    stored = _stored(db, module)
    assert stored.content_text == f"{READING}\n- Revisado." and stored.storyboard["scenes"][2]["visual"]["variant"] == 1
    # The new version is made now, for the editor's preview (no activities are drafted again).
    assert {job.type for job in db.query(Job).filter(Job.status == "queued")} == {"visuals.prepare"}


def test_left_out_changes_are_reported_and_text_is_cut_to_the_slides_limits(
    client, db, admin_headers, module, worker, monkeypatch
):
    db.add(Evaluation(module_id=module.id, spec=[{"id": "q1", "type": "true_false", "prompt": "Se mide en frío",
                                                  "correct": True, "explanation": ""}]))
    db.commit()
    prompts = []

    def patch(self, prompt):
        prompts.append(prompt)
        scene = designer.SceneDraft.model_construct(**{
            **self._scene("bullets", "t" * 900, "").model_dump(), "icons": ["no-existe", "check"],
        })
        change = designer.SceneChangeDraft
        return designer.StoryboardPatchDraft(summary="Varios cambios", changes=[
            change(action="update", scene_id="s2", fields=["title", "icons"], scene=scene, visual_action="keep",
                   reason="Título más claro"),
            change(action="update", scene_id="s1", fields=["title"], scene=scene, visual_action="keep", reason="x"),
            change(action="update", scene_id="s9", fields=["title"], scene=scene, visual_action="keep", reason="x"),
            change(action="update", scene_id="s2", fields=["subtitle"], scene=scene, visual_action="keep", reason="x"),
            change(action="delete", scene_id="s4", fields=[], scene=scene, visual_action="keep", reason="x"),
        ], reading_changed=False, reading_summary="", affected_question_ids=["q1", "q-inventada"], unresolved="")

    monkeypatch.setattr(FakeLLM, "_storyboard_patch", patch)
    revision = _proposed(client, admin_headers, worker, module.id, scene_ids=["s2", "s3"])

    assert "PREGUNTAS DE LA EVALUACIÓN" in prompts[0] and "q1: Se mide en frío" in prompts[0]
    [change] = revision["changes"]["scenes"]
    assert change["scene_id"] == "s2"
    assert change["after"]["slide"]["title"] == "t" * 300  # the slide's limit
    assert change["after"]["slide"]["icons"] == ["", "check"]  # an icon not in the catalog is drawn as none
    assert [(item["scene_id"], item["reason"]) for item in revision["changes"]["ignored"]] == [
        ("s1", "La escena no está entre las que elegiste."),
        ("s9", "Esa escena no existe en el guion."),
        ("s2", "Ya había otro cambio para esta escena."),
        ("s4", "La escena no está entre las que elegiste."),
    ]
    assert revision["changes"]["affected_question_ids"] == ["q1"]


def test_applying_after_other_edits_rebases_them_or_refuses_by_scene(client, db, admin_headers, module, worker):
    revision = _proposed(client, admin_headers, worker, module.id, scene_ids=["s2"])
    _save(client, admin_headers, module.id, lambda scenes: scenes[3].update(narration="Un cierre editado a mano."))

    applied = _apply(client, admin_headers, revision["id"])

    assert applied.status_code == 200 and applied.json()["revision"]["changes"]["rebased"] is True
    scenes = _stored(db, module).storyboard["scenes"]
    assert scenes[1]["slide"]["title"] == "Revisado: Cómo medir" and scenes[3]["narration"] == "Un cierre editado a mano."
    assert [change["scene_id"] for change in applied.json()["revision"]["changes"]["scenes"]] == ["s2"]

    second = _proposed(client, admin_headers, worker, module.id, "Otro título", scene_ids=["s2"])
    _save(client, admin_headers, module.id, lambda scenes: scenes[1]["slide"].update(title="Editado a mano"))
    stale = _apply(client, admin_headers, second["id"])

    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "revision_stale" and stale.json()["detail"]["scene_ids"] == ["s2"]
    assert "la escena 2" in stale.json()["detail"]["message"]
    assert _stored(db, module).storyboard["scenes"][1]["slide"]["title"] == "Editado a mano"
    assert _revision(client, admin_headers, second["id"])["status"] == "proposed"  # still there to discard


def test_nothing_is_applied_while_the_video_is_being_produced(client, db, admin_headers, module, worker):
    revision = _proposed(client, admin_headers, worker, module.id)
    render = queue.enqueue(db, "video.render", {}, course_id=module.course_id, module_id=module.id,
                           dedupe_key=f"render:{module.id}")

    for status in ("queued", "running"):
        render.status = status
        db.commit()
        refused = _apply(client, admin_headers, revision["id"])
        assert refused.status_code == 409 and "se está produciendo" in refused.json()["detail"]
    render.status = "succeeded"
    db.commit()
    stored = _stored(db, module)
    stored.generation_status = "generating"  # e.g. its script being rewritten
    db.commit()
    assert _apply(client, admin_headers, revision["id"]).status_code == 409

    stored.generation_status = "completed"
    db.commit()
    assert _apply(client, admin_headers, revision["id"]).status_code == 200


def test_applying_and_producing_the_video_again(client, db, admin_headers, module, worker):
    video = MediaAsset(kind="video", status="ready", bucket="b", path="v.mp4", course_id=module.course_id)
    db.add(video)
    db.flush()
    module.video_asset_id = video.id
    db.commit()
    revision = _proposed(client, admin_headers, worker, module.id)
    assert revision["changes"]["impact"]["needs_render"] is True

    applied = _apply(client, admin_headers, revision["id"], render=True)

    assert applied.status_code == 200 and applied.json()["render_job"]["type"] == "video.render"
    assert _stored(db, module).generation_status == "queued"
    rendering = queue.active_with_key(db, f"render:{module.id}")
    assert rendering.id == applied.json()["render_job"]["id"]
    # A double click: the same video, not another one.
    again = _apply(client, admin_headers, revision["id"], render=True)
    assert again.status_code == 200 and again.json()["render_job"]["id"] == rendering.id
    assert db.query(Job).filter(Job.type == "video.render").count() == 1


def test_only_a_video_the_module_has_is_produced_again(client, db, admin_headers, module, worker, monkeypatch):
    monkeypatch.setattr(studio_routes, "voice_available", lambda: False)  # no narration on this server
    video = MediaAsset(kind="video", status="ready", bucket="b", path="v.mp4", course_id=module.course_id)
    db.add(video)
    db.flush()
    module.video_asset_id = video.id
    db.commit()
    revision = _proposed(client, admin_headers, worker, module.id)

    refused = _apply(client, admin_headers, revision["id"], render=True)  # its video couldn't be narrated

    assert refused.status_code == 503 and _revision(client, admin_headers, revision["id"])["status"] == "proposed"
    stored = _stored(db, module)
    assert stored.storyboard["scenes"] == SCENES  # the change waits with its video
    stored.video_asset_id = None  # no video yet: its first one is made from the studio
    db.commit()

    applied = _apply(client, admin_headers, revision["id"], render=True)

    assert applied.status_code == 200, applied.text
    assert applied.json()["revision"]["status"] == "applied" and applied.json()["render_job"] is None
    assert db.query(Job).filter(Job.type == "video.render").count() == 0


def test_undo_restores_what_the_change_touched_and_keeps_later_edits(client, db, admin_headers, module, worker):
    revision = _proposed(client, admin_headers, worker, module.id, "Cambia la narración", scene_ids=["s2"])
    assert _apply(client, admin_headers, revision["id"]).status_code == 200
    _save(client, admin_headers, module.id, lambda scenes: scenes[0]["slide"].update(title="Portada editada"))

    undone = client.post(f"/api/v1/revisions/{revision['id']}/revert", headers=admin_headers)

    assert undone.status_code == 200, undone.text
    undo = undone.json()
    assert (undo["source"], undo["status"]) == ("revert", "applied") and undo["summary"].startswith("Deshace")
    assert undo["scope"]["reverted_id"] == revision["id"] and undo["scope"]["scene_ids"] == ["s2"]
    scenes = _stored(db, module).storyboard["scenes"]
    assert scenes[1] == SCENES[1]  # exactly what it was
    assert scenes[0]["slide"]["title"] == "Portada editada"  # the later edit elsewhere stays
    assert _revision(client, admin_headers, revision["id"])["status"] == "reverted"
    assert client.post(f"/api/v1/revisions/{revision['id']}/revert", headers=admin_headers).status_code == 409

    redone = client.post(f"/api/v1/revisions/{undo['id']}/revert", headers=admin_headers)  # an undo can be undone

    assert redone.status_code == 200
    assert _stored(db, module).storyboard["scenes"][1]["slide"]["title"] == "Revisado: Cómo medir"
    history = client.get(f"/api/v1/modules/{module.id}/revisions", headers=admin_headers).json()
    assert [(item["source"], item["status"]) for item in history] == [
        ("revert", "applied"), ("revert", "reverted"), ("manual", "applied"), ("observation", "reverted"),
    ]


def test_undo_is_refused_when_the_same_scene_changed_since(client, db, admin_headers, module, worker):
    revision = _proposed(client, admin_headers, worker, module.id, scene_ids=["s2"])
    assert _apply(client, admin_headers, revision["id"]).status_code == 200
    _save(client, admin_headers, module.id, lambda scenes: scenes[1]["slide"].update(title="Editado a mano"))

    refused = client.post(f"/api/v1/revisions/{revision['id']}/revert", headers=admin_headers)

    assert refused.status_code == 409
    detail = refused.json()["detail"]
    assert detail["code"] == "revision_conflict" and detail["scene_ids"] == ["s2"]
    assert detail["message"] == (
        "No se puede deshacer: el guion cambió después en la escena 2. Edítalo a mano o pide otro cambio."
    )
    assert _stored(db, module).storyboard["scenes"][1]["slide"]["title"] == "Editado a mano"


def test_undoing_waits_for_the_video_and_the_script_and_is_only_for_applied_changes(
    client, db, admin_headers, module, worker
):
    asked = _ask(client, admin_headers, module.id, scene_ids=["s2"])
    revision_id = asked["revision"]["id"]
    early = _apply(client, admin_headers, revision_id)  # the AI hasn't answered yet
    assert early.status_code == 409 and "aún se está preparando" in early.json()["detail"]
    worker()
    not_applied = _revert(client, admin_headers, revision_id)
    assert not_applied.status_code == 409
    assert not_applied.json()["detail"] == "Solo se puede deshacer un cambio aplicado"
    assert _apply(client, admin_headers, revision_id).status_code == 200
    applied = _stored(db, module).storyboard["scenes"]

    # The render reads the script when it starts: undoing meanwhile would publish a video of the other one.
    render = queue.enqueue(db, "video.render", {}, course_id=module.course_id, module_id=module.id,
                           dedupe_key=f"render:{module.id}")
    for status in ("queued", "running"):
        render.status = status
        db.commit()
        refused = _revert(client, admin_headers, revision_id)
        assert refused.status_code == 409 and "se está produciendo" in refused.json()["detail"]
    render.status = "succeeded"
    db.commit()
    stored = _stored(db, module)
    stored.generation_status = "generating"  # its script being rewritten
    db.commit()
    refused = _revert(client, admin_headers, revision_id)
    assert refused.status_code == 409 and "se está generando" in refused.json()["detail"]
    stored.generation_status, stored.source = "completed", "text"  # it got content of its own meanwhile
    db.commit()
    refused = _revert(client, admin_headers, revision_id)
    assert refused.status_code == 409 and "tiene su propio contenido" in refused.json()["detail"]

    assert _stored(db, module).storyboard["scenes"] == applied  # nothing was undone
    assert _revision(client, admin_headers, revision_id)["status"] == "applied"
    stored = _stored(db, module)
    stored.source = "ai"
    db.commit()
    assert _revert(client, admin_headers, revision_id).status_code == 200
    discarded = _proposed(client, admin_headers, worker, module.id, "Otro título", scene_ids=["s2"])
    assert client.post(f"/api/v1/revisions/{discarded['id']}/discard", headers=admin_headers).status_code == 200
    assert _revert(client, admin_headers, discarded["id"]).status_code == 409


def test_a_double_click_gets_the_same_proposal_and_another_observation_waits(client, db, admin_headers, module, worker):
    first = _ask(client, admin_headers, module.id, "Más claro", scene_ids=["s2"])
    again = _ask(client, admin_headers, module.id, "Más claro", scene_ids=["s2"])
    assert (again["revision"]["id"], again["job"]["id"]) == (first["revision"]["id"], first["job"]["id"])
    other = client.post(f"/api/v1/modules/{module.id}/revisions", headers=admin_headers, json={"observation": "Otra"})
    assert other.status_code == 409
    worker()

    newer = _ask(client, admin_headers, module.id, "Pide otro ajuste")  # once proposed, a new one replaces it

    assert _revision(client, admin_headers, first["revision"]["id"])["status"] == "superseded"
    assert _apply(client, admin_headers, first["revision"]["id"]).status_code == 409
    assert newer["revision"]["scope"]["scene_ids"] == []  # the whole script


def test_discarding_a_proposal(client, db, admin_headers, module, worker):
    asked = _ask(client, admin_headers, module.id)
    discarded = client.post(f"/api/v1/revisions/{asked['revision']['id']}/discard", headers=admin_headers)
    assert discarded.status_code == 200 and discarded.json()["status"] == "discarded"
    assert db.get(Job, asked["job"]["id"]).status == "canceled" and worker() == 0  # the AI isn't asked

    revision = _proposed(client, admin_headers, worker, module.id)
    assert client.post(f"/api/v1/revisions/{revision['id']}/discard", headers=admin_headers).status_code == 200
    assert _apply(client, admin_headers, revision["id"]).status_code == 409
    assert _stored(db, module).storyboard["scenes"] == SCENES

    applied = _proposed(client, admin_headers, worker, module.id)
    assert _apply(client, admin_headers, applied["id"]).status_code == 200
    assert client.post(f"/api/v1/revisions/{applied['id']}/discard", headers=admin_headers).status_code == 409


def test_a_proposal_the_ai_cant_make_fails_with_the_reason(client, db, admin_headers, module, worker, monkeypatch):
    def nothing(self, prompt):
        return designer.StoryboardPatchDraft(summary="", changes=[], reading_changed=False, reading_summary="",
                                             affected_question_ids=[], unresolved="Los materiales no traen ese dato.")

    monkeypatch.setattr(FakeLLM, "_storyboard_patch", nothing)
    asked = _ask(client, admin_headers, module.id, "Agrega la norma de 2025")
    worker()

    failed = _revision(client, admin_headers, asked["revision"]["id"])
    assert failed["status"] == "failed" and failed["error"] == "La IA no propuso cambios: Los materiales no traen ese dato."
    assert db.get(Job, asked["job"]["id"]).status == "failed"  # not retried: the same ask gets the same answer

    def refuse(self, *args, **kwargs):
        raise LLMError("El modelo no pudo generar este contenido.", permanent=True)

    monkeypatch.setattr(FakeLLM, "structured", refuse)
    asked = _ask(client, admin_headers, module.id, "Otra cosa")
    worker()
    assert _revision(client, admin_headers, asked["revision"]["id"])["error"] == "El modelo no pudo generar este contenido."


def test_what_can_be_revised(client, db, admin, admin_headers, module):
    url = f"/api/v1/modules/{module.id}/revisions"
    assert client.post(url, headers=admin_headers, json={"observation": "   "}).status_code == 422
    unknown = client.post(url, headers=admin_headers, json={"observation": "Más claro", "scene_ids": ["s9"]})
    assert unknown.status_code == 422
    without_quiz = client.post(url, headers=admin_headers, json={"observation": "x", "target": "quiz"})
    assert without_quiz.status_code == 422 and without_quiz.json()["detail"] == "«Presión» no tiene evaluación todavía"
    queue.enqueue(db, "ai.module_draft", {"module_id": module.id}, module_id=module.id, dedupe_key=f"draft:{module.id}")
    assert client.post(url, headers=admin_headers, json={"observation": "Más claro"}).status_code == 409

    course = Course(title="Manual", status="draft", source="manual", settings={}, created_by=admin.id)
    db.add(course)
    db.flush()
    own = Module(course_id=course.id, title="Propio", order=1, source="text", content_text="Texto")
    empty = Module(course_id=course.id, title="Sin guion", order=2, source="ai", storyboard={"scenes": []})
    db.add_all([own, empty])
    db.commit()
    assert client.post(f"/api/v1/modules/{own.id}/revisions", headers=admin_headers,
                       json={"observation": "Más claro"}).status_code == 409
    assert client.post(f"/api/v1/modules/{empty.id}/revisions", headers=admin_headers,
                       json={"observation": "Más claro"}).status_code == 422
    assert client.get("/api/v1/revisions/999", headers=admin_headers).status_code == 404


def test_a_finding_of_the_video_review_is_a_proposal_from_qa(client, admin_headers, module, worker):
    asked = _ask(client, admin_headers, module.id, "El texto se corta", scene_ids=["s2"], qa_issue_id="issue-7")
    assert asked["revision"]["source"] == "qa" and asked["revision"]["scope"]["qa_issue_id"] == "issue-7"


# ── History of what is saved by hand and rewritten ────────────────────


def test_saving_by_hand_and_rewriting_with_ai_are_kept_and_can_be_undone(
    client, db, admin_headers, module, worker, storage
):
    _save(client, admin_headers, module.id, lambda scenes: scenes[1].update(narration="Editada a mano."))
    _save(client, admin_headers, module.id, lambda scenes: None)  # saved again without changes: nothing new

    history = client.get(f"/api/v1/modules/{module.id}/revisions", headers=admin_headers).json()
    assert [(item["source"], item["status"], item["summary"]) for item in history] == [
        ("manual", "applied", "Cambios a mano en 1 escena."),
    ]
    manual = _revision(client, admin_headers, history[0]["id"])
    assert [change["scene_id"] for change in manual["changes"]["scenes"]] == ["s2"]
    edited = _stored(db, module).storyboard["scenes"]

    client.post(f"/api/v1/modules/{module.id}/storyboard/regenerate", headers=admin_headers,
                json={"feedback": "Más corto"})
    worker()

    history = client.get(f"/api/v1/modules/{module.id}/revisions", headers=admin_headers).json()
    assert [(item["source"], item["observation"]) for item in history] == [("regenerate", "Más corto"), ("manual", "")]
    rewrite = _revision(client, admin_headers, history[0]["id"])
    assert rewrite["changes"]["reading"]["before"] == READING
    assert client.post(f"/api/v1/revisions/{rewrite['id']}/revert", headers=admin_headers).status_code == 200
    stored = _stored(db, module)
    assert stored.storyboard["scenes"] == edited and stored.content_text == READING
    assert client.post(f"/api/v1/revisions/{manual['id']}/revert", headers=admin_headers).status_code == 200
    assert _stored(db, module).storyboard["scenes"][1]["narration"] == SCENES[1]["narration"]


def test_a_rewrite_retires_the_changes_proposed_to_the_script_it_replaces(
    client, db, admin_headers, module, worker, storage
):
    # The rewrite's scenes take the old ids (s1, s2…): a scene the proposal adds after s2 would land in it.
    proposed = _proposed(client, admin_headers, worker, module.id, scene_ids=["s2"])
    _regenerate(client, admin_headers, module.id)
    worker()

    assert _revision(client, admin_headers, proposed["id"])["status"] == "superseded"
    assert _apply(client, admin_headers, proposed["id"]).status_code == 409
    assert _module_entry(client, admin_headers, module)["revision_pending"] is False
    history = client.get(f"/api/v1/modules/{module.id}/revisions", headers=admin_headers).json()
    assert [(item["source"], item["status"]) for item in history] == [
        ("regenerate", "applied"), ("observation", "superseded"),
    ]

    # Asked before a rewrite and answered after it: an answer about a script that no longer exists.
    asked = _ask(client, admin_headers, module.id, "Otro ajuste")
    _regenerate(client, admin_headers, module.id, "Más corto")
    revise = db.get(Job, asked["job"]["id"])
    revise.run_after = queue.utcnow() + timedelta(hours=1)  # the rewrite finishes first
    db.commit()
    worker()
    assert _revision(client, admin_headers, asked["revision"]["id"])["status"] == "superseded"
    revise = db.get(Job, asked["job"]["id"])
    revise.run_after = queue.utcnow()
    db.commit()
    worker()

    db.expire_all()
    assert db.get(Job, asked["job"]["id"]).result == {"skipped": "revision no longer waiting"}
    revision = _revision(client, admin_headers, asked["revision"]["id"])
    assert revision["status"] == "superseded" and revision["changes"] is None  # the AI's answer is left out
    assert _module_entry(client, admin_headers, module)["revision_pending"] is False


def test_editing_the_reading_in_the_module_sheet_is_kept_and_can_be_undone(client, db, admin_headers, module):
    url = f"/api/v1/modules/{module.id}"
    assert client.patch(url, headers=admin_headers, json={"content_text": "Otra lectura"}).status_code == 200
    assert client.patch(url, headers=admin_headers, json={"title": "Presión de inflado"}).status_code == 200
    assert client.patch(url, headers=admin_headers, json={"content_text": "Otra lectura"}).status_code == 200

    history = client.get(f"/api/v1/modules/{module.id}/revisions", headers=admin_headers).json()
    assert [(item["source"], item["status"], item["summary"]) for item in history] == [
        ("manual", "applied", "Cambios a mano en la lectura."),  # the title isn't the script; the same text, nothing
    ]
    manual = _revision(client, admin_headers, history[0]["id"])
    assert manual["changes"]["reading"] == {"before": READING, "after": "Otra lectura"}
    assert manual["changes"]["scenes"] == [] and manual["created_by_name"] == "admin"

    assert _revert(client, admin_headers, manual["id"]).status_code == 200
    assert _stored(db, module).content_text == READING

    own = Module(course_id=module.course_id, title="Propio", order=2, source="text", content_text="Texto")
    db.add(own)
    db.commit()
    assert client.patch(f"/api/v1/modules/{own.id}", headers=admin_headers,
                        json={"content_text": "Otro texto"}).status_code == 200
    assert db.query(ModuleRevision).filter(ModuleRevision.module_id == own.id).count() == 0  # no script to undo


# ── The video and the script ──────────────────────────────────────────


def test_the_video_tells_which_scenes_it_no_longer_shows(client, db, admin_headers, module, worker, storage):
    video = MediaAsset(kind="video", status="ready", bucket="b", path="v.mp4", course_id=module.course_id)
    db.add(video)
    db.flush()
    module.video_asset_id = video.id
    module.storyboard = {**module.storyboard, "render": {**RENDER, "scene_hashes": revisions.video_scenes(SCENES)}}
    db.commit()

    def detail():
        [entry] = client.get(f"/api/v1/courses/{module.course_id}", headers=admin_headers).json()["modules"]
        return entry["stale_scene_ids"], entry["revision_pending"]

    assert detail() == ([], False)
    revision = _proposed(client, admin_headers, worker, module.id, scene_ids=["s2"])
    assert detail() == ([], True)  # a proposal waits for review; the script didn't change yet

    assert _apply(client, admin_headers, revision["id"]).status_code == 200

    assert detail() == (["s2"], False)
    _save(client, admin_headers, module.id, lambda scenes: scenes.insert(0, scenes.pop(3)))  # the closing moved first
    assert detail() == (["s4", "s2"], False)
    _save(client, admin_headers, module.id, lambda scenes: scenes.pop(0))
    assert detail() == (["s2", "s4"], False)  # a scene the video shows that the script no longer has


def test_videos_made_before_record_their_scenes_from_their_render(db, module, worker, storage, caplog):
    video = MediaAsset(kind="video", status="ready", bucket="b", path="v.mp4", course_id=module.course_id)
    render = Job(type="video.render", status="succeeded", payload={}, state={"input": {"scenes": SCENES}})
    gone = Module(course_id=module.course_id, title="Sin trabajo", order=2, source="ai", video_asset_id="x",
                  storyboard={"scenes": SCENES, "render": {**RENDER, "job_id": "borrado"}})
    db.add_all([video, render, gone])
    db.flush()
    module.video_asset_id = video.id
    module.storyboard = {**module.storyboard, "render": {**RENDER, "job_id": render.id}}
    module.storyboard["scenes"][1]["narration"] = "Editada después del video."
    db.commit()

    queue.enqueue(db, "legacy.migrate", dedupe_key="legacy-migrate")
    with caplog.at_level(logging.INFO, logger="app.services.revisions"):
        worker()

    db.expire_all()
    assert db.query(Job).filter(Job.type == "legacy.migrate").one().result["video_scenes_recorded"] == 1
    stored = db.get(Module, module.id)
    assert stored.storyboard["render"]["scene_hashes"] == revisions.video_scenes(SCENES)
    assert revisions.stale_scene_ids(stored.storyboard) == ["s2"]
    assert "scene_hashes" not in db.get(Module, gone.id).storyboard["render"]  # its job doesn't say: unknown
    assert revisions.backfill_video_scenes(db) == 0  # idempotent
    assert [record.modules for record in caplog.records if record.getMessage() == "video_scenes_backfilled"] == [1]


# ── The merge, the diff and the undo, on their own ────────────────────


def _change(action="update", scene_id="s2", fields=("title",), visual="keep", **scene):
    values = {"layout": "bullets", "title": "", "narration": "", **scene}
    return designer.SceneChangeDraft(
        action=action, scene_id=scene_id, fields=list(fields), visual_action=visual, reason="Porque sí",
        scene=FakeLLM()._scene(values.pop("layout"), values.pop("title"), values.pop("narration"), **values),
    )


def _patch(*changes, **fields):
    return designer.StoryboardPatchDraft(**{
        "summary": "Cambios", "changes": list(changes), "reading_changed": False, "reading_summary": "",
        "affected_question_ids": [], "unresolved": "", **fields,
    })


def _propose(*changes, scenes=SCENES, scope=None, production=Production(0, False), max_clips=2, **fields):
    before = {"scenes": copy.deepcopy(scenes), "reading": READING}
    return revisions.propose(before, _patch(*changes, **fields), scene_ids=scope, production=production,
                             question_ids=[], max_clips=max_clips)


def test_points_bring_their_icons_and_icons_alone_follow_the_points():
    proposal = _propose(_change(fields=["points"], points=["Uno", "Dos", "Tres"], icons=["eye", "cog", "check"]))
    assert proposal.after["scenes"][1]["slide"]["points"] == ["Uno", "Dos", "Tres"]
    assert proposal.after["scenes"][1]["slide"]["icons"] == ["eye", "cog", "check"]

    proposal = _propose(_change(fields=["icons"], icons=["eye", "cog"]))  # the model left the points empty
    assert proposal.after["scenes"][1]["slide"]["points"] == SCENES[1]["slide"]["points"]
    assert proposal.after["scenes"][1]["slide"]["icons"] == ["eye", "cog"]

    # A case's parts lose the label the slide already shows, as in a first draft.
    case = [{**SCENES[1], "slide": {"layout": "case", "points": ["a", "b", "c", "d"]}}]
    proposal = _propose(_change(scene_id="s2", fields=["points"], points=["Situación: una flota", "b", "c", "d"]),
                        scenes=case)
    assert proposal.after["scenes"][0]["slide"]["points"] == ["una flota", "b", "c", "d"]


def test_another_layout_takes_the_models_whole_slide_and_keeps_the_eyebrow():
    proposal = _propose(_change(fields=["layout"], layout="steps", title="Pasos", points=["Uno", "Dos"],
                                icons=["eye", "cog"]))
    slide = proposal.after["scenes"][1]["slide"]
    assert (slide["layout"], slide["title"], slide["points"], slide["icons"]) == ("steps", "Pasos", ["Uno", "Dos"], [])
    assert slide["eyebrow"] == "Paso 1"
    # Out of the "visual" layout its infographic goes; into it, one is made from the title.
    proposal = _propose(_change(scene_id="s3", fields=["layout"], layout="bullets", title="Huellas", points=["A"]),
                        _change(scene_id="s4", fields=["layout"], layout="visual", title="Resumen visual"))
    assert proposal.after["scenes"][2]["visual"] == NONE
    assert proposal.after["scenes"][3]["visual"]["kind"] == "infographic"
    assert proposal.after["scenes"][3]["visual"]["prompt"] == "Resumen visual"


def test_visuals_change_only_as_asked_and_within_the_clip_budget():
    proposal = _propose(_change(scene_id="s3", fields=["visual"], visual="new_version"),
                        _change(scene_id="s2", fields=["title", "visual"], visual="keep", title="Otro"))
    assert proposal.after["scenes"][2]["visual"]["variant"] == 1
    assert proposal.after["scenes"][1]["visual"] == NONE
    assert proposal.changes["impact"]["visuals_to_make"] == 1

    clip = {"kind": "clip", "query": "", "prompt": "a truck", "variant": 0}
    scenes = [*SCENES[:3], {**SCENES[3], "visual": clip}]
    proposal = _propose(_change(scene_id="s2", fields=["visual"], visual="new_prompt", visual_kind="clip",
                                visual_prompt="a tire being measured"), scenes=scenes, max_clips=1)
    assert proposal.after["scenes"][1]["visual"]["kind"] == "none"  # the module's one clip is taken
    with pytest.raises(revisions.ProposalError):  # "another version" of no picture: nothing changes
        _propose(_change(scene_id="s1", fields=["visual"], visual="new_version"))
    # A new infographic the model didn't describe is drawn from the scene's title, as in a first draft.
    proposal = _propose(_change(scene_id="s3", fields=["visual"], visual="new_prompt", visual_kind="infographic"))
    assert proposal.after["scenes"][2]["visual"]["prompt"] == "Presión y desgaste"


def test_scenes_added_and_removed_and_who_presents_them():
    two = _after_video(Production(presenters=2, has_video=True), SCENES)
    proposal = _propose(_change(action="insert_after", scene_id="s1", fields=[], title="Nueva", narration="Hola."),
                        _change(action="delete", scene_id="s4", fields=[]), production=two)

    ids = [scene["id"] for scene in proposal.after["scenes"]]
    assert ids[0] == "s1" and re.fullmatch(r"s[0-9a-f]{8}", ids[1]) and ids[2:] == ["s2", "s3"]
    assert [(change["action"], change["position"]) for change in proposal.changes["scenes"]] == [
        ("insert", {"before": None, "after": 2}), ("delete", {"before": 4, "after": None}),
    ]
    assert proposal.changes["impact"] == {
        "narration_scenes": [ids[1]], "narration_chars": 5, "presenter_takes": 2, "reassigns_presenters": True,
        "visuals_to_make": 0, "needs_render": True,
    }
    # The last scene removed: nobody else changes presenter, only its take is recorded again.
    impact = _propose(_change(action="delete", scene_id="s4", fields=[]), production=two).changes["impact"]
    assert (impact["presenter_takes"], impact["reassigns_presenters"]) == (1, False)
    one = _propose(_change(fields=["narration"], narration="Otra narración."), production=Production(1, True))
    assert one.changes["impact"]["presenter_takes"] == 1
    assert _propose(_change(title="Otro"), production=two).changes["impact"]["presenter_takes"] == 0
    ignored = _propose(_change(title="Otro"), _change(action="insert_after", scene_id="s1", fields=[])).changes
    assert ignored["ignored"][0]["reason"] == "La escena nueva no tenía narración."


def test_a_proposal_that_changes_nothing_fails():
    with pytest.raises(revisions.ProposalError, match="no propuso"):
        _propose(_change(title="Cómo medir"))  # the same title
    with pytest.raises(revisions.ProposalError, match="todas las escenas"):
        _propose(*(_change(action="delete", scene_id=scene["id"], fields=[]) for scene in SCENES))


def test_when_every_change_is_left_out_the_failure_says_why():
    with pytest.raises(revisions.ProposalError) as failed:  # the admin chose scene 2; the AI changed scene 3
        _propose(_change(scene_id="s3", title="Otro"), scope=["s2"])
    assert str(failed.value) == (
        "La IA solo propuso cambios que no se pueden aplicar. La escena no está entre las que elegiste."
    )

    with pytest.raises(revisions.ProposalError) as failed:  # and one that changes nothing: not a reason
        _propose(_change(scene_id="s9", title="Otro"), _change(scene_id="s3", title="Otro"),
                 _change(title="Cómo medir"), scope=["s2"], unresolved="No encontré la escena del caso.")
    assert str(failed.value) == (
        "La IA solo propuso cambios que no se pueden aplicar. Esa escena no existe en el guion. "
        "La escena no está entre las que elegiste. No encontré la escena del caso."
    )


def test_narration_diff_goes_word_by_word():
    before, after = "Mide la presión en frío, cada semana.", "Mide la presión  en caliente, cada semana."
    parts = revisions.narration_diff(before, after)
    assert parts == [
        {"op": "equal", "text": "Mide la presión  en "}, {"op": "delete", "text": "frío, "},
        {"op": "insert", "text": "caliente, "}, {"op": "equal", "text": "cada semana."},
    ]
    assert "".join(part["text"] for part in parts if part["op"] != "delete") == after
    assert revisions.narration_diff("", "Hola.") == [{"op": "insert", "text": "Hola."}]
    assert revisions.narration_diff("Hola.", "") == [{"op": "delete", "text": "Hola."}]


def test_three_way_rebases_by_scene_and_names_what_conflicts():
    base = {"scenes": copy.deepcopy(SCENES), "reading": READING}
    target = copy.deepcopy(base)
    target["scenes"][1]["slide"]["title"] = "Nuevo"
    target["scenes"].insert(2, {**SCENES[0], "id": "s9", "narration": "Nueva escena."})
    current = copy.deepcopy(base)
    current["scenes"][0]["slide"]["title"] = "Editado"  # elsewhere
    current["scenes"].insert(0, {**SCENES[3], "id": "s8"})  # added elsewhere
    current["reading"] = "Otra lectura"

    merged = revisions.three_way(current, base, target)

    assert [scene["id"] for scene in merged["scenes"]] == ["s8", "s1", "s2", "s9", "s3", "s4"]
    assert merged["scenes"][1]["slide"]["title"] == "Editado" and merged["scenes"][2]["slide"]["title"] == "Nuevo"
    assert merged["reading"] == "Otra lectura"  # the change didn't touch it
    assert revisions.three_way(base, base, target) == target

    current["scenes"][2] = copy.deepcopy(target["scenes"][1])  # s2 edited by hand just as the change does: no conflict
    assert revisions.three_way(current, base, target)["scenes"][2] == target["scenes"][1]
    current["scenes"][2]["narration"] = "Editada"  # now otherwise
    with pytest.raises(revisions.Conflict) as conflict:
        revisions.three_way(current, base, {**target, "reading": "Lectura nueva"})
    assert conflict.value.ids == ["s2"] and conflict.value.reading is True

    moved = {**base, "scenes": [base["scenes"][3], *base["scenes"][:3]]}  # the closing moved first
    assert [s["id"] for s in revisions.three_way(current, base, moved)["scenes"]] == ["s4", "s8", "s1", "s2", "s3"]


def test_scenes_that_only_differ_in_defaults_left_out_are_the_same():
    spelled_out = [
        {**scene, "slide": {**scene["slide"], "icons": scene["slide"].get("icons", []),
                            "eyebrow": scene["slide"].get("eyebrow", "")}}
        for scene in SCENES
    ]
    assert revisions.video_scenes(spelled_out) == revisions.video_scenes(SCENES)
    storyboard = {"scenes": spelled_out, "render": {"scene_hashes": revisions.video_scenes(SCENES)}}
    assert revisions.stale_scene_ids(storyboard) == []
    assert revisions.stale_scene_ids({"scenes": SCENES, "render": {"job_id": "x"}}) == []  # unknown: none


class _GrownSlide(Slide):
    caption: str = ""  # e.g. a field a later version adds to every slide, with its default


class _GrownVisual(SceneVisual):
    style: str = ""


class _GrownScene(StoryboardScene):
    slide: _GrownSlide
    visual: _GrownVisual = Field(default_factory=_GrownVisual)


def test_a_field_added_to_the_slides_later_leaves_the_videos_up_to_date(monkeypatch):
    shown = revisions.video_scenes(SCENES)  # kept with each video when it is published
    monkeypatch.setattr(revisions, "StoryboardScene", _GrownScene)  # the next deploy

    assert revisions.video_scenes(SCENES) == shown
    resaved = [{**scene, "slide": {**scene["slide"], "caption": ""}} for scene in SCENES]  # by the editor, then
    assert revisions.stale_scene_ids({"scenes": resaved, "render": {"scene_hashes": shown}}) == []
    captioned = [resaved[0], {**resaved[1], "slide": {**resaved[1]["slide"], "caption": "Nuevo"}}, *resaved[2:]]
    assert revisions.stale_scene_ids({"scenes": captioned, "render": {"scene_hashes": shown}}) == ["s2"]


# ── The prompt and the model's usage ──────────────────────────────────


def test_the_revision_prompt_puts_what_repeats_first_and_the_observation_last():
    outline = designer.CourseOutline(title="Llantas", description="D", audience="Asesores", objectives=[],
                                     modules=[designer.OutlineModule(**OUTLINE),
                                              designer.OutlineModule(**{**OUTLINE, "title": "Rotación"})])
    chart = {"id": "s5", "slide": {"layout": "chart", "title": "Costo", "chart_labels": ["A", "B"],
                                   "chart_values": [1.5, 0.0], "chart_unit": "MXN"}, "narration": "Compara."}
    prompt = designer.revision_prompt(outline, outline.modules[0], 1, "cercano", "Material de llantas",
                                      [*SCENES, chart], READING, [("q1", "¿Se mide en frío?")], ["s2"],
                                      "Haz el título más claro")

    order = ["Material de llantas", "CURSO: Llantas", "REGLAS DE LAS ESCENAS", "CÓMO RESPONDER", "MÓDULO 1: Presión",
             "GUION ACTUAL", "LECTURA ACTUAL", "q1: ¿Se mide en frío?", "ESCENAS EN ALCANCE: s2",
             "OBSERVACIÓN DEL ADMINISTRADOR:"]
    assert [prompt.index(part) for part in order] == sorted(prompt.index(part) for part in order)
    assert prompt.endswith("OBSERVACIÓN DEL ADMINISTRADOR:\nHaz el título más claro")
    # A change to another module of the course starts the same, up to its own module (what the provider reuses).
    other = designer.revision_prompt(outline, outline.modules[1], 2, "cercano", "Material de llantas", SCENES[:2],
                                     "", [], None, "Otra cosa")
    assert len(os.path.commonprefix([prompt, other])) >= prompt.index("MÓDULO 1: Presión")
    script = designer.storyboard_json([*SCENES, chart])
    assert script in prompt
    assert '"id": "s2"' in script and '"subtitle": "Lo esencial"' in script and '"icons": ["clock", "check"]' in script
    assert '"visual_kind": "infographic", "visual_prompt": "Infografía: tres huellas"' in script
    assert '"chart_values": [1.5, 0.0]' in script and '"eyebrow"' not in script  # the eyebrow is the editor's
    assert designer.scene_rules(2) in designer.module_prompt(outline, outline.modules[0], 1, "", "")  # one set of rules


class _Completions:
    def parse(self, **kwargs):
        parsed = designer.QuizDraft(quiz=[])
        message = type("Message", (), {"refusal": None, "parsed": parsed})()
        usage = type("Usage", (), {"prompt_tokens": 1200, "completion_tokens": 300})()
        return type("Completion", (), {"choices": [type("Choice", (), {"message": message})()], "usage": usage})()


def test_the_models_usage_is_kept_for_who_asks():
    llm = OpenAILLM("sk-test", "gpt-4o")
    llm.client = type("Client", (), {"chat": type("Chat", (), {"completions": _Completions()})()})()
    usage = {}
    llm.structured("s", "u", designer.QuizDraft, usage=usage)
    assert usage == {"prompt_tokens": 1200, "completion_tokens": 300}
    assert llm.structured("s", "u", designer.QuizDraft).quiz == []  # without asking, as before


def test_the_proposal_keeps_the_models_usage(client, db, admin_headers, module, worker, monkeypatch):
    real = FakeLLM.structured

    def counted(self, system, user, schema, max_tokens=8000, usage=None):
        if usage is not None:
            usage.update(prompt_tokens=10, completion_tokens=5)
        return real(self, system, user, schema, max_tokens)

    monkeypatch.setattr(FakeLLM, "structured", counted)
    revision = _proposed(client, admin_headers, worker, module.id)
    assert db.get(ModuleRevision, revision["id"]).usage == {"prompt_tokens": 10, "completion_tokens": 5}
