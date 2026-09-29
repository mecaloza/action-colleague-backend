"""
Course design with an LLM: outline from a brief + materials, then a storyboard and quiz per module.

The model fills flat, strict schemas (Structured Outputs); this module converts them into the
canonical slide and question formats and drops anything that does not validate, instead of
inventing placeholder content.
"""

from typing import Literal

from pydantic import BaseModel

from app.services.ai.llm import LLM
from app.services.quiz import validate_questions
from app.services.slides.spec import MAX_COLUMN_POINTS, MAX_POINTS, ComparisonColumn, Layout, Slide, visible_points

LANGUAGE_NAMES = {"es": "español latinoamericano", "en": "English", "pt": "português do Brasil"}
WORDS_PER_MINUTE = 150  # narration pace
WORDS_PER_SCENE = 85  # average narration of one scene
MIN_SCENES, MAX_SCENES = 5, 14  # per module, whatever its duration


# ── Schemas the model fills ───────────────────────────────────────────


class OutlineModule(BaseModel):
    title: str
    summary: str
    objectives: list[str]
    key_points: list[str]
    estimated_minutes: int
    include_quiz: bool


class CourseOutline(BaseModel):
    title: str
    description: str
    audience: str
    objectives: list[str]
    modules: list[OutlineModule]


class SceneDraft(BaseModel):
    layout: Layout
    title: str
    subtitle: str
    points: list[str]
    stat_value: str
    stat_label: str
    quote_author: str
    left_heading: str
    left_points: list[str]
    right_heading: str
    right_points: list[str]
    narration: str


class PairDraft(BaseModel):
    left: str
    right: str


class QuestionDraft(BaseModel):
    type: Literal["single_choice", "true_false", "ordering", "matching", "fill_blank"]  # quiz.QUESTION_TYPES
    prompt: str
    scenario: str
    options: list[str]
    correct_index: int
    correct_bool: bool
    items: list[str]
    pairs: list[PairDraft]
    answers: list[str]
    hint: str
    explanation: str


class ModuleDraft(BaseModel):
    scenes: list[SceneDraft]
    reading_summary: str
    quiz: list[QuestionDraft]


class QuizDraft(BaseModel):
    quiz: list[QuestionDraft]


# ── Prompts ───────────────────────────────────────────────────────────

# The offline FakeLLM reads a few markers back from these prompts ("BRIEF DEL ADMINISTRADOR:", "AUDIENCIA:",
# "Exactamente N módulos", "MÓDULO N:", "lista vacía"): keep them when rewording.

_SYSTEM = (
    "Eres un diseñador instruccional senior que crea cursos corporativos breves, prácticos y memorables. "
    "Trabajas solo con la información del brief y de los materiales: si algo no está en ellos, no lo inventes "
    "(ni cifras, ni nombres, ni normas). Escribes en {language}, con frases cortas y concretas."
)


def _materials_block(materials: str, limit: int) -> str:
    text = materials.strip()
    if not text:
        return "No se adjuntaron materiales: basa el contenido en el brief y en buenas prácticas generales."
    if len(text) > limit:
        text = text[:limit] + "\n[… material recortado …]"
    return f"MATERIALES (fuente principal):\n<<<\n{text}\n>>>"


def _feedback_block(feedback: str, subject: str) -> str:
    """What the admin wants changed in the previous PROPUESTA (outline) or VERSIÓN (script); empty without feedback."""
    text = feedback.strip()
    return f"\nCAMBIOS QUE PIDE EL ADMINISTRADOR SOBRE LA {subject} ANTERIOR:\n{text}\n" if text else ""


def _scene_count(minutes: int) -> int:
    """How many scenes a module of this duration needs, given the narration of each."""
    return max(MIN_SCENES, min(MAX_SCENES, round(minutes * WORDS_PER_MINUTE / WORDS_PER_SCENE)))


