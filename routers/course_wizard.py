"""
Course Creation Wizard — AI-powered course generation.

Flow:
1. Upload materials (PDFs, images) + natural language description
2. AI analyzes and proposes breakdown (sections, modules, evaluations)
3. Admin reviews/edits breakdown
4. Choose: manual script OR AI-generated script
5. Choose voice: catalog OR clone from audio
6. Generate final content (video + audio via HeyGen + ElevenLabs)
"""

import json
import os
import uuid
import base64
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from auth import get_current_user, require_admin
from database import get_db
from models import Course, Evaluation, Module, User

router = APIRouter(prefix="/courses/ai", tags=["course-wizard"])

def _get_openai_key() -> str:
    return os.getenv("OPENAI_API_KEY", "")

def _get_elevenlabs_key() -> str:
    return os.getenv("ELEVENLABS_API_KEY", "")

def _get_heygen_key() -> str:
    return os.getenv("HEYGEN_API_KEY", "")
UPLOAD_DIR = os.getenv("UPLOAD_DIR", "/tmp/colleague-uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)


# ── Schemas ───────────────────────────────────────────────────────────

class ModuleProposal(BaseModel):
    title: str
    description: str
    duration_minutes: int = 10
    content_points: List[str] = []
    has_evaluation: bool = True
    evaluation_questions: int = 3


class CourseBreakdown(BaseModel):
    title: str
    description: str
    target_audience: str = ""
    total_duration_minutes: int = 0
    modules: List[ModuleProposal] = []


class AnalyzeResponse(BaseModel):
    session_id: str
    breakdown: CourseBreakdown
    source_summary: str = ""
    confidence: float = 0.85


class ScriptRequest(BaseModel):
    session_id: str
    breakdown: CourseBreakdown
    tone: str = "professional"  # professional, casual, academic
    language: str = "es"


class ModuleScript(BaseModel):
    module_index: int
    title: str
    script: str
    duration_estimate_seconds: int = 0
    evaluation: Optional[dict] = None


class ScriptResponse(BaseModel):
    session_id: str
    scripts: List[ModuleScript]


class VoiceOption(BaseModel):
    id: str
    name: str
    language: str
    gender: str
    preview_url: str = ""
    provider: str = "elevenlabs"


class VoicesResponse(BaseModel):
    voices: List[VoiceOption]


class CloneVoiceRequest(BaseModel):
    name: str
    description: str = ""


class CloneVoiceResponse(BaseModel):
    voice_id: str
    name: str
    status: str


class GenerateRequest(BaseModel):
    session_id: str
    course_title: str
    scripts: List[ModuleScript]
    voice_id: str
    generate_video: bool = True
    avatar_id: str = ""


class GenerateResponse(BaseModel):
    course_id: int
    status: str
    modules_created: int
    message: str


# ── Helpers ───────────────────────────────────────────────────────────

def _call_openai(system_prompt: str, user_prompt: str, json_mode: bool = True) -> str:
    """Call OpenAI API. Returns the text response."""
    if not _get_openai_key():
        return ""

    import httpx
    headers = {
        "Authorization": f"Bearer {_get_openai_key()}",
        "Content-Type": "application/json",
    }
    body: dict = {
        "model": "gpt-4o",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.7,
        "max_tokens": 4000,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}

    try:
        r = httpx.post("https://api.openai.com/v1/chat/completions", json=body, headers=headers, timeout=60)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"OpenAI error: {e}")
        return ""


def _extract_text_from_pdf(file_path: str) -> str:
    """Extract text from PDF. Falls back to empty string."""
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(file_path)
        text = ""
        for page in doc:
            text += page.get_text()
        doc.close()
        return text[:15000]  # Limit
    except ImportError:
        # Fallback: try pdfplumber
        try:
            import pdfplumber
            with pdfplumber.open(file_path) as pdf:
                text = ""
                for page in pdf.pages:
                    text += (page.extract_text() or "") + "\n"
            return text[:15000]
        except ImportError:
            return "[PDF text extraction not available - install PyMuPDF or pdfplumber]"


def _auto_generate_evaluation(module_title: str, content_text: str) -> list:
    """Auto-generate 5 mixed evaluation questions from module content using GPT."""
    system_prompt = (
        "Eres un diseñador instruccional experto. Generas preguntas de evaluación variadas "
        "basadas en el contenido de un módulo de capacitación. Responde SOLO JSON válido."
    )
    user_prompt = (
        f"Basándote en el siguiente contenido del módulo '{module_title}', genera exactamente 5 preguntas de evaluación "
        "usando estos tipos (una de cada tipo, en este orden):\n\n"
        "1. scenario: situación laboral con opciones\n"
        '   {{"type": "scenario", "scenario": "Descripción...", "question": "¿Qué harías?", "options": ["A", "B", "C", "D"], "correct": 0, "explanation": "Por qué..."}}\n\n'
        "2. ordering: ordenar pasos\n"
        '   {{"type": "ordering", "question": "Ordena estos pasos:", "items": ["Paso B", "Paso A", "Paso D", "Paso C"], "correct_order": [1, 0, 3, 2]}}\n\n'
        "3. matching: emparejar conceptos\n"
        '   {{"type": "matching", "question": "Empareja:", "pairs": [{{"left": "A", "right": "Def A"}}, {{"left": "B", "right": "Def B"}}, {{"left": "C", "right": "Def C"}}]}}\n\n'
        "4. fill_blank: completar frase\n"
        '   {{"type": "fill_blank", "question": "La _____ es clave.", "answer": "comunicación", "hint": "Empieza con c"}}\n\n'
        "5. true_false: verdadero o falso\n"
        '   {{"type": "true_false", "statement": "Afirmación...", "correct": false, "explanation": "Porque..."}}\n\n'
        f"Contenido del módulo:\n{content_text[:3000]}\n\n"
        'Responde en JSON: {{"questions": [...]}}'
    )

    ai_response = _call_openai(system_prompt, user_prompt)
    if ai_response:
        try:
            data = json.loads(ai_response)
            questions = data.get("questions", [])
            if isinstance(questions, list) and len(questions) > 0:
                return questions
        except (json.JSONDecodeError, Exception):
            pass
    return []


