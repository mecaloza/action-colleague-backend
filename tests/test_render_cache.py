"""Producing a module's video again pays only for what changed (real FFmpeg, the offline fake voice and presenter):
each scene's speech and each presenter's take are kept by content, a proposed change says exactly what producing the
video again will pay, a render that fails or finishes never loses what was kept, and deleting the course deletes it."""

import copy
import json
import shutil
import subprocess

import pytest

from app.db.models import Course, Job, MediaAsset, Module
from app.services import revisions
from app.services.ai.fake import FakeAvatar, FakeVoice
from app.services.revisions import Production
from app.services.storage import get_storage
from app.services.video import cache as video_cache
from app.services.video import compose
from app.services.video.voice import Speech, VoiceError
from app.worker.jobs import render as render_job
from tests.conftest import auth_headers
from tests.test_render_resilience import THREE_SCENES
from tests.test_video import RENDER_CHOICES, _run_until_done

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is not installed")

TWO_PRESENTERS = {**RENDER_CHOICES, "co_avatar_id": "fake-avatar-2", "co_voice_id": "fake-voice-2"}
NOTHING = {"speak": [], "start": [], "upload_audio": []}


@pytest.fixture
def admin_headers(client, admin):
    return auth_headers(client, admin.email)


@pytest.fixture
def ai_module(db, admin):
    course = Course(title="Seguridad", status="draft", source="ai", settings={}, created_by=admin.id)
    db.add(course)
    db.flush()
    module = Module(course_id=course.id, title="Casco", order=1, source="ai", content_text="Resumen",
                    storyboard={"scenes": copy.deepcopy(THREE_SCENES)})
    db.add(module)
    db.commit()
    return module


@pytest.fixture
def paid(monkeypatch):
    """What producing the videos asks the providers for: speech (text, voice), presenter takes (avatar) and the
    narrations handed to HeyGen."""
    calls = copy.deepcopy(NOTHING)
    speak, start, upload_audio = FakeVoice.speak, FakeAvatar.start, FakeAvatar.upload_audio

    def counted_speak(self, text, voice_id, previous_text="", next_text=""):
        calls["speak"].append((text, voice_id))
        return speak(self, text, voice_id, previous_text, next_text)

    def counted_start(self, avatar_id, background, **request):
        calls["start"].append(avatar_id)
        return start(self, avatar_id, background, **request)

    def counted_upload(self, audio):
        calls["upload_audio"].append(audio.name)
        return upload_audio(self, audio)

    monkeypatch.setattr(FakeVoice, "speak", counted_speak)
    monkeypatch.setattr(FakeAvatar, "start", counted_start)
    monkeypatch.setattr(FakeAvatar, "upload_audio", counted_upload)
    return calls


def _forget(paid) -> None:
    for calls in paid.values():
        calls.clear()


def _produce(client, headers, db, worker, module, choices: dict | None = None) -> Module:
    """The admin produces the module's video (with these voice and presenter choices, the first time) and it is made."""
    if choices is None:
        response = client.post(f"/api/v1/modules/{module.id}/render", headers=headers)
    else:
        response = client.post(f"/api/v1/courses/{module.course_id}/render", headers=headers, json=choices)
    assert response.status_code == 202, response.text
    _run_until_done(db, worker)
    db.expire_all()
    produced = db.get(Module, module.id)
    assert produced.generation_status == "completed", produced.generation_error
    return produced


def _edit(client, headers, module_id: int, edit) -> None:
    """The admin edits the script in the editor and saves it."""
    storyboard = client.get(f"/api/v1/modules/{module_id}/storyboard", headers=headers).json()
    edit(storyboard["scenes"])
    saved = client.put(f"/api/v1/modules/{module_id}/storyboard", headers=headers, json={"scenes": storyboard["scenes"]})
    assert saved.status_code == 200, saved.text


def _kept(storage, folder: str, pattern: str = "*") -> list[str]:
    """The files kept for the next videos in one of the course's folders (narration, presenter)."""
    return sorted(path.name for path in (storage.root / storage.bucket).glob(f"courses/*/{folder}/{pattern}"))


def _speech_kept(storage) -> list[str]:
    """The speech clips kept (each with its timings beside it, and its key's latest naming one)."""
    return _kept(storage, "narration", "*.mp3")


