"""Animated AI videos: beats revealed with the narration, icons, and the presenter large on the cover."""

import shutil
import subprocess
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image, ImageChops
from pydantic import ValidationError

from app.services.ai import designer
from app.services.slides.render import BUBBLE_BOX, HERO_BOX, beat_count
from app.services.slides.render import render as render_slide
from app.services.slides.spec import Slide, SlideContext
from app.services.video import compose, motion
from app.services.video.captions import TimedWord
from tests.conftest import auth_headers

ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is not installed")

BULLETS = Slide(
    layout="bullets", title="Tu equipo", points=["El casco siempre", "Guantes para químicos", "Gafas al cortar"],
    icons=["hard-hat", "hand", "glasses"],
)


def _words(text: str, start: float, step: float = 0.4) -> list[TimedWord]:
    return [TimedWord(word, start + index * step, start + index * step + 0.3) for index, word in enumerate(text.split())]


def _region_changed(a: Image.Image, b: Image.Image, box: tuple[int, int, int, int]) -> bool:
    return ImageChops.difference(a.crop(box), b.crop(box)).getbbox() is not None


# ── Beat timing ───────────────────────────────────────────────────────


def test_each_point_appears_when_the_narration_names_it():
    narration = "Primero el casco, después los guantes y al final las gafas"
    words = _words(narration, start=10.0)
    times = motion.beat_times(BULLETS, words, start=10.0, length=5.0)

    said = {word.text.strip(",").lower(): word.start for word in words}
    assert times[0] == pytest.approx(said["casco"] - motion.LEAD_SECONDS)
    assert times[1] == pytest.approx(said["guantes"] - motion.LEAD_SECONDS)
    assert times[2] == pytest.approx(said["gafas"] - motion.LEAD_SECONDS)


def test_points_the_narration_never_names_are_spread_in_order_and_on_time():
    times = motion.beat_times(BULLETS, _words("Hablemos de protección personal", 0.0), start=0.0, length=6.0)

    assert times == sorted(times)
    assert all(later - earlier >= motion.MIN_BEAT_GAP - 1e-9 for earlier, later in zip(times, times[1:]))
    assert times[0] >= motion.FIRST_BEAT_DELAY and times[-1] <= 6.0


def test_a_short_scene_still_shows_every_beat_before_it_ends():
    times = motion.beat_times(BULLETS, [], start=3.0, length=1.0)
    assert len(times) == 3 and all(3.0 < at <= 3.0 + 1.0 for at in times)


def test_in_a_short_scene_the_beats_share_the_time_evenly():
    five = Slide(layout="bullets", title="T", points=["a uno", "b dos", "c tres", "d cuatro", "e cinco"])
    times = motion.beat_times(five, [], start=0.0, length=3.0)
    gaps = [later - earlier for earlier, later in zip(times, times[1:])]
    assert min(gaps) > 0.5 and max(gaps) - min(gaps) < 0.05  # none piled up at the end


def test_a_plural_in_the_narration_still_reveals_its_point():
    slide = Slide(layout="bullets", title="T", points=["Una idea", "Otra cosa"])
    words = _words("Primero ideas sueltas y después otra cosa", 0.0)
    assert motion.beat_times(slide, words, 0.0, 5.0)[0] == pytest.approx(0.4 - motion.LEAD_SECONDS)


def test_a_headline_slide_shows_its_text_at_once():
    statement = Slide(layout="statement", title="La seguridad es primero", quote_author="Jefa de planta")
    times = motion.beat_times(statement, _words("Recuerda esta frase de la jefa de planta", 2.0), start=2.0, length=4.0)
    assert times[0] == pytest.approx(2.0 + motion.FIRST_BEAT_DELAY)
    assert len(times) == beat_count(statement) - 1


# ── Beats and icons in the slide ──────────────────────────────────────


def test_beats_reveal_items_without_moving_the_ones_already_shown():
    context = SlideContext(index=2, total=4)
    full = render_slide(BULLETS, context)
    title_only = render_slide(BULLETS, SlideContext(index=2, total=4, reveal=1))
    two = render_slide(BULLETS, SlideContext(index=2, total=4, reveal=3))

    lower_half = (0, 540, 1920, 1080)
    assert _region_changed(title_only, full, lower_half)  # the points are not there yet
    assert ImageChops.difference(two, full).getbbox()[1] > 300  # only the last point (lower down) is missing
    first_point = (0, 250, 1920, 420)
    assert not _region_changed(two, full, first_point)  # what is shown sits where it does on the full slide