# ── Endpoints ─────────────────────────────────────────────────────────

@router.post("/analyze", response_model=AnalyzeResponse)
async def analyze_materials(
    description: str = Form(""),
    files: List[UploadFile] = File(default=[]),
    _admin: User = Depends(require_admin),
):
    """
    Step 1: Upload materials + description → AI proposes course breakdown.
    """
    session_id = str(uuid.uuid4())[:8]
    extracted_texts = []

    # Save and extract text from uploaded files
    for f in files:
        ext = (f.filename or "").rsplit(".", 1)[-1].lower()
        file_path = os.path.join(UPLOAD_DIR, f"{session_id}_{f.filename}")
        content = await f.read()
        with open(file_path, "wb") as out:
            out.write(content)

        if ext == "pdf":
            text = _extract_text_from_pdf(file_path)
            if text:
                extracted_texts.append(f"[PDF: {f.filename}]\n{text}")
        elif ext in ("png", "jpg", "jpeg", "webp"):
            extracted_texts.append(f"[Image: {f.filename} - uploaded for visual reference]")
        else:
            # Try reading as text
            try:
                extracted_texts.append(f"[File: {f.filename}]\n{content.decode('utf-8', errors='ignore')[:5000]}")
            except Exception:
                extracted_texts.append(f"[File: {f.filename} - binary content]")

    source_text = "\n\n".join(extracted_texts)
    source_summary = f"Analyzed {len(files)} file(s): {', '.join(f.filename or '?' for f in files)}" if files else "No files uploaded"

    # Build AI prompt
    user_prompt = f"""Descripción del curso que quiere el administrador:
{description or 'No se proporcionó descripción'}

Material fuente extraído:
{source_text or 'No se subieron archivos'}

Genera una propuesta de estructura de curso con módulos, cada uno con:
- Título claro
- Descripción de qué se enseña
- Duración estimada en minutos
- Puntos clave del contenido (3-5 bullets)
- Si necesita evaluación (true/false)
- Número de preguntas de evaluación

Responde SOLO en JSON con esta estructura:
{{
  "title": "Título del curso",
  "description": "Descripción general",
  "target_audience": "A quién va dirigido",
  "total_duration_minutes": 60,
  "modules": [
    {{
      "title": "Módulo 1: ...",
      "description": "...",
      "duration_minutes": 15,
      "content_points": ["punto 1", "punto 2", "punto 3"],
      "has_evaluation": true,
      "evaluation_questions": 3
    }}
  ]
}}"""

    system_prompt = "Eres un diseñador instruccional experto. Creas estructuras de cursos de capacitación empresarial efectivos, prácticos y bien organizados. Responde siempre en español. Responde SOLO JSON válido."

    ai_response = _call_openai(system_prompt, user_prompt)

    if ai_response:
        try:
            data = json.loads(ai_response)
            breakdown = CourseBreakdown(**data)
        except (json.JSONDecodeError, Exception):
            breakdown = _fallback_breakdown(description)
    else:
        breakdown = _fallback_breakdown(description)

    return AnalyzeResponse(
        session_id=session_id,
        breakdown=breakdown,
        source_summary=source_summary,
        confidence=0.85 if ai_response else 0.5,
    )


def _fallback_breakdown(description: str) -> CourseBreakdown:
    """Generate a basic breakdown when AI is unavailable."""
    title = description[:80] if description else "Nuevo Curso"
    return CourseBreakdown(
        title=title,
        description=description or "Curso generado automáticamente",
        target_audience="Colaboradores de la empresa",
        total_duration_minutes=45,
        modules=[
            ModuleProposal(
                title="Módulo 1: Introducción",
                description="Introducción al tema principal del curso",
                duration_minutes=10,
                content_points=["Objetivos del curso", "Contexto general", "Qué aprenderás"],
                has_evaluation=False,
            ),
            ModuleProposal(
                title="Módulo 2: Contenido Principal",
                description="Desarrollo del tema central",
                duration_minutes=20,
                content_points=["Concepto clave 1", "Concepto clave 2", "Aplicación práctica"],
                has_evaluation=True,
                evaluation_questions=3,
            ),
            ModuleProposal(
                title="Módulo 3: Cierre y Evaluación",
                description="Resumen y evaluación final",
                duration_minutes=15,
                content_points=["Resumen de lo aprendido", "Mejores prácticas", "Próximos pasos"],
                has_evaluation=True,
                evaluation_questions=5,
            ),
        ],
    )