def _leftovers(storage) -> list[str]:
    """What renders leave in their own folders while they work."""
    return [str(path) for path in (storage.root / storage.bucket).rglob("render/*/*")]


def _captions(db, storage, module: Module) -> str:
    return storage.file_path(db.get(MediaAsset, module.captions_asset_id).path).read_text(encoding="utf-8")


def _renders(db) -> list[Job]:
    return db.query(Job).filter(Job.type == "video.render").order_by(Job.created_at, Job.id).all()


# ── Producing again ───────────────────────────────────────────────────


def test_a_change_to_the_slides_alone_is_produced_again_without_asking_any_provider(
    client, db, admin_headers, ai_module, storage, worker, paid
):
    module = _produce(client, admin_headers, db, worker, ai_module, RENDER_CHOICES)
    assert [voice for _, voice in paid["speak"]] == ["fake-voice-es"] * 3 and paid["start"] == ["fake-avatar"]
    [first] = _renders(db)
    video, captions = module.video_asset_id, _captions(db, storage, module)
    timings = {key: first.state[key] for key in ("starts", "beats")}
    _forget(paid)
    _edit(client, admin_headers, ai_module.id, lambda scenes: scenes[1]["slide"].update(title="Las reglas del casco"))

    module = _produce(client, admin_headers, db, worker, ai_module)

    assert paid == NOTHING  # neither narrated nor recorded again: only composed
    assert module.video_asset_id != video and revisions.stale_scene_ids(module.storyboard) == []
    # Same speech, same timings: the captions and the points' entrances come from the timings kept with it.
    assert _captions(db, storage, module) == captions
    [second] = [job for job in _renders(db) if job.id != first.id]
    assert {key: second.state[key] for key in ("starts", "beats")} == timings
    # What was kept stays for the next videos; the jobs' own files are gone.
    assert len(_speech_kept(storage)) == 3 and len(_kept(storage, "narration")) == 9  # with timings and latest
    assert len(_kept(storage, "presenter")) == 1 and not _leftovers(storage)


def test_each_scenes_speech_is_kept_with_its_timings_and_the_video_records_what_it_left(
    client, db, admin_headers, ai_module, storage, worker
):
    module = _produce(client, admin_headers, db, worker, ai_module, RENDER_CHOICES)

    record = module.storyboard["render"]["cache"]
    keys = [video_cache.narration_key("fake-voice-es", scene["narration"]) for scene in THREE_SCENES]
    assert list(record["clips"]) == keys and len(record["takes"]) == 1
    clip, timings = video_cache.narration_paths(module.course_id, keys[0], record["clips"][keys[0]])
    latest = json.loads(storage.file_path(video_cache.latest_path(module.course_id, keys[0])).read_text())
    assert latest == {"digest": record["clips"][keys[0]]}
    kept = json.loads(storage.file_path(timings).read_text(encoding="utf-8"))
    text = THREE_SCENES[0]["narration"]
    assert (kept["text"], kept["voice_id"], kept["model"]) == (text, "fake-voice-es", "eleven_multilingual_v2")
    assert kept["digest"] == video_cache.digest(storage.file_path(clip).read_bytes()) == record["clips"][keys[0]]
    assert "".join(kept["alignment"]["characters"]) == text
    assert " ".join(word["text"] for word in kept["words"]) == text
    assert kept["words"][0]["start"] == 0.0 and kept["words"][-1]["end"] > kept["words"][0]["end"]
    assert storage.file_path(video_cache.take_path(module.course_id, record["takes"][0])).exists()


