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
        if schema is designer.ActivitiesDraft:
            return designer.ActivitiesDraft(activities=self._activities())
        raise NotImplementedError(schema.__name__)

    def _activities(self) -> list[designer.ActivityDraft]:
        """One activity of each kind."""
        empty = {"situations": [], "inspection_prompt": "", "scene": "cards", "verdicts": [], "inspect_items": [],
                 "simulator_prompt": "", "variables": [], "formula": "", "output_label": "", "output_unit": "",
                 "output_decimals": 0, "target_op": "lte", "target_value": 0, "target_value2": 0,
                 "success_text": "", "fail_text": "", "hint": "", "quick_mode": "order", "quick_prompt": "", "items": [], "pairs": [], "seconds": 0}
        choice = designer.ChoiceDraft
        decision = designer.ActivityDraft(**{**empty, "type": "decision", "title": "Cliente que solo pide precio",
            "intro": "Atiende al cliente.", "situations": [
                designer.SituationDraft(speaker="Cliente", situation="Quiero la llanta más barata.", choices=[
                    choice(text="Le pregunto por su ruta.", points=8, feedback="Entiendes su operación.", next_situation=1),
                    choice(text="Le doy la más barata.", points=2, feedback="El costo por km sube.", next_situation=-1)]),
                designer.SituationDraft(speaker="Cliente", situation="Hago carretera con mucha carga.", choices=[
                    choice(text="Le muestro el costo por km.", points=6, feedback="Ve el ahorro.", next_situation=-1),
                    choice(text="Le hablo de la marca.", points=1, feedback="Sin números no convence.", next_situation=-1)]),
            ]})
        tire = designer.ItemDraft
        fact = designer.FactDraft
        inspection = designer.ActivityDraft(**{**empty, "type": "inspection", "title": "Revisa la unidad",
            "intro": "Revisa cada llanta y decide.", "scene": "truck", "inspection_prompt": "Revisa las 4 llantas.",
            "verdicts": ["Sigue rodando", "Reencauchar", "Retirar"], "inspect_items": [
                tire(label="Llanta 1", facts=[fact(label="Profundidad", value="12 mm")], look="even", answer=0,
                     explanation="Desgaste parejo y buena profundidad."),
                tire(label="Llanta 2", facts=[fact(label="Profundidad", value="4 mm")], look="shoulders", answer=1,
                     explanation="Hombros gastados: baja presión; el casco aún sirve."),
                tire(label="Llanta 3", facts=[fact(label="Profundidad", value="9 mm")], look="bulge", answer=2,
                     explanation="Un chipote es una ruptura interna: se retira."),
                tire(label="Llanta 4", facts=[fact(label="Profundidad", value="11 mm")], look="even", answer=0,
                     explanation="Está bien: sigue rodando.")]})
        simulator = designer.ActivityDraft(**{**empty, "type": "simulator", "title": "Baja el costo por km",
            "intro": "Mueve los controles.", "simulator_prompt": "Llega a menos de $0.05 por km.",
            "variables": [designer.VariableDraft(key="precio", label="Precio", unit="MXN", min=4000, max=12000,
                                                 step=500, default=6000),
                          designer.VariableDraft(key="km", label="Kilómetros", unit="km", min=60000, max=250000,
                                                 step=10000, default=100000)],
            "formula": "precio / km", "output_label": "Costo por km", "output_unit": "MXN/km", "output_decimals": 3,
            "target_op": "lte", "target_value": 0.05, "success_text": "¡Lo lograste!", "fail_text": "Aún no.",
            "hint": "Más kilómetros bajan el costo."})
        quick = designer.ActivityDraft(**{**empty, "type": "quick", "title": "Revisión en patio",
            "intro": "Contra reloj.", "quick_mode": "order", "quick_prompt": "Ordena la revisión.", "seconds": 45,
            "items": ["Medir presión en frío", "Revisar desgaste", "Leer el DOT", "Recomendar"]})
        return [decision, inspection, simulator, quick]

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
        base = {"subtitle": "", "points": [], "icons": [], "icon": "", "stat_value": "", "stat_label": "", "quote_author": "",
                "visual_kind": "none", "visual_query": "", "visual_prompt": "",
                "chart_labels": [], "chart_values": [], "chart_unit": "",
                "left_heading": "", "left_points": [], "right_heading": "", "right_points": []}
        return designer.SceneDraft(layout=layout, title=title, narration=narration, **{**base, **fields})

    def _module(self, prompt: str) -> designer.ModuleDraft:
        title_match = re.search(r"MÓDULO \d+: (.+)", prompt)
        title = title_match.group(1).strip() if title_match else "Módulo"
        include_quiz = "lista vacía" not in prompt
        scenes = [
            self._scene("cover", title, f"En este módulo vas a aprender {title.lower()}.",
                        subtitle="Lo esencial en pocos minutos", visual_kind="stock", visual_query="truck highway"),
            self._scene("bullets", "Lo más importante", "Empecemos por las ideas clave.",
                        points=["Primera idea clave", "Segunda idea clave", "Tercera idea clave"],
                        icons=["lightbulb", "target", "shield-check"]),
            self._scene("visual", "Cómo se desgasta según la presión", "Mira las tres huellas de la infografía.",
                        visual_kind="infographic",
                        visual_prompt="Infografía: «Presión y desgaste». Tres huellas: baja, correcta y alta presión."),
            self._scene("case", "Caso: una flota de reparto", "Veamos un caso real.",
                        points=["Situación de la flota", "Lo que se encontró", "Lo que se hizo", "Lo que mejoró"]),
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
        self._audio: dict[str, float] = {}  # uploaded narration id -> its duration
        self._durations: dict[str, float] = {}  # video id -> its duration
        self._videos: dict[str, str] = {}  # idempotency key -> video id, like HeyGen

    def looks(self):
        return [
            AvatarLook(id="fake-avatar", name="Presentadora de prueba", preview_image_url="", preview_video_url=""),
            AvatarLook(id="fake-avatar-2", name="Directivo de prueba", preview_image_url="", preview_video_url="", own=True),
            AvatarLook(id="fake-avatar-basic", name="Presentador básico", preview_image_url="", preview_video_url="",
                       engines=["avatar_iii"]),
        ]

    def upload_audio(self, audio):
        asset_id = uuid.uuid4().hex
        self._audio[asset_id] = _duration(audio)
        return asset_id

    def start(self, avatar_id, background, *, idempotency_key, audio_asset_id=None, audio_url=None, engine=None):
        if idempotency_key not in self._videos:
            video_id = uuid.uuid4().hex
            self._durations[video_id] = self._audio.get(audio_asset_id or "", FALLBACK_SECONDS)
            self._videos[idempotency_key] = video_id
        return self._videos[idempotency_key]

    def status(self, video_id):
        return RenderStatus(status="completed", video_url=f"fake://{video_id}")

    def download(self, url, dest):
        duration = self._durations.get(url.removeprefix("fake://"), FALLBACK_SECONDS)
        subprocess.run(
            ["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i",
             f"testsrc2=size=720x720:rate=25:duration={duration:.2f}", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(dest)],
            check=True, capture_output=True,
        )