def outline_prompt(
    brief: str, audience: str, tone: str, target_modules: int | None, minutes: int, materials: str, feedback: str = ""
) -> str:
    feedback_block = _feedback_block(feedback, "PROPUESTA")
    modules_rule = (
        f"Exactamente {target_modules} módulos." if target_modules else "Entre 3 y 6 módulos, según la extensión del material."
    )
    return f"""Propón la estructura de un curso.

BRIEF DEL ADMINISTRADOR:
{brief.strip() or "(sin brief)"}

AUDIENCIA: {audience or "colaboradores de la empresa"}
TONO: {tone or "profesional y cercano"}
DURACIÓN TOTAL APROXIMADA: {minutes} minutos

Reglas:
- {modules_rule} Cada módulo enseña una sola idea central y construye sobre el anterior.
- Título del curso: máximo 70 caracteres. Descripción: 1-2 frases.
- Por módulo: título (máx. 60 caracteres), resumen (1-2 frases), 2-4 objetivos que empiecen con un verbo,
  3-5 puntos clave, minutos estimados (3-15) y si debe tener evaluación (normalmente sí).
- Objetivos del curso: 3-5.
{feedback_block}
{_materials_block(materials, 60_000)}"""


def module_prompt(
    outline: CourseOutline, module: OutlineModule, number: int, tone: str, materials: str, feedback: str = ""
) -> str:
    scenes = _scene_count(module.estimated_minutes)
    module_list = "\n".join(f"{i}. {m.title}" for i, m in enumerate(outline.modules, start=1))
    feedback_block = _feedback_block(feedback, "VERSIÓN")
    return f"""Escribe el guion en escenas del módulo {number} de un curso y su evaluación.

CURSO: {outline.title} — {outline.description}
AUDIENCIA: {outline.audience}
TONO: {tone or "profesional y cercano"}
MÓDULOS DEL CURSO:
{module_list}

MÓDULO {number}: {module.title}
Resumen: {module.summary}
Objetivos: {"; ".join(module.objectives)}
Puntos clave: {"; ".join(module.key_points)}
Duración objetivo: {module.estimated_minutes} minutos (~{WORDS_PER_MINUTE} palabras por minuto de narración)

ESCENAS ({scenes} aprox.). Cada escena es una diapositiva + lo que el presentador dice mientras se ve:
- La primera escena usa layout "cover" (título del módulo + subtítulo con el beneficio para quien aprende).
- La última usa "closing" (título + 3 conclusiones en points).
- En medio alterna layouts según el contenido: "bullets" (2-4 points), "steps" (3-5 pasos en points),
  "statement" (una idea fuerte en title; quote_author opcional), "stat" (solo si el material trae una cifra
  real: stat_value + stat_label), "comparison" (left_heading/left_points vs right_heading/right_points,
  2-4 points cada lado, p. ej. «Correcto» vs «Incorrecto»).
- Texto en pantalla MUY breve: title máx. 60 caracteres, cada point máx. 80 caracteres. La pantalla resume;
  la narración explica.
- narration: 40-110 palabras, natural, en segunda persona, sin leer literalmente la diapositiva, sin
  marcas como [pausa] ni emojis. Debe fluir de una escena a la siguiente.
- Deja vacíos ("" o []) los campos que el layout no usa.

reading_summary: resumen en Markdown para leer (5-10 líneas, con viñetas).

{feedback_block}
EVALUACIÓN: {"4 a 6 preguntas" if module.include_quiz else "lista vacía (este módulo no lleva evaluación)"} que midan los objetivos,
mezclando tipos:
- single_choice: prompt + scenario opcional (caso laboral breve) + 3-4 options + correct_index (desde 0).
- true_false: prompt es una afirmación; correct_bool.
- ordering: items en el ORDEN CORRECTO (3-5); se barajan al mostrarlos.
- matching: 3-4 pairs (left concepto → right definición), sin repetir textos.
- fill_blank: prompt con "_____" y answers (1-3 variantes aceptadas); hint breve.
Cada pregunta con explanation (1-2 frases). Deja vacíos los campos que el tipo no usa.

{_materials_block(materials, 40_000)}"""