def test_a_scene_narrated_anew_costs_its_speech_and_its_presenters_take_only(
    client, db, admin_headers, ai_module, storage, worker, paid
):
    _produce(client, admin_headers, db, worker, ai_module, TWO_PRESENTERS)
    assert [voice for _, voice in paid["speak"]] == ["fake-voice-es", "fake-voice-2", "fake-voice-es"]
    assert sorted(paid["start"]) == ["fake-avatar", "fake-avatar-2"]
    _forget(paid)
    said = "Hay dos reglas: siempre puesto y revisado antes de cada turno."
    _edit(client, admin_headers, ai_module.id, lambda scenes: scenes[1].update(narration=said))

    _produce(client, admin_headers, db, worker, ai_module)

    # Its neighbours keep their speech even though the hints sent along with them changed.
    assert paid["speak"] == [(said, "fake-voice-2")]
    assert paid["start"] == ["fake-avatar-2"] and len(paid["upload_audio"]) == 1  # only who says it is recorded

    # Undoing the change is free: the scene's first speech and the take over it are still kept.
    _forget(paid)
    _edit(client, admin_headers, ai_module.id, lambda scenes: scenes[1].update(narration=THREE_SCENES[1]["narration"]))
    module = _produce(client, admin_headers, db, worker, ai_module)
    assert paid == NOTHING
    assert len(module.storyboard["render"]["cache"]["takes"]) == 3  # every take its videos left, both versions


def test_a_scene_added_at_the_end_records_only_its_own_presenter(
    client, db, admin_headers, ai_module, storage, worker, paid
):
    _produce(client, admin_headers, db, worker, ai_module, TWO_PRESENTERS)
    _forget(paid)
    closing = {**THREE_SCENES[0], "id": "s4", "narration": "Repasemos lo que aprendiste hoy."}
    _edit(client, admin_headers, ai_module.id, lambda scenes: scenes.append(closing))

    _produce(client, admin_headers, db, worker, ai_module)

    # The scene before it, now followed by another, keeps the first presenter's take (only the silence after changed).
    assert paid["speak"] == [(closing["narration"], "fake-voice-2")] and paid["start"] == ["fake-avatar-2"]


def test_a_proposed_change_says_exactly_what_producing_the_video_again_pays(
    client, db, admin_headers, ai_module, storage, worker, paid
):
    _produce(client, admin_headers, db, worker, ai_module, TWO_PRESENTERS)
    _forget(paid)

    def proposed(observation: str) -> dict:
        asked = client.post(f"/api/v1/modules/{ai_module.id}/revisions", headers=admin_headers,
                            json={"observation": observation, "scene_ids": ["s2"]})
        assert asked.status_code == 202, asked.text
        worker()
        revision = client.get(f"/api/v1/revisions/{asked.json()['revision']['id']}", headers=admin_headers).json()
        assert revision["status"] == "proposed", revision
        return revision

    title = proposed("Haz el título más claro")["changes"]["impact"]
    assert title == {"narration_scenes": [], "narration_chars": 0, "presenter_takes": 0, "reassigns_presenters": False,
                     "visuals_to_make": 0, "needs_render": True}

    revision = proposed("Cambia la narración y el título")  # "Pedir otro ajuste": this one replaces it
    said = revision["changes"]["scenes"][0]["after"]["narration"]
    assert revision["changes"]["impact"] == {
        "narration_scenes": ["s2"], "narration_chars": len(said), "presenter_takes": 1, "reassigns_presenters": False,
        "visuals_to_make": 0, "needs_render": True,
    }
    applied = client.post(f"/api/v1/revisions/{revision['id']}/apply", headers=admin_headers, json={"render": True})
    assert applied.status_code == 200 and applied.json()["render_job"], applied.text
    _run_until_done(db, worker)

    assert paid["speak"] == [(said, "fake-voice-2")] and paid["start"] == ["fake-avatar-2"]  # what it said, no more
    db.expire_all()
    assert revisions.stale_scene_ids(db.get(Module, ai_module.id).storyboard) == []


def test_a_pending_proposal_tells_its_cost_as_of_now(client, db, admin_headers, ai_module, storage, worker):
    course = db.get(Course, ai_module.course_id)
    course.settings = dict(RENDER_CHOICES)  # the voice and the presenter are chosen; no video yet
    db.commit()
    asked = client.post(f"/api/v1/modules/{ai_module.id}/revisions", headers=admin_headers,
                        json={"observation": "Haz el título más claro", "scene_ids": ["s2"]})
    assert asked.status_code == 202, asked.text
    worker()
    url = f"/api/v1/revisions/{asked.json()['revision']['id']}"
    impact = client.get(url, headers=admin_headers).json()["changes"]["impact"]
    assert impact["narration_scenes"] == ["s1", "s2", "s3"] and impact["presenter_takes"] == 1  # nothing kept yet
    assert not impact["needs_render"]

    _produce(client, admin_headers, db, worker, ai_module)  # its video is made while the proposal waits

    revision = client.get(url, headers=admin_headers).json()
    assert revision["status"] == "proposed" and revision["changes"]["impact"] == {
        "narration_scenes": [], "narration_chars": 0, "presenter_takes": 0, "reassigns_presenters": False,
        "visuals_to_make": 0, "needs_render": True,
    }