@router.post("/generate-scripts", response_model=ScriptResponse)
async def generate_scripts(
    payload: ScriptRequest,
    _admin: User = Depends(require_admin),
):
    """
    Step 3: Generate narration scripts for each module.
    """
    scripts: List[ModuleScript] = []

    for i, module in enumerate(payload.breakdown.modules):
        eval_instruction = ""
        eval_json = '"evaluation": null'
        if module.has_evaluation:
            eval_instruction = (
                "También genera 5 preguntas de evaluación VARIADAS usando estos tipos (mezcla obligatoria, NO repitas el mismo tipo):\n\n"
                "1. tipo 'scenario': situación laboral con opciones\n"
                '   {"type": "scenario", "scenario": "Descripción de situación...", "question": "¿Qué harías?", "options": ["A", "B", "C", "D"], "correct": 0, "explanation": "Por qué..."}\n\n'
                "2. tipo 'ordering': ordenar pasos correctamente\n"
                '   {"type": "ordering", "question": "Ordena estos pasos:", "items": ["Paso B", "Paso A", "Paso D", "Paso C"], "correct_order": [1, 0, 3, 2]}\n\n'
                "3. tipo 'matching': emparejar conceptos con definiciones\n"
                '   {"type": "matching", "question": "Empareja cada concepto:", "pairs": [{"left": "Concepto A", "right": "Definición A"}, {"left": "Concepto B", "right": "Definición B"}, {"left": "Concepto C", "right": "Definición C"}]}\n\n'
                "4. tipo 'fill_blank': completar la frase\n"
                '   {"type": "fill_blank", "question": "La _____ es fundamental para...", "answer": "comunicación", "hint": "Empieza con c"}\n\n'
                "5. tipo 'true_false': verdadero o falso con explicación\n"
                '   {"type": "true_false", "statement": "Afirmación...", "correct": false, "explanation": "Porque..."}\n\n'
                "IMPORTANTE: Genera exactamente 5 preguntas, una de cada tipo, en el orden listado. "
                "Las preguntas deben ser relevantes al contenido del módulo y desafiantes pero justas."
            )
            eval_json = '"evaluation": {"questions": [...]}'

        points_str = ", ".join(module.content_points)
        user_prompt = (
            f"Genera un script de narración para un video de capacitación empresarial.\n\n"
            f"Curso: {payload.breakdown.title}\n"
            f"Módulo: {module.title}\n"
            f"Descripción: {module.description}\n"
            f"Puntos a cubrir: {points_str}\n"
            f"Duración objetivo: {module.duration_minutes} minutos\n"
            f"Tono: {payload.tone}\n"
            f"Idioma: {payload.language}\n\n"
            f"El script debe:\n"
            f"- Ser natural y conversacional (como si un instructor hablara)\n"
            f"- Cubrir todos los puntos clave\n"
            f"- Tener una intro, desarrollo y cierre\n"
            f"- Calcular ~150 palabras por minuto de narración\n\n"
            f"{eval_instruction}\n\n"
            f'Responde en JSON:\n{{"script": "texto completo del script...", '
            f'"duration_estimate_seconds": 600, {eval_json}}}'
        )

        system_prompt = "Eres un guionista experto en contenido educativo corporativo. Creas scripts naturales, claros y atractivos para videos de capacitación. Responde SOLO JSON válido."

        ai_response = _call_openai(system_prompt, user_prompt)

        if ai_response:
            try:
                data = json.loads(ai_response)
                scripts.append(ModuleScript(
                    module_index=i,
                    title=module.title,
                    script=data.get("script", f"Script para {module.title}"),
                    duration_estimate_seconds=data.get("duration_estimate_seconds", module.duration_minutes * 60),
                    evaluation=data.get("evaluation"),
                ))
                continue
            except (json.JSONDecodeError, Exception):
                pass

        # Fallback
        fallback_questions = None
        if module.has_evaluation:
            fallback_questions = {"questions": [
                {"type": "scenario", "scenario": f"Estás aplicando los conceptos de {module.title} en tu trabajo diario.", "question": f"¿Cuál es la mejor práctica para {module.title}?", "options": ["Opción A", "Opción B", "Opción C", "Opción D"], "correct": 0, "explanation": "Esta es la respuesta correcta porque aplica los principios fundamentales."},
                {"type": "ordering", "question": f"Ordena los pasos para implementar {module.title}:", "items": ["Paso 2", "Paso 1", "Paso 4", "Paso 3"], "correct_order": [1, 0, 3, 2]},
                {"type": "matching", "question": f"Empareja los conceptos de {module.title} con sus definiciones:", "pairs": [{"left": "Concepto A", "right": "Definición A"}, {"left": "Concepto B", "right": "Definición B"}, {"left": "Concepto C", "right": "Definición C"}]},
                {"type": "fill_blank", "question": f"En {module.title}, el principio fundamental es la _____.", "answer": "práctica", "hint": "Empieza con 'p'"},
                {"type": "true_false", "statement": f"En {module.title}, siempre se debe actuar sin consultar al equipo.", "correct": False, "explanation": "Es importante consultar al equipo antes de tomar decisiones importantes."},
            ]}
        scripts.append(ModuleScript(
            module_index=i,
            title=module.title,
            script=f"Bienvenidos al módulo: {module.title}.\n\n{module.description}\n\nEn este módulo cubriremos:\n" + "\n".join(f"- {p}" for p in module.content_points) + "\n\n[Script completo pendiente de generación con IA]",
            duration_estimate_seconds=module.duration_minutes * 60,
            evaluation=fallback_questions,
        ))

    return ScriptResponse(session_id=payload.session_id, scripts=scripts)


@router.get("/voices", response_model=VoicesResponse)
async def list_voices(_admin: User = Depends(require_admin)):
    """
    List available voices from ElevenLabs (or catalog).
    """
    if _get_elevenlabs_key():
        try:
            import httpx
            r = httpx.get(
                "https://api.elevenlabs.io/v1/voices",
                headers={"xi-api-key": _get_elevenlabs_key()},
                timeout=15,
            )
            r.raise_for_status()
            voices = []
            for v in r.json().get("voices", [])[:20]:
                voices.append(VoiceOption(
                    id=v["voice_id"],
                    name=v["name"],
                    language=v.get("labels", {}).get("language", "es"),
                    gender=v.get("labels", {}).get("gender", "unknown"),
                    preview_url=v.get("preview_url", ""),
                    provider="elevenlabs",
                ))
            return VoicesResponse(voices=voices)
        except Exception:
            pass

    # Default catalog
    return VoicesResponse(voices=[
        VoiceOption(id="voice_mateo", name="Mateo", language="es", gender="male", provider="catalog"),
        VoiceOption(id="voice_sofia", name="Sofía", language="es", gender="female", provider="catalog"),
        VoiceOption(id="voice_carlos", name="Carlos", language="es", gender="male", provider="catalog"),
        VoiceOption(id="voice_valentina", name="Valentina", language="es", gender="female", provider="catalog"),
        VoiceOption(id="voice_diego", name="Diego", language="es", gender="male", provider="catalog"),
        VoiceOption(id="voice_isabella", name="Isabella", language="es", gender="female", provider="catalog"),
        VoiceOption(id="voice_james", name="James", language="en", gender="male", provider="catalog"),
        VoiceOption(id="voice_emma", name="Emma", language="en", gender="female", provider="catalog"),
    ])