def test_icons_replace_the_numbers_and_unknown_ones_are_drawn_as_none():
    plain = render_slide(BULLETS.model_copy(update={"icons": []}), SlideContext())
    with_icons = render_slide(BULLETS, SlideContext())
    assert ImageChops.difference(plain, with_icons).getbbox() is not None

    # A name no longer in the catalog doesn't break a saved script; extra icons are dropped.
    slide = Slide(layout="bullets", title="T", points=["a"], icons=["not-an-icon", "hand"], icon="gone")
    assert slide.icons == [""] and slide.icon == ""
    with pytest.raises(ValidationError):
        Slide(layout="bullets", title="T", points=["a"], icons=[7])


def test_a_blank_point_does_not_shift_the_icons():
    gapped = Slide(layout="bullets", title="T", points=["Casco", " ", "Gafas"], icons=["hard-hat", "hand", "glasses"])
    clean = Slide(layout="bullets", title="T", points=["Casco", "Gafas"], icons=["hard-hat", "glasses"])
    assert ImageChops.difference(render_slide(gapped, SlideContext()), render_slide(clean, SlideContext())).getbbox() is None


def test_the_preview_shows_the_cover_as_the_video_does(client, admin):
    cover = {"layout": "cover", "title": "Bienvenida", "subtitle": "Seguridad"}
    context = {"index": 1, "total": 5, "presenter": True}
    headers = auth_headers(client, admin.email)
    preview = client.post("/api/v1/slides/preview", json={"slide": cover, "context": context}, headers=headers)
    assert preview.status_code == 200, preview.text
    shown = Image.open(BytesIO(preview.content)).convert("RGB")
    video = render_slide(Slide(**cover), SlideContext(index=1, total=5, hero=True)).resize(shown.size, Image.LANCZOS)
    assert ImageChops.difference(shown, video).getbbox() is None


def test_the_model_s_icons_stay_with_their_points():
    scene = designer.SceneDraft(
        layout="bullets", title="T", subtitle="", points=["Casco", " ", "Gafas"], icons=["hard-hat", "x", "glasses"],
        icon="", stat_value="", stat_label="", quote_author="", left_heading="", left_points=[], right_heading="",
        right_points=[], narration="n",
    )
    slide = designer.to_slide(scene)
    assert slide.points == ["Casco", "Gafas"] and slide.icons == ["hard-hat", "glasses"]


def test_the_hero_cover_keeps_its_text_clear_of_the_presenter():
    cover = Slide(layout="cover", title="Un título bastante largo para el curso de seguridad", subtitle="Subtítulo")
    hero = render_slide(cover, SlideContext(hero=True))
    left, top, right, bottom = HERO_BOX
    background = render_slide(Slide(layout="cover", title=" "), SlideContext(hero=True))
    assert not _region_changed(hero, background, (left - 40, top, right, bottom))


# ── The video ─────────────────────────────────────────────────────────


def _scene_images(tmp_path: Path, slide: Slide, name: str, context: SlideContext) -> list[Path]:
    paths = []
    for shown in range(1, beat_count(slide) + 1):
        path = tmp_path / f"{name}_{shown}.png"
        render_slide(slide, SlideContext(**{**context.__dict__, "reveal": shown})).save(path)
        paths.append(path)
    return paths


def _frame(video: Path, at: float, dest: Path) -> Image.Image:
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-ss", f"{at:.3f}", "-i", str(video), "-frames:v", "1",
                    str(dest)], check=True, capture_output=True)
    return Image.open(dest).convert("RGB")


def _silent_narration(tmp_path: Path, lengths: list[float]) -> compose.Narration:
    scenes = []
    for index, length in enumerate(lengths):
        path = tmp_path / f"silence{index}.wav"
        subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-t", str(length), "-i",
                        "anullsrc=r=48000:cl=mono", str(path)], check=True, capture_output=True)
        scenes.append(path)
    return compose.build_narration(scenes, tmp_path)