# ── Nothing kept is lost ──────────────────────────────────────────────


def test_a_render_that_fails_keeps_the_speech_it_already_paid_for(
    client, db, admin_headers, ai_module, storage, worker, paid, monkeypatch
):
    speak = FakeVoice.speak

    def out_of_credits_at_the_third(self, text, voice_id, previous_text="", next_text=""):
        if "tercera" in text:
            raise VoiceError("Se acabaron los créditos de ElevenLabs; recárgalos para seguir narrando.", permanent=True)
        return speak(self, text, voice_id, previous_text, next_text)

    monkeypatch.setattr(FakeVoice, "speak", out_of_credits_at_the_third)
    started = client.post(f"/api/v1/courses/{ai_module.course_id}/render", headers=admin_headers, json=RENDER_CHOICES)
    assert started.status_code == 202, started.text
    _run_until_done(db, worker)
    db.expire_all()
    assert _renders(db)[0].status == "failed" and not _leftovers(storage)
    assert len(_speech_kept(storage)) == 2  # the first two scenes' speech (the module has no video to record it)

    monkeypatch.setattr(FakeVoice, "speak", speak)  # the credits are back
    _forget(paid)
    _produce(client, admin_headers, db, worker, ai_module)
    assert [text for text, _ in paid["speak"]] == [THREE_SCENES[2]["narration"]]


def test_speech_ffmpeg_cannot_read_is_never_kept(client, db, admin_headers, ai_module, storage, worker, paid, monkeypatch):
    speak, garbled = FakeVoice.speak, []

    def garbled_once(self, text, voice_id, previous_text="", next_text=""):
        speech = speak(self, text, voice_id, previous_text, next_text)
        if "Hay dos reglas" in text and not garbled:
            garbled.append(text)
            return Speech(b"no es un mp3", speech.alignment)
        return speech

    monkeypatch.setattr(FakeVoice, "speak", garbled_once)
    started = client.post(f"/api/v1/courses/{ai_module.course_id}/render", headers=admin_headers, json=RENDER_CHOICES)
    assert started.status_code == 202, started.text
    worker()  # the first scene is kept, the second arrives broken: the job tries again later
    db.expire_all()
    job = _renders(db)[0]
    assert job.status == "queued" and "escena 2 llegó dañada" in job.error
    assert len(_speech_kept(storage)) == 1  # only the first scene's speech

    _run_until_done(db, worker)
    db.expire_all()
    assert _renders(db)[0].status == "succeeded"
    first, second, third = (scene["narration"] for scene in THREE_SCENES)
    assert [text for text, _ in paid["speak"]] == [first, second, second, third]  # the second asked again, once


def test_a_take_ffmpeg_cannot_decode_is_not_kept_for_the_next_video(
    client, db, admin_headers, ai_module, storage, worker, paid, monkeypatch
):
    download = FakeAvatar.download

    def without_picture(self, url, dest):  # it reads as long as it should, but there is no picture in it
        subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=duration=8",
                        "-c:a", "aac", "-f", "mp4", str(dest)], check=True, capture_output=True)

    monkeypatch.setattr(FakeAvatar, "download", without_picture)
    module = _produce(client, admin_headers, db, worker, ai_module, RENDER_CHOICES)
    render = module.storyboard["render"]
    assert render["avatar_id"] == "" and "sin presentador" in render["warning"]
    assert render["cache"]["takes"] == [] and not _kept(storage, "presenter")  # it would leave the next one out too

    monkeypatch.setattr(FakeAvatar, "download", download)
    _forget(paid)
    module = _produce(client, admin_headers, db, worker, ai_module)
    assert paid["speak"] == [] and paid["start"] == ["fake-avatar"]  # HeyGen asked again; the speech is kept
    assert module.storyboard["render"]["avatar_id"] == "fake-avatar" and len(_kept(storage, "presenter")) == 1


