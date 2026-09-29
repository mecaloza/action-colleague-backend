"""
Offline stand-ins for the AI providers (tests, local development and e2e without API keys).

They produce schema-valid, deterministic content quickly, so every flow — outline, storyboard,
quiz, narration, presenter, rendering — runs end to end with real FFmpeg and no network.
"""

import re
import subprocess
import tempfile
import uuid
from pathlib import Path

from app.services.ai import designer
from app.services.video.avatar import AvatarLook, RenderStatus
from app.services.video.voice import Alignment, Speech, VoiceInfo, Word

SECONDS_PER_WORD = 0.32
FALLBACK_SECONDS = 5.0  # length of a fake clip when it can't be measured


def _topic(text: str) -> str:
    words = re.findall(r"\w+", text)
    return " ".join(words[:5]).capitalize() or "Tema del curso"


class FakeLLM:
    def structured(self, system, user, schema, max_tokens=8000):
        if schema is designer.CourseOutline:
            return self._outline(user)
        if schema is designer.ModuleDraft:
            return self._module(user)
        if schema is designer.QuizDraft:
            return designer.QuizDraft(quiz=self._questions())
        raise NotImplementedError(schema.__name__)

    def _outline(self, prompt: str) -> designer.CourseOutline:
        brief = prompt.split("BRIEF DEL ADMINISTRADOR:", 1)[-1].split("AUDIENCIA:", 1)[0].strip()
        topic = _topic(brief)
        count_match = re.search(r"Exactamente (\d+) módulos", prompt)
        count = int(count_match.group(1)) if count_match else 3
        modules = [
            designer.OutlineModule(
                title=f"{topic}: parte {i}",
                summary=f"Qué necesitas saber sobre {topic.lower()} (parte {i}).",
                objectives=[f"Explicar la idea {i}", f"Aplicar la idea {i} en el trabajo"],
                key_points=[f"Punto clave {i}.{n}" for n in range(1, 4)],
                estimated_minutes=3,
                include_quiz=i != 1,
            )
            for i in range(1, count + 1)
        ]
        return designer.CourseOutline(
            title=f"Curso de {topic.lower()}",
            description=f"Un curso práctico sobre {topic.lower()}.",
            audience="Colaboradores de la empresa",
            objectives=["Comprender el tema", "Aplicarlo en el día a día", "Evitar errores comunes"],
            modules=modules,
        )

    def _scene(self, layout: str, title: str, narration: str, **fields) -> designer.SceneDraft:
        base = {"subtitle": "", "points": [], "stat_value": "", "stat_label": "", "quote_author": "",
                "left_heading": "", "left_points": [], "right_heading": "", "right_points": []}
        return designer.SceneDraft(layout=layout, title=title, narration=narration, **{**base, **fields})

    def _module(self, prompt: str) -> designer.ModuleDraft:
        title_match = re.search(r"MÓDULO \d+: (.+)", prompt)
        title = title_match.group(1).strip() if title_match else "Módulo"
        include_quiz = "lista vacía" not in prompt
        scenes = [
            self._scene("cover", title, f"En este módulo vas a aprender {title.lower()}.",
                        subtitle="Lo esencial en pocos minutos"),
            self._scene("bullets", "Lo más importante", "Empecemos por las ideas clave.",
                        points=["Primera idea clave", "Segunda idea clave", "Tercera idea clave"]),
            self._scene("comparison", "Así sí, así no", "Veamos la diferencia.",
                        left_heading="Correcto", left_points=["Hacerlo con cuidado"],
                        right_heading="Incorrecto", right_points=["Hacerlo con prisa"]),
            self._scene("closing", "Para recordar", "Repasemos lo aprendido.", points=["Idea uno", "Idea dos", "Idea tres"]),
        ]
        return designer.ModuleDraft(
            scenes=scenes,
            reading_summary=f"### {title}\n\n- Primera idea clave\n- Segunda idea clave\n- Tercera idea clave",
            quiz=self._questions() if include_quiz else [],
        )

    def _questions(self) -> list[designer.QuestionDraft]:
        empty = {"scenario": "", "options": [], "correct_index": 0, "correct_bool": False, "items": [], "pairs": [],
                 "answers": [], "hint": ""}
        return [
            designer.QuestionDraft(**{**empty, "type": "single_choice", "prompt": "¿Cuál es la primera idea clave?",
                                      "options": ["La primera", "Ninguna", "La última"], "correct_index": 0,
                                      "explanation": "Es la primera."}),
            designer.QuestionDraft(**{**empty, "type": "true_false", "prompt": "Hay que hacerlo con prisa.",
                                      "correct_bool": False, "explanation": "Se hace con cuidado."}),
            designer.QuestionDraft(**{**empty, "type": "ordering", "prompt": "Ordena los pasos",
                                      "items": ["Preparar", "Hacer", "Revisar"], "explanation": "Ese es el orden."}),
            designer.QuestionDraft(**{**empty, "type": "fill_blank", "prompt": "Hay que trabajar con _____.",
                                      "answers": ["cuidado"], "hint": "c...", "explanation": "Con cuidado."}),
        ]


def _tone(seconds: float, dest: Path) -> None:
    subprocess.run(
        ["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i", f"sine=frequency=220:duration={seconds:.2f}",
         "-ar", "44100", "-ac", "1", "-b:a", "96k", str(dest)],
        check=True, capture_output=True,
    )


def _duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    return float(out or FALLBACK_SECONDS)


class FakeVoice:
    def voices(self):
        return [VoiceInfo(id="fake-voice-es", name="Voz de prueba", gender="female", language="es", category="premade")]

    def speak(self, text, voice_id, previous_text="", next_text=""):
        seconds = max(1.0, len(text.split()) * SECONDS_PER_WORD)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "speech.mp3"
            _tone(seconds, path)
            audio = path.read_bytes()
        step = seconds / max(len(text), 1)  # every character gets the same slice of the clip
        chars = range(len(text))
        alignment = Alignment(list(text), [i * step for i in chars], [(i + 1) * step for i in chars])
        return Speech(audio, alignment)

    def clone(self, name, sample, mime_type):
        return f"fake-clone-{uuid.uuid4().hex[:8]}"

    def transcribe(self, media_url, language):
        return [Word("Transcripción", 0.0, 0.8), Word("de", 0.8, 1.0), Word("prueba.", 1.0, 1.6)]


class FakeAvatar:
    """Renders a square test pattern as long as the narration, like a presenter would be."""

    def __init__(self):
        self._durations: dict[str, float] = {}

    def looks(self):
        return [AvatarLook(id="fake-avatar", name="Presentadora de prueba", preview_image_url="", preview_video_url="")]

    def start(self, audio, avatar_id, background, audio_url=None):
        video_id = uuid.uuid4().hex
        self._durations[video_id] = _duration(audio)
        return video_id

    def status(self, video_id):
        return RenderStatus(status="completed", video_url=f"fake://{video_id}")

    def download(self, url, dest):
        duration = self._durations.get(url.removeprefix("fake://"), FALLBACK_SECONDS)
        subprocess.run(
            ["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i",
             f"testsrc2=size=720x720:rate=25:duration={duration:.2f}", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(dest)],
            check=True, capture_output=True,
        )