@router.post("/clone-voice", response_model=CloneVoiceResponse)
async def clone_voice(
    name: str = Form(...),
    description: str = Form(""),
    audio_file: UploadFile = File(...),
    _admin: User = Depends(require_admin),
):
    """
    Upload audio sample to clone a voice via ElevenLabs.
    """
    # Save audio file
    file_path = os.path.join(UPLOAD_DIR, f"voice_{uuid.uuid4()[:8]}_{audio_file.filename}")
    content = await audio_file.read()
    with open(file_path, "wb") as out:
        out.write(content)

    if _get_elevenlabs_key():
        try:
            import httpx
            with open(file_path, "rb") as f:
                r = httpx.post(
                    "https://api.elevenlabs.io/v1/voices/add",
                    headers={"xi-api-key": _get_elevenlabs_key()},
                    data={"name": name, "description": description},
                    files={"files": (audio_file.filename, f, audio_file.content_type or "audio/mpeg")},
                    timeout=30,
                )
                r.raise_for_status()
                voice_id = r.json().get("voice_id", "")
                return CloneVoiceResponse(voice_id=voice_id, name=name, status="ready")
        except Exception as e:
            print(f"ElevenLabs clone error: {e}")

    # Mock response
    mock_id = f"cloned_{uuid.uuid4().hex[:8]}"
    return CloneVoiceResponse(voice_id=mock_id, name=name, status="ready")