def test_speech_another_render_keeps_meanwhile_never_changes_what_a_video_reuses(
    client, db, admin_headers, ai_module, storage, worker, paid, tmp_path
):
    module = _produce(client, admin_headers, db, worker, ai_module, RENDER_CHOICES)
    captions = _captions(db, storage, module)
    # Another module says the same in the same voice and its render narrated it too, at the same time: its clip (it
    # lasts and sounds a little different) is kept as well, with its own timings, and is that narration's latest.
    text = THREE_SCENES[0]["narration"]
    key = video_cache.narration_key("fake-voice-es", text)
    other = FakeVoice().speak(f"{text} Otra toma.", "fake-voice-es")
    (tmp_path / "otra.mp3").write_bytes(other.audio)
    (tmp_path / "otra.json").write_text(json.dumps(render_job._timings("fake-voice-es", text, other)))
    render_job._keep_speech(module.course_id, key, tmp_path / "otra.mp3", tmp_path / "otra.json", tmp_path / "l.json")
    _forget(paid)
    _edit(client, admin_headers, ai_module.id, lambda scenes: scenes[1]["slide"].update(title="Las reglas del casco"))

    module = _produce(client, admin_headers, db, worker, ai_module)

    # The module's video reuses the clip its last one used, the one the change's cost counted on: nothing is narrated
    # or recorded again, and its words keep their timings. Both clips stay, each with its own timings.
    assert paid == NOTHING and _captions(db, storage, module) == captions
    assert len([name for name in _speech_kept(storage) if name.startswith(key)]) == 2


def test_deleting_the_course_deletes_what_its_videos_kept(client, db, admin_headers, ai_module, storage, worker):
    _produce(client, admin_headers, db, worker, ai_module, RENDER_CHOICES)
    assert _kept(storage, "narration") and _kept(storage, "presenter")

    assert client.delete(f"/api/v1/courses/{ai_module.course_id}", headers=admin_headers).status_code == 204

    assert not _kept(storage, "narration") and not _kept(storage, "presenter")


def test_a_render_narrated_by_the_previous_release_still_publishes_and_cleans_up(
    client, db, admin_headers, ai_module, storage, worker, monkeypatch
):
    narrate = render_job._narrate

    def narrate_like_the_previous_release(ctx, render, work):
        narrate(ctx, render, work)
        # Its speech went to the job's own folder, with the timings in the state and no digests.
        for index in list(ctx.state["narration"]):
            own = f"{render_job._intermediate_prefix(render, ctx)}/scene_{int(index):02d}.mp3"
            get_storage().upload_file(own, work / f"scene_{int(index):02d}.mp3", "audio/mpeg")
            ctx.state["narration"][index] = {"path": own, "alignment": None}

    monkeypatch.setattr(render_job, "_narrate", narrate_like_the_previous_release)
    module = _produce(client, admin_headers, db, worker, ai_module, RENDER_CHOICES)

    assert module.storyboard["render"]["avatar_id"] == "fake-avatar"
    assert module.storyboard["render"]["cache"] == {"clips": {}, "takes": []}  # nothing it can tell apart
    assert not _leftovers(storage) and not _kept(storage, "presenter")  # its take, its own: deleted as before


# ── On their own ──────────────────────────────────────────────────────


def test_a_renders_intermediate_files_never_include_what_is_kept():
    kept_clip, kept_take = "courses/7/narration/abc.mp3", "courses/7/presenter/def.mp4"
    own = "courses/7/modules/3/render/job/scene_01.mp3"
    state = {
        "narration": {"0": {"path": kept_clip, "timings": "courses/7/narration/abc.json", "key": "abc"},
                      "1": {"path": own, "alignment": None}},  # narrated by the previous release
        "master_path": "courses/7/modules/3/render/job/master.flac", "avatar_path": kept_take,
        "take_paths": ["courses/7/modules/3/render/job/heygen_co.mp3"],
    }
    assert render_job.intermediate_paths(state) == [
        own, "courses/7/modules/3/render/job/master.flac", "courses/7/modules/3/render/job/heygen_co.mp3",
    ]
    assert video_cache.is_kept("courses/7/visuals/x.png") and not video_cache.is_kept("courses/7/uploads/narration")