@ffmpeg
def test_points_appear_in_the_video_at_their_beat(tmp_path):
    narration = _silent_narration(tmp_path, [6.0])
    images = _scene_images(tmp_path, BULLETS, "bullets", SlideContext(index=1, total=1))
    start = narration.starts[0]
    times = [start + 1.5, start + 3.0, start + 4.5]
    output = tmp_path / "video.mp4"
    compose.compose_video([motion.Scene(images, start, times)], narration, None, output, tmp_path)

    full = Image.open(images[-1]).convert("RGB")
    before_last = _frame(output, times[2] - 0.3, tmp_path / "a.png")
    after_last = _frame(output, times[2] + motion.ENTRANCE_SECONDS + 0.3, tmp_path / "b.png")
    last_point = (0, ImageChops.difference(Image.open(images[-2]).convert("RGB"), full).getbbox()[1], 1920, 1080)
    assert _mean_difference(before_last, full, last_point) > _mean_difference(after_last, full, last_point) * 3


def _mean_difference(a: Image.Image, b: Image.Image, box: tuple[int, int, int, int]) -> float:
    diff = ImageChops.difference(a.crop(box), b.crop(box)).convert("L")
    return sum(diff.histogram()[i] * i for i in range(256)) / (diff.width * diff.height)


@ffmpeg
def test_the_presenter_is_large_on_the_cover_and_in_the_bubble_after(tmp_path):
    from app.services.ai.fake import FakeAvatar

    narration = _silent_narration(tmp_path, [2.5, 2.5])
    cover = Slide(layout="cover", title="Bienvenida", subtitle="Seguridad")
    hero_images = _scene_images(tmp_path, cover, "cover", SlideContext(index=1, total=2, hero=True))
    bullet_images = _scene_images(tmp_path, BULLETS, "bullets", SlideContext(index=2, total=2, presenter=True))
    scenes = [
        motion.Scene(hero_images, narration.starts[0], [narration.starts[0] + 0.2, narration.starts[0] + 0.8]),
        motion.Scene(bullet_images, narration.starts[1], [narration.starts[1] + 0.3, narration.starts[1] + 0.9,
                                                          narration.starts[1] + 1.5]),
    ]
    presenter = FakeAvatar()
    clip = tmp_path / "narration.mp3"
    compose.to_mp3(narration.audio, clip)
    video_id = presenter.start("fake-avatar", "#1D1D1D", idempotency_key="t", audio_asset_id=presenter.upload_audio(clip))
    avatar = tmp_path / "presenter.mp4"
    presenter.download(f"fake://{video_id}", avatar)

    with_presenter, without = tmp_path / "with.mp4", tmp_path / "without.mp4"
    compose.compose_video(scenes, narration, avatar, with_presenter, tmp_path, hero=[True, False])
    compose.compose_video(scenes, narration, None, without, tmp_path)

    hero_center = ((HERO_BOX[0] + HERO_BOX[2]) // 2, (HERO_BOX[1] + HERO_BOX[3]) // 2)
    bubble_center = ((BUBBLE_BOX[0] + BUBBLE_BOX[2]) // 2, (BUBBLE_BOX[1] + BUBBLE_BOX[3]) // 2)

    def presenter_at(at: float, point: tuple[int, int]) -> bool:
        a, b = _frame(with_presenter, at, tmp_path / "p.png"), _frame(without, at, tmp_path / "q.png")
        return sum(abs(x - y) for x, y in zip(a.getpixel(point), b.getpixel(point))) > 60

    on_cover, on_bullets = narration.starts[0] + 1.0, narration.starts[1] + 1.0
    assert presenter_at(on_cover, hero_center) and not presenter_at(on_cover, bubble_center)
    assert presenter_at(on_bullets, bubble_center) and not presenter_at(on_bullets, hero_center)


def test_points_with_shared_words_never_arrive_together():
    slide = Slide(layout="bullets", title="T", points=["Primera idea clave", "Segunda idea clave", "Tercera idea clave"])
    from app.services.video.captions import words_from_text

    times = motion.beat_times(slide, words_from_text("Empecemos por las ideas clave.", 5.26, 6.86), 5.26, 1.6)
    gaps = [later - earlier for earlier, later in zip(times, times[1:])]
    assert min(gaps) > 0.4 and times[-1] <= 6.86


def test_the_large_presenter_leaves_before_the_next_slide_slides_in():
    scenes = [motion.Scene([Path("a")], 0.4, []), motion.Scene([Path("b")], 5.0, []), motion.Scene([Path("c")], 9.0, [])]
    intervals = compose.hero_intervals(scenes, [True, False, True], 12.0)
    assert intervals == [(0.0, 5.0 - motion.TRANSITION_SECONDS), (9.0, 12.0)]