@router.post("/generate-content", response_model=GenerateResponse)
async def generate_content(
    payload: GenerateRequest,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """
    Step 5: Create the course with all modules, scripts, and trigger
    video/audio generation.
    """
    # Create course in DB
    course = Course(
        title=payload.course_title,
        description=f"Curso generado con IA. Voz: {payload.voice_id}",
        status="draft",
        created_by=admin.id,
    )
    db.add(course)
    db.flush()

    for script in payload.scripts:
        module = Module(
            course_id=course.id,
            title=script.title,
            order=script.module_index + 1,
            content_text=script.script,
        )
        db.add(module)
        db.flush()

        # Create evaluation if present in script, or auto-generate from content
        if script.evaluation and script.evaluation.get("questions"):
            ev = Evaluation(
                module_id=module.id,
                questions_json=json.dumps(script.evaluation["questions"]),
            )
            db.add(ev)
        elif script.script and script.script.strip():
            # Auto-generate evaluation from module content
            auto_questions = _auto_generate_evaluation(script.title, script.script)
            if auto_questions:
                ev = Evaluation(
                    module_id=module.id,
                    questions_json=json.dumps(auto_questions),
                )
                db.add(ev)

        # TODO: Trigger async video/audio generation
        # - ElevenLabs: text-to-speech with voice_id
        # - HeyGen: generate video with avatar + audio
        # For now, mark as pending
        # module.video_url = f"pending://generation/{payload.session_id}/{script.module_index}"

    db.commit()

    return GenerateResponse(
        course_id=course.id,
        status="created",
        modules_created=len(payload.scripts),
        message=f"Curso '{payload.course_title}' creado con {len(payload.scripts)} módulos. Generación de contenido multimedia en cola.",
    )


# ── Audio Generation (ElevenLabs TTS) ────────────────────────────────

class GenerateAudioRequest(BaseModel):
    course_id: int
    voice_id: str


class GenerateAudioResponse(BaseModel):
    course_id: int
    modules_processed: int
    status: str
    details: List[dict] = []


def _bg_generate_audio(course_id: int, voice_id: str):
    """Background task: generate audio for all modules."""
    import httpx
    from database import SessionLocal

    db = SessionLocal()
    try:
        modules = db.query(Module).filter(Module.course_id == course_id).order_by(Module.order).all()
        el_key = _get_elevenlabs_key()
        if not el_key or not modules:
            return

        for mod in modules:
            if not mod.content_text or not mod.content_text.strip():
                continue
            try:
                mod.generation_status = "generating"
                db.commit()
                r = httpx.post(
                    f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
                    headers={"xi-api-key": el_key, "Content-Type": "application/json"},
                    json={"text": mod.content_text[:5000], "model_id": "eleven_multilingual_v2", "voice_settings": {"stability": 0.5, "similarity_boost": 0.75}},
                    timeout=120,
                )
                r.raise_for_status()
                audio_filename = f"course_{course_id}_module_{mod.id}.mp3"
                audio_path = os.path.join(UPLOAD_DIR, audio_filename)
                with open(audio_path, "wb") as f:
                    f.write(r.content)
                mod.audio_url = f"/uploads/{audio_filename}"
                mod.generation_status = "completed"
                db.commit()
            except Exception as e:
                mod.generation_status = "failed"
                db.commit()
                print(f"[audio] Error module {mod.id}: {e}")
    finally:
        db.close()


@router.post("/generate-audio", response_model=GenerateAudioResponse)
async def generate_audio(
    payload: GenerateAudioRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    """
    Queue audio generation for all modules (returns immediately).
    """
    modules = db.query(Module).filter(Module.course_id == payload.course_id).order_by(Module.order).all()
    if not modules:
        raise HTTPException(status_code=404, detail="No modules found for this course")

    if not _get_elevenlabs_key():
        raise HTTPException(status_code=500, detail="ElevenLabs API key not configured")

    count = 0
    for mod in modules:
        if mod.content_text and mod.content_text.strip():
            mod.generation_status = "queued"
            count += 1
    db.commit()

    background_tasks.add_task(_bg_generate_audio, payload.course_id, payload.voice_id)

    return GenerateAudioResponse(
        course_id=payload.course_id,
        modules_processed=count,
        status="queued",
        details=[{"message": "Audio generation started in background"}],
    )


# ── Video Generation (HeyGen) ────────────────────────────────────────

class GenerateVideoRequest(BaseModel):
    course_id: int
    avatar_id: str = ""  # HeyGen avatar ID


class GenerateVideoResponse(BaseModel):
    course_id: int
    modules_processed: int
    status: str
    details: List[dict] = []


@router.post("/generate-video", response_model=GenerateVideoResponse)
async def generate_video(
    payload: GenerateVideoRequest,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    """
    Generate video with avatar for all modules using HeyGen API.
    """
    modules = db.query(Module).filter(Module.course_id == payload.course_id).order_by(Module.order).all()
    if not modules:
        raise HTTPException(status_code=404, detail="No modules found for this course")

    details = []
    for mod in modules:
        if not mod.content_text or mod.content_text.strip() == "":
            details.append({"module_id": mod.id, "title": mod.title, "status": "skipped"})
            continue

        if _get_heygen_key():
            try:
                import httpx
                # Use the audio if available, otherwise text-based
                script_input: dict = {}
                if mod.audio_url and os.path.exists(os.path.join(UPLOAD_DIR, mod.audio_url.replace("/uploads/", ""))):
                    script_input = {
                        "type": "audio",
                        "audio_url": mod.audio_url,
                    }
                else:
                    script_input = {
                        "type": "text",
                        "input": mod.content_text[:2000],
                        "voice_id": "",  # Use default
                    }

                # Create video via HeyGen API v2
                r = httpx.post(
                    "https://api.heygen.com/v2/video/generate",
                    headers={
                        "X-Api-Key": _get_heygen_key(),
                        "Content-Type": "application/json",
                    },
                    json={
                        "video_inputs": [{
                            "character": {
                                "type": "avatar",
                                "avatar_id": payload.avatar_id or "Daisy-inskirt-20220818",
                                "avatar_style": "normal",
                            },
                            "voice": {
                                "type": "text",
                                "input_text": mod.content_text[:1500],
                                "voice_id": "0f3be0bc527846d78b5e88b04c439d5f",
                            },
                            "background": {
                                "type": "color",
                                "value": "#1a1a2e",
                            },
                        }],
                        "dimension": {"width": 1920, "height": 1080},
                    },
                    timeout=30,
                )
                r.raise_for_status()
                result = r.json()
                video_id = result.get("data", {}).get("video_id", "")

                if video_id:
                    mod.video_url = f"heygen://pending/{video_id}"
                    mod.generation_status = "generating"
                    db.commit()
                    details.append({
                        "module_id": mod.id,
                        "title": mod.title,
                        "status": "queued",
                        "video_id": video_id,
                    })
                else:
                    details.append({
                        "module_id": mod.id,
                        "title": mod.title,
                        "status": "failed",
                        "error": "No video_id returned",
                    })
                continue
            except Exception as e:
                details.append({
                    "module_id": mod.id,
                    "title": mod.title,
                    "status": "failed",
                    "error": str(e)[:200],
                })
                continue

        details.append({
            "module_id": mod.id,
            "title": mod.title,
            "status": "no_api_key",
        })

    queued = sum(1 for d in details if d["status"] == "queued")
    return GenerateVideoResponse(
        course_id=payload.course_id,
        modules_processed=len(details),
        status="queued" if queued > 0 else "failed",
        details=details,
    )


# ── Serve uploaded files ──────────────────────────────────────────────
from fastapi.responses import FileResponse

@router.get("/uploads/{filename}")
async def serve_upload(filename: str):
    """Serve uploaded/generated files."""
    file_path = os.path.join(UPLOAD_DIR, filename)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(file_path, media_type="audio/mpeg")


@router.get("/video-status/{module_id}")
async def check_video_status(
    module_id: int,
    db: Session = Depends(get_db),
):
    """Check HeyGen video generation status and update module when ready."""
    import httpx

    mod = db.query(Module).filter(Module.id == module_id).first()
    if not mod:
        raise HTTPException(status_code=404, detail="Module not found")

    if not mod.video_url or not mod.video_url.startswith("heygen://pending/"):
        return {
            "module_id": mod.id,
            "status": mod.generation_status or "unknown",
            "video_url": mod.video_url,
        }

    video_id = mod.video_url.replace("heygen://pending/", "")

    if not _get_heygen_key():
        raise HTTPException(status_code=500, detail="HeyGen API key not configured")

    try:
        r = httpx.get(
            f"https://api.heygen.com/v1/video_status.get?video_id={video_id}",
            headers={"X-Api-Key": _get_heygen_key()},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json().get("data", {})
        status = data.get("status", "unknown")

        if status == "completed":
            video_url = data.get("video_url", "")
            mod.video_url = video_url
            mod.generation_status = "completed"
            db.commit()
            return {
                "module_id": mod.id,
                "status": "completed",
                "video_url": video_url,
            }
        elif status == "failed":
            error_msg = data.get("error", {}).get("message", "Unknown error")
            mod.generation_status = "failed"
            mod.video_url = ""
            db.commit()
            return {
                "module_id": mod.id,
                "status": "failed",
                "error": error_msg,
            }
        else:
            return {
                "module_id": mod.id,
                "status": status,
                "video_id": video_id,
            }
    except Exception as e:
        return {
            "module_id": mod.id,
            "status": "error",
            "error": str(e),
        }


@router.post("/check-all-videos/{course_id}")
async def check_all_videos(
    course_id: int,
    db: Session = Depends(get_db),
):
    """Check status of all pending videos for a course."""
    import httpx

    modules = db.query(Module).filter(
        Module.course_id == course_id,
        Module.video_url.like("heygen://pending/%"),
    ).all()

    if not modules:
        return {"message": "No pending videos", "results": []}

    results = []
    for mod in modules:
        video_id = mod.video_url.replace("heygen://pending/", "")
        try:
            r = httpx.get(
                f"https://api.heygen.com/v1/video_status.get?video_id={video_id}",
                headers={"X-Api-Key": _get_heygen_key()},
                timeout=15,
            )
            r.raise_for_status()
            data = r.json().get("data", {})
            status = data.get("status", "unknown")

            if status == "completed":
                video_url = data.get("video_url", "")
                mod.video_url = video_url
                mod.generation_status = "completed"
                results.append({"module_id": mod.id, "status": "completed", "video_url": video_url})
            elif status == "failed":
                mod.generation_status = "failed"
                mod.video_url = ""
                results.append({"module_id": mod.id, "status": "failed"})
            else:
                results.append({"module_id": mod.id, "status": status, "video_id": video_id})
        except Exception as e:
            results.append({"module_id": mod.id, "status": "error", "error": str(e)})

    db.commit()
    return {"results": results}


# ── Multi-Scene Video Generation ──────────────────────────────────────────────

async def _split_into_scenes(content: str) -> list:
    """Use GPT to split module content into alternating scenes."""
    import httpx
    prompt = f"""Divide the following educational content into 4-6 scenes for a video.
Each scene should alternate between:
- "avatar": The instructor (avatar) explains a concept (provide the narration text, max 200 words)
- "infographic": A visual/infographic slide with key data (provide a DALL-E prompt for the image and a short narration text, max 100 words)

Start with an avatar intro and end with an avatar summary.
Return ONLY a JSON array, no other text:
[
  {{"type": "avatar", "narration": "..."}},
  {{"type": "infographic", "narration": "...", "image_prompt": "Professional infographic about ..., clean design, violet and white theme, modern, educational"}},
  ...
]

Content:
{content[:3000]}"""

    r = httpx.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {_get_openai_key()}", "Content-Type": "application/json"},
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.7,
            "response_format": {"type": "json_object"},
        },
        timeout=60,
    )
    r.raise_for_status()
    import json
    text = r.json()["choices"][0]["message"]["content"]
    data = json.loads(text)
    # Handle both {"scenes": [...]} and direct [...]
    if isinstance(data, dict):
        return data.get("scenes", data.get("data", []))
    return data


async def _generate_infographic(prompt: str) -> str:
    """Generate an infographic image with DALL-E and return the URL."""
    import httpx
    r = httpx.post(
        "https://api.openai.com/v1/images/generations",
        headers={"Authorization": f"Bearer {_get_openai_key()}", "Content-Type": "application/json"},
        json={
            "model": "dall-e-3",
            "prompt": prompt,
            "n": 1,
            "size": "1792x1024",  # Landscape for video
            "quality": "standard",
        },
        timeout=60,
    )
    r.raise_for_status()
    return r.json()["data"][0]["url"]


def _bg_generate_video_v2(course_id: int, avatar_id: str | None):
    """Background task: generate multi-scene videos for all modules."""
    import httpx
    import logging
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger("video-v2")
    logger.setLevel(logging.INFO)
    from database import SessionLocal

    db = SessionLocal()
    try:
        modules = db.query(Module).filter(Module.course_id == course_id).order_by(Module.order).all()
        heygen_key = _get_heygen_key()
        openai_key = _get_openai_key()
        logger.info(f"[video-v2] Starting for course {course_id}: {len(modules)} modules, heygen={'yes' if heygen_key else 'NO'}, openai={'yes' if openai_key else 'NO'}")
        if not heygen_key or not openai_key or not modules:
            logger.error(f"[video-v2] Missing keys or modules, aborting")
            return

        for mod in modules:
            if not mod.content_text or not mod.content_text.strip():
                continue
            try:
                mod.generation_status = "generating"
                db.commit()

                # Step 1: Split into scenes via GPT (sync httpx)
                prompt = f"""Divide the following educational content into 4-6 scenes for a training video.
Each scene should alternate between:
- "avatar": The instructor explains a concept (narration text, max 200 words). Used for introductions, transitions, and conclusions.
- "infographic": A visual slide with KEY POINTS displayed as readable text. Provide:
  - "narration": what the avatar says while the slide shows (max 100 words)
  - "slide_title": a short title for the slide (3-8 words, in Spanish)
  - "slide_points": array of exactly 3 key points (each 3-10 words MAX, short and punchy, in Spanish) that summarize the concept
  - "slide_subtitle": optional subtitle or category

The slide will be generated programmatically with clean, readable text — NOT with AI image generation.
Focus on extracting the most important facts, steps, or principles from the content.

Start with an avatar intro and end with an avatar summary.
Return ONLY valid JSON:
{{"scenes": [
  {{"type":"avatar","narration":"..."}},
  {{"type":"infographic","narration":"...","slide_title":"...","slide_points":["punto 1","punto 2","punto 3"],"slide_subtitle":"..."}}
]}}

Content:
{mod.content_text[:3000]}"""

                import json as _json
                sr = httpx.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {openai_key}", "Content-Type": "application/json"},
                    json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": prompt}], "temperature": 0.7, "response_format": {"type": "json_object"}},
                    timeout=60,
                )
                sr.raise_for_status()
                scene_data = _json.loads(sr.json()["choices"][0]["message"]["content"])
                scenes = scene_data.get("scenes", scene_data) if isinstance(scene_data, dict) else scene_data
                logger.info(f"[video-v2] Module {mod.id}: {len(scenes) if scenes else 0} scenes from GPT")
                if not scenes:
                    continue

                # Step 2: Build video_inputs
                from utils.infographic import generate_infographic, upload_to_supabase
                import os, uuid
                sb_url = os.getenv("SUPABASE_URL", "")
                sb_key = os.getenv("SUPABASE_SERVICE_KEY", os.getenv("SUPABASE_KEY", ""))

                video_inputs = []
                scene_idx = 0
                for scene in scenes:
                    narration = scene.get("narration", "")[:500]
                    has_slide = scene.get("type") == "infographic" and scene.get("slide_title")
                    # Fallback: also support old image_prompt format
                    has_image_prompt = scene.get("type") == "infographic" and scene.get("image_prompt") and not has_slide
                    is_infographic = has_slide or has_image_prompt
                    _aid = avatar_id or "Daisy-inskirt-20220818"
                    base_voice = {"type": "text", "input_text": narration, "voice_id": "0f3be0bc527846d78b5e88b04c439d5f"}

                    if is_infographic:
                        # Avatar small in bottom-right corner so infographic is fully visible
                        base_char = {
                            "type": "avatar", "avatar_id": _aid, "avatar_style": "normal",
                            "scale": 0.22, "offset": {"x": 0.42, "y": 0.28}
                        }

                        img_url = None
                        if has_slide:
                            # Generate infographic with Pillow (readable text)
                            try:
                                img_bytes = generate_infographic(
                                    title=scene.get("slide_title", ""),
                                    points=scene.get("slide_points", []),
                                    subtitle=scene.get("slide_subtitle", ""),
                                )
                                fname = f"m{mod.id}_s{scene_idx}_{uuid.uuid4().hex[:8]}.png"
                                img_url = upload_to_supabase(img_bytes, fname, sb_url, sb_key)
                                if img_url:
                                    logger.info(f"[video-v2] Module {mod.id}: Infographic generated & uploaded OK")
                                else:
                                    logger.warning(f"[video-v2] Module {mod.id}: Supabase upload failed")
                            except Exception as gen_err:
                                logger.warning(f"[video-v2] Module {mod.id}: Infographic gen failed: {gen_err}")

                        elif has_image_prompt:
                            # Fallback to DALL-E for old format
                            try:
                                ir = httpx.post(
                                    "https://api.openai.com/v1/images/generations",
                                    headers={"Authorization": f"Bearer {openai_key}", "Content-Type": "application/json"},
                                    json={"model": "dall-e-3", "prompt": scene["image_prompt"], "n": 1, "size": "1792x1024", "quality": "standard"},
                                    timeout=60,
                                )
                                ir.raise_for_status()
                                img_url = ir.json()["data"][0]["url"]
                                logger.info(f"[video-v2] Module {mod.id}: DALL-E fallback OK")
                            except Exception as img_err:
                                logger.warning(f"[video-v2] Module {mod.id}: DALL-E failed: {img_err}")

                        if img_url:
                            video_inputs.append({"character": base_char, "voice": base_voice, "background": {"type": "image", "url": img_url}})
                        else:
                            video_inputs.append({"character": base_char, "voice": base_voice, "background": {"type": "color", "value": "#2d1b69"}})
                    else:
                        # Avatar full-size centered for talking scenes
                        base_char = {"type": "avatar", "avatar_id": _aid, "avatar_style": "normal"}
                        video_inputs.append({"character": base_char, "voice": base_voice, "background": {"type": "color", "value": "#1a1a2e"}})

                # Step 3: Send to HeyGen
                r = httpx.post(
                    "https://api.heygen.com/v2/video/generate",
                    headers={"X-Api-Key": heygen_key, "Content-Type": "application/json"},
                    json={"video_inputs": video_inputs, "dimension": {"width": 1920, "height": 1080}},
                    timeout=30,
                )
                r.raise_for_status()
                video_id = r.json().get("data", {}).get("video_id", "")
                logger.info(f"[video-v2] Module {mod.id}: HeyGen queued with {len(video_inputs)} scenes, video_id={video_id}")
                if video_id:
                    mod.video_url = f"heygen://pending/{video_id}"
                    db.commit()
            except Exception as e:
                mod.generation_status = "failed"
                db.commit()
                logger.error(f"[video-v2] Error module {mod.id}: {e}", exc_info=True)
    finally:
        db.close()


@router.post("/generate-video-v2")
async def generate_video_v2(
    payload: GenerateVideoRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    """Queue multi-scene video generation (returns immediately)."""
    modules = db.query(Module).filter(Module.course_id == payload.course_id).order_by(Module.order).all()
    if not modules:
        raise HTTPException(status_code=404, detail="No modules found")

    if not _get_heygen_key() or not _get_openai_key():
        raise HTTPException(status_code=500, detail="HeyGen or OpenAI API key not configured")

    # Mark all as generating
    for mod in modules:
        if mod.content_text and mod.content_text.strip():
            mod.generation_status = "queued"
    db.commit()

    # Run in background
    background_tasks.add_task(_bg_generate_video_v2, payload.course_id, payload.avatar_id)

    return {
        "course_id": payload.course_id,
        "modules_queued": sum(1 for m in modules if m.content_text and m.content_text.strip()),
        "status": "queued",
        "message": "Video generation started in background. Use check-all-videos to monitor progress.",
    }


# ── Single Module Regeneration ────────────────────────────────────────────────

@router.post("/regenerate-module/{module_id}/audio")
async def regenerate_module_audio(
    module_id: int,
    voice_id: str = "IKne3meq5aSn9XLyUdCD",
    background_tasks: BackgroundTasks = None,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    """Regenerate audio for a single module."""
    mod = db.get(Module, module_id)
    if not mod:
        raise HTTPException(404, "Module not found")
    if not _get_elevenlabs_key():
        raise HTTPException(500, "ElevenLabs API key not configured")

    mod.generation_status = "queued"
    db.commit()

    def _bg():
        import httpx
        from database import SessionLocal
        s = SessionLocal()
        try:
            m = s.get(Module, module_id)
            if not m or not m.content_text:
                return
            m.generation_status = "generating"
            s.commit()
            r = httpx.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
                headers={"xi-api-key": _get_elevenlabs_key(), "Content-Type": "application/json"},
                json={"text": m.content_text[:5000], "model_id": "eleven_multilingual_v2", "voice_settings": {"stability": 0.5, "similarity_boost": 0.75}},
                timeout=120,
            )
            r.raise_for_status()
            fname = f"course_{m.course_id}_module_{m.id}.mp3"
            with open(os.path.join(UPLOAD_DIR, fname), "wb") as f:
                f.write(r.content)
            m.audio_url = f"/uploads/{fname}"
            m.generation_status = "completed"
            s.commit()
        except Exception as e:
            try:
                m2 = s.get(Module, module_id)
                if m2:
                    m2.generation_status = "failed"
                    s.commit()
            except:
                pass
            print(f"[regen-audio] Error module {module_id}: {e}")
        finally:
            s.close()

    background_tasks.add_task(_bg)
    return {"module_id": module_id, "status": "queued"}


@router.post("/regenerate-module/{module_id}/video")
async def regenerate_module_video(
    module_id: int,
    avatar_id: str = "Daisy-inskirt-20220818",
    background_tasks: BackgroundTasks = None,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    """Regenerate multi-scene video for a single module."""
    mod = db.get(Module, module_id)
    if not mod:
        raise HTTPException(404, "Module not found")
    if not _get_heygen_key() or not _get_openai_key():
        raise HTTPException(500, "HeyGen or OpenAI API key not configured")

    mod.generation_status = "queued"
    db.commit()

    def _bg():
        import httpx, json as _json
        from database import SessionLocal
        s = SessionLocal()
        try:
            m = s.get(Module, module_id)
            if not m or not m.content_text:
                return
            m.generation_status = "generating"
            s.commit()
            openai_key = _get_openai_key()
            heygen_key = _get_heygen_key()

            # Split into scenes
            prompt = f"""Divide the following educational content into 4-6 scenes for a training video.
Each scene should alternate between:
- "avatar": The instructor explains a concept (narration text, max 200 words)
- "infographic": A visual slide with KEY POINTS displayed as readable text. Provide:
  - "narration": what the avatar says while the slide shows (max 100 words)
  - "slide_title": a short title for the slide (3-8 words, in Spanish)
  - "slide_points": array of exactly 3 key points (each 5-15 words, in Spanish)
  - "slide_subtitle": optional subtitle or category

Start with an avatar intro and end with an avatar summary.
Return ONLY valid JSON:
{{"scenes": [
  {{"type":"avatar","narration":"..."}},
  {{"type":"infographic","narration":"...","slide_title":"...","slide_points":["punto 1","punto 2","punto 3"],"slide_subtitle":"..."}}
]}}

Content:
{m.content_text[:3000]}"""

            sr = httpx.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {openai_key}", "Content-Type": "application/json"},
                json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": prompt}], "temperature": 0.7, "response_format": {"type": "json_object"}},
                timeout=60,
            )
            sr.raise_for_status()
            scene_data = _json.loads(sr.json()["choices"][0]["message"]["content"])
            scenes = scene_data.get("scenes", scene_data) if isinstance(scene_data, dict) else scene_data

            from utils.infographic import generate_infographic, upload_to_supabase
            import os, uuid
            sb_url = os.getenv("SUPABASE_URL", "")
            sb_key = os.getenv("SUPABASE_SERVICE_KEY", os.getenv("SUPABASE_KEY", ""))

            video_inputs = []
            s_idx = 0
            for scene in (scenes or []):
                narration = scene.get("narration", "")[:500]
                has_slide = scene.get("type") == "infographic" and scene.get("slide_title")
                is_infographic = has_slide or (scene.get("type") == "infographic" and scene.get("image_prompt"))
                if is_infographic:
                    base_char = {
                        "type": "avatar", "avatar_id": avatar_id, "avatar_style": "normal",
                        "scale": 0.22, "offset": {"x": 0.42, "y": 0.28}
                    }
                else:
                    base_char = {"type": "avatar", "avatar_id": avatar_id, "avatar_style": "normal"}
                base_voice = {"type": "text", "input_text": narration, "voice_id": "0f3be0bc527846d78b5e88b04c439d5f"}

                if has_slide:
                    img_url = None
                    try:
                        img_bytes = generate_infographic(
                            title=scene.get("slide_title", ""),
                            points=scene.get("slide_points", []),
                            subtitle=scene.get("slide_subtitle", ""),
                        )
                        fname = f"m{module_id}_s{s_idx}_{uuid.uuid4().hex[:8]}.png"
                        img_url = upload_to_supabase(img_bytes, fname, sb_url, sb_key)
                    except Exception as gen_err:
                        print(f"[regen] Infographic gen failed: {gen_err}")
                    if img_url:
                        video_inputs.append({"character": base_char, "voice": base_voice, "background": {"type": "image", "url": img_url}})
                    else:
                        video_inputs.append({"character": base_char, "voice": base_voice, "background": {"type": "color", "value": "#2d1b69"}})
                elif is_infographic:
                    video_inputs.append({"character": base_char, "voice": base_voice, "background": {"type": "color", "value": "#2d1b69"}})
                else:
                    video_inputs.append({"character": base_char, "voice": base_voice, "background": {"type": "color", "value": "#1a1a2e"}})
                s_idx += 1

            if video_inputs:
                r = httpx.post(
                    "https://api.heygen.com/v2/video/generate",
                    headers={"X-Api-Key": heygen_key, "Content-Type": "application/json"},
                    json={"video_inputs": video_inputs, "dimension": {"width": 1920, "height": 1080}},
                    timeout=30,
                )
                r.raise_for_status()
                vid = r.json().get("data", {}).get("video_id", "")
                if vid:
                    m.video_url = f"heygen://pending/{vid}"
                    s.commit()
        except Exception as e:
            try:
                m2 = s.get(Module, module_id)
                if m2:
                    m2.generation_status = "failed"
                    s.commit()
            except:
                pass
            print(f"[regen-video] Error module {module_id}: {e}")
        finally:
            s.close()

    background_tasks.add_task(_bg)
    return {"module_id": module_id, "status": "queued"}