def test_the_record_keeps_what_every_video_left_but_the_takes_dropped():
    first = video_cache.record(None, {"a": "1", "b": "2"}, ["t1"])
    second = video_cache.record(first, {"b": "2", "c": "3"}, ["t2", "t2"])
    assert second == {"clips": {"a": "1", "b": "2", "c": "3"}, "takes": ["t1", "t2"]}
    assert video_cache.record(second, {}, ["t2"], dropped=["t2"])["takes"] == ["t1"]
    many = video_cache.record(None, {f"k{n}": "d" for n in range(400)}, [f"t{n}" for n in range(80)])
    assert len(many["clips"]) == video_cache.MAX_RECORDED_CLIPS and "k399" in many["clips"] and "k0" not in many["clips"]
    assert many["takes"][-1] == "t79" and len(many["takes"]) == video_cache.MAX_RECORDED_TAKES


def test_a_take_is_told_damaged_only_when_ffmpeg_cannot_decode_it(tmp_path):
    take = tmp_path / "toma.mp4"
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=160x160:duration=4",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(take)], check=True, capture_output=True)
    data = bytearray(take.read_bytes())
    middle = len(data) // 3
    data[middle:middle + 4000] = bytes((byte * 7 + 13) % 256 for byte in data[middle:middle + 4000])
    damaged = tmp_path / "dañada.mp4"
    damaged.write_bytes(bytes(data))

    assert compose.decodes(take)
    assert compose.clip_seconds(damaged) == 4 and not compose.decodes(damaged)  # its length reads fine all the same


def test_a_take_is_told_apart_by_its_speech_and_where_it_starts_not_by_what_follows_it():
    def key(*clips):
        return video_cache.take_key("avatar", "avatar_iv", clips)

    assert video_cache.scene_roles(3) == ["first", "next", "next"] and video_cache.scene_roles(0) == []
    assert key(("d1", "first"), ("d3", "next")) == key(("d1", "first"), ("d3", "next"))
    assert key(("d1", "first"), ("d3", "next")) != key(("d1", "next"), ("d3", "next"))  # the lead-in moves the rest
    assert key(("d1", "first"), ("d3", "next")) != key(("d1", "first"), ("d4", "next"))
    assert video_cache.take_key("otro", "avatar_iv", [("d1", "first")]) != key(("d1", "first"))


def test_the_impact_counts_what_the_kept_speech_and_takes_dont_cover():
    scenes = [{"id": scene["id"], "narration": scene["narration"]} for scene in THREE_SCENES]
    script = {"scenes": scenes, "reading": ""}
    two = Production(2, True, ("voz", "voz-2"), ("a", "b"), "avatar_iv")
    keys = two.narration_keys(scenes)
    digests = [f"clip-{key}" for key in keys]
    made = Production(2, True, two.voices, two.avatars, two.engine, video_cache.Kept(
        video_cache.record(None, dict(zip(keys, digests)), list(filter(None, two.take_keys(scenes, digests))))
    ))

    def impact(production, after=script):
        return revisions.impact(script, after, production)

    # A video made before its speech was kept (or no video yet): everything is paid once.
    everything = impact(two)
    assert everything["narration_scenes"] == ["s1", "s2", "s3"] and everything["presenter_takes"] == 2
    assert impact(made)["narration_scenes"] == [] and impact(made)["presenter_takes"] == 0
    # Another voice for the course: every scene it says is narrated anew, and its presenter recorded again.
    other_voice = Production(2, True, ("voz", "otra"), two.avatars, two.engine, made.kept)
    assert (impact(other_voice)["narration_scenes"], impact(other_voice)["presenter_takes"]) == (["s2"], 1)
    other_engine = Production(2, True, two.voices, two.avatars, "avatar_v", made.kept)
    assert (impact(other_engine)["narration_scenes"], impact(other_engine)["presenter_takes"]) == ([], 2)
    edited = copy.deepcopy(script)
    edited["scenes"][2]["narration"] = "Otra escena."
    assert impact(made, edited) == {
        "narration_scenes": ["s3"], "narration_chars": 12, "presenter_takes": 1, "reassigns_presenters": False,
        "visuals_to_make": 0, "needs_render": True,
    }
    # Without a presenter there is no take to record.
    assert impact(Production(0, True, ("voz",)))["presenter_takes"] == 0