def quiz_prompt(title: str, content: str, count: int) -> str:
    return f"""Crea {count} preguntas de evaluación para el módulo «{title}», mezclando tipos
(single_choice, true_false, ordering, matching, fill_blank) con las mismas reglas de siempre:
single_choice con 3-4 options y correct_index; true_false con correct_bool; ordering con items en el orden
correcto; matching con 3-4 pairs; fill_blank con "_____" y answers. Cada una con explanation.
Solo con información del contenido.

CONTENIDO DEL MÓDULO:
<<<
{content[:30_000]}
>>>"""


# ── Conversions ───────────────────────────────────────────────────────


def _column(heading: str, points: list[str]) -> ComparisonColumn:
    return ComparisonColumn(heading=heading.strip(), points=visible_points(points, MAX_COLUMN_POINTS))


def to_slide(scene: SceneDraft) -> Slide:
    return Slide(
        layout=scene.layout,
        title=scene.title.strip(),
        subtitle=scene.subtitle.strip(),
        points=[point.strip() for point in visible_points(scene.points, MAX_POINTS)],
        stat_value=scene.stat_value.strip(),
        stat_label=scene.stat_label.strip(),
        quote_author=scene.quote_author.strip(),
        left=_column(scene.left_heading, scene.left_points),
        right=_column(scene.right_heading, scene.right_points),
    )


def to_scenes(drafts: list[SceneDraft]) -> list[dict]:
    """Storyboard scenes (`{id, slide, narration}`); a scene without narration is dropped."""
    narrated = [scene for scene in drafts if scene.narration.strip()]
    return [
        {"id": f"s{number}", "slide": to_slide(scene).model_dump(), "narration": scene.narration.strip()}
        for number, scene in enumerate(narrated, start=1)
    ]


def to_question(draft: QuestionDraft) -> dict | None:
    """The draft in the canonical question format, or None when it does not validate."""
    base = {"type": draft.type, "prompt": draft.prompt.strip(), "explanation": draft.explanation.strip()}
    if draft.type == "single_choice":
        base |= {"scenario": draft.scenario.strip(), "options": draft.options, "correct_index": draft.correct_index}
    elif draft.type == "true_false":
        base |= {"correct": draft.correct_bool}
    elif draft.type == "ordering":
        base |= {"items": draft.items}
    elif draft.type == "matching":
        base |= {"pairs": [pair.model_dump() for pair in draft.pairs]}
    else:
        base |= {"answers": draft.answers, "hint": draft.hint.strip()}
    try:
        return validate_questions([base])[0].model_dump(mode="json")
    except ValueError:  # InvalidQuestions, or whatever else the question schema rejects
        return None


def to_questions(drafts: list[QuestionDraft]) -> list[dict]:
    """The drafts that validate, numbered q1, q2... in order."""
    questions = [question for draft in drafts if (question := to_question(draft))]
    for number, question in enumerate(questions, start=1):
        question["id"] = f"q{number}"
    return questions


def _system(language: str) -> str:
    return _SYSTEM.format(language=LANGUAGE_NAMES.get(language, language))


def generate_outline(
    llm: LLM, *, brief: str, language: str, audience: str, tone: str, target_modules: int | None,
    minutes: int, materials: str, feedback: str = "",
) -> CourseOutline:
    return llm.structured(
        _system(language),
        outline_prompt(brief, audience, tone, target_modules, minutes, materials, feedback),
        CourseOutline,
        max_tokens=4000,
    )


def generate_module(
    llm: LLM, outline: CourseOutline, module: OutlineModule, number: int, *, language: str, tone: str,
    materials: str, feedback: str = "",
) -> ModuleDraft:
    return llm.structured(
        _system(language),
        module_prompt(outline, module, number, tone, materials, feedback),
        ModuleDraft,
        max_tokens=12000,
    )


def generate_quiz(llm: LLM, *, title: str, content: str, count: int, language: str) -> list[dict]:
    draft = llm.structured(_system(language), quiz_prompt(title, content, count), QuizDraft, max_tokens=4000)
    return to_questions(draft.quiz)
