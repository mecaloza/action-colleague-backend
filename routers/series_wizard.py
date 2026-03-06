"""
Micro-Series Wizard — AI-powered narrative video series generation.

Flow:
1. POST /series/ai/generate-script  → GPT generates episodes + scenes structure
2. POST /series/ai/create           → Save structure to DB
3. POST /series/ai/generate-videos  → Kick off Sora video generation
4. POST /series/ai/generate-audio   → Kick off ElevenLabs narration
5. GET  /series/ai/status/{id}      → Poll generation status
"""

import json
import os
import uuid
from typing import List, Optional

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from auth import get_current_user, require_admin
from database import SessionLocal, get_db
from models import Episode, Scene, Series, User
from schemas import (
    EpisodeResponse,
    SceneResponse,
    SeriesGenerateRequest,
    SeriesListItem,
    SeriesResponse,
)

router = APIRouter(prefix="/series/ai", tags=["series-wizard"])


# ── Helpers ──────────────────────────────────────────────────────────


def _get_openai_key() -> str:
    return os.getenv("OPENAI_API_KEY", "")


def _get_elevenlabs_key() -> str:
    return os.getenv("ELEVENLABS_API_KEY", "")


def _get_elevenlabs_voice_id() -> str:
    return os.getenv("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")


def _supabase_url() -> str:
    return os.getenv("SUPABASE_URL", "")


def _supabase_key() -> str:
    return os.getenv("SUPABASE_SERVICE_KEY", os.getenv("SUPABASE_KEY", ""))


def _upload_to_supabase(file_bytes: bytes, filename: str, bucket: str, content_type: str = "video/mp4") -> str:
    """Upload a file to Supabase Storage and return the public URL."""
    sb_url = _supabase_url()
    sb_key = _supabase_key()
    if not sb_url or not sb_key:
        return ""

    headers = {
        "Authorization": f"Bearer {sb_key}",
        "apikey": sb_key,
    }

    # Ensure bucket exists
    httpx.post(
        f"{sb_url}/storage/v1/bucket",
        headers={**headers, "Content-Type": "application/json"},
        json={"id": bucket, "name": bucket, "public": True},
        timeout=10,
    )

    # Upload file
    httpx.post(
        f"{sb_url}/storage/v1/object/{bucket}/{filename}",
        headers={**headers, "Content-Type": content_type, "x-upsert": "true"},
        content=file_bytes,
        timeout=60,
    )

    return f"{sb_url}/storage/v1/object/public/{bucket}/{filename}"


# ── Local Schemas (request/response for this router) ─────────────────


class SceneProposal(BaseModel):
    order: int
    sora_prompt: str
    narration_text: str
    duration_seconds: int = 8  # 4, 8, or 12


class EpisodeProposal(BaseModel):
    title: str
    synopsis: str
    order: int
    scenes: List[SceneProposal] = []


class GenerateScriptResponse(BaseModel):
    title: str
    description: str
    category: str
    episodes: List[EpisodeProposal]


class CreateSeriesRequest(BaseModel):
    title: str
    description: str = ""
    category: str = "custom"
    episodes: List[EpisodeProposal] = []


class GenerateVideosResponse(BaseModel):
    series_id: int
    scenes_queued: int
    message: str


class GenerateAudioResponse(BaseModel):
    series_id: int
    scenes_queued: int
    message: str


class SceneStatus(BaseModel):
    scene_id: int
    order: int
    status: str
    video_url: str = ""
    audio_url: str = ""


class EpisodeStatus(BaseModel):
    episode_id: int
    title: str
    status: str
    scenes: List[SceneStatus] = []


class SeriesStatusResponse(BaseModel):
    series_id: int
    title: str
    status: str
    episodes: List[EpisodeStatus] = []


# ── 1. Generate Script (GPT) ────────────────────────────────────────


@router.post("/generate-script", response_model=GenerateScriptResponse)
async def generate_script(
    payload: SeriesGenerateRequest,
    _admin: User = Depends(require_admin),
):
    """
    Use GPT to generate a full micro-series structure:
    episodes with scenes, Sora prompts, and narration text.
    """
    openai_key = _get_openai_key()
    if not openai_key:
        raise HTTPException(status_code=500, detail="OpenAI API key not configured")

    system_prompt = (
        "You are an expert scriptwriter for corporate training micro-series. "
        "You create short, cinematic narrative episodes for workplace training. "
        "Each episode has 3-6 scenes. Each scene will be generated as a short video "
        "using OpenAI Sora, so your visual descriptions must be detailed and cinematic.\n\n"
        "For each scene provide:\n"
        "- sora_prompt: A detailed cinematic prompt describing the shot type, subjects, "
        "action, setting, lighting, and mood. Be specific about camera angles and movements.\n"
        "- narration_text: Voiceover narration for the scene (2-4 sentences).\n"
        "- duration_seconds: 4, 8, or 12 seconds (choose based on scene complexity).\n\n"
        "Return ONLY valid JSON with this structure:\n"
        '{"episodes": [{"title": "...", "synopsis": "...", "order": 1, '
        '"scenes": [{"order": 1, "sora_prompt": "...", "narration_text": "...", '
        '"duration_seconds": 8}]}]}'
    )

    user_prompt = (
        f"Create a micro-series with {payload.num_episodes} episodes.\n"
        f"Title: {payload.title}\n"
        f"Description: {payload.description}\n"
        f"Category: {payload.category}\n"
    )
    if payload.case_description:
        user_prompt += f"Case/Scenario: {payload.case_description}\n"

    try:
        r = httpx.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {openai_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": "gpt-4o-mini",
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.7,
                "response_format": {"type": "json_object"},
            },
            timeout=60,
        )
        r.raise_for_status()
        data = json.loads(r.json()["choices"][0]["message"]["content"])
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"OpenAI API error: {e}")

    episodes = []
    for ep in data.get("episodes", []):
        scenes = []
        for sc in ep.get("scenes", []):
            dur = sc.get("duration_seconds", 8)
            if dur not in (4, 8, 12):
                dur = 8
            scenes.append(SceneProposal(
                order=sc.get("order", 0),
                sora_prompt=sc.get("sora_prompt", ""),
                narration_text=sc.get("narration_text", ""),
                duration_seconds=dur,
            ))
        episodes.append(EpisodeProposal(
            title=ep.get("title", ""),
            synopsis=ep.get("synopsis", ""),
            order=ep.get("order", 0),
            scenes=scenes,
        ))

    return GenerateScriptResponse(
        title=payload.title,
        description=payload.description,
        category=payload.category,
        episodes=episodes,
    )


# ── 2. Create Series in DB ──────────────────────────────────────────


@router.post("/create", response_model=SeriesResponse, status_code=201)
async def create_series(
    payload: CreateSeriesRequest,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """
    Save the generated script structure to the database.
    Creates Series + Episodes + Scenes in one transaction.
    """
    series = Series(
        title=payload.title,
        description=payload.description,
        category=payload.category,
        status="draft",
        created_by=admin.id,
    )
    db.add(series)
    db.flush()  # get series.id

    for ep_data in payload.episodes:
        episode = Episode(
            series_id=series.id,
            title=ep_data.title,
            synopsis=ep_data.synopsis,
            order=ep_data.order,
            status="draft",
        )
        db.add(episode)
        db.flush()  # get episode.id

        total_duration = 0
        for sc_data in ep_data.scenes:
            scene = Scene(
                episode_id=episode.id,
                order=sc_data.order,
                sora_prompt=sc_data.sora_prompt,
                narration_text=sc_data.narration_text,
                duration_seconds=sc_data.duration_seconds,
                status="draft",
            )
            db.add(scene)
            total_duration += sc_data.duration_seconds

        episode.duration_seconds = total_duration

    db.commit()
    db.refresh(series)
    return series


# ── 3. Generate Videos (Sora) ────────────────────────────────────────


def _generate_videos_background(series_id: int):
    """Background task: generate Sora videos for all scenes in a series."""
    db = SessionLocal()
    try:
        openai_key = _get_openai_key()
        if not openai_key:
            return

        series = db.get(Series, series_id)
        if not series:
            return

        series.status = "generating"
        db.commit()

        for episode in series.episodes:
            episode.status = "generating"
            db.commit()

            for scene in episode.scenes:
                if scene.status == "completed" and scene.video_url:
                    continue
                try:
                    scene.status = "generating_video"
                    db.commit()

                    # Create Sora video
                    r = httpx.post(
                        "https://api.openai.com/v1/videos",
                        headers={
                            "Authorization": f"Bearer {openai_key}",
                            "Content-Type": "application/json",
                        },
                        json={
                            "model": "sora-2",
                            "prompt": scene.sora_prompt,
                            "size": "1280x720",
                            "seconds": str(scene.duration_seconds),
                        },
                        timeout=30,
                    )
                    r.raise_for_status()
                    video_id = r.json().get("id", "")
                    scene.sora_video_id = video_id

                    # Poll until completed
                    for _ in range(120):  # max ~10 minutes
                        import time
                        time.sleep(5)
                        status_r = httpx.get(
                            f"https://api.openai.com/v1/videos/{video_id}",
                            headers={"Authorization": f"Bearer {openai_key}"},
                            timeout=15,
                        )
                        status_r.raise_for_status()
                        status_data = status_r.json()
                        if status_data.get("status") == "completed":
                            break
                        if status_data.get("status") == "failed":
                            raise Exception(f"Sora generation failed: {status_data.get('error', 'unknown')}")
                    else:
                        raise Exception("Sora generation timed out")

                    # Download video content
                    dl_r = httpx.get(
                        f"https://api.openai.com/v1/videos/{video_id}/content",
                        headers={"Authorization": f"Bearer {openai_key}"},
                        timeout=120,
                    )
                    dl_r.raise_for_status()

                    # Upload to Supabase
                    filename = f"series_{series_id}_ep_{episode.id}_scene_{scene.id}.mp4"
                    public_url = _upload_to_supabase(dl_r.content, filename, "series-videos", "video/mp4")

                    if public_url:
                        scene.video_url = public_url
                    else:
                        scene.video_url = f"sora://completed/{video_id}"

                    scene.status = "completed"
                    db.commit()

                except Exception as e:
                    scene.status = "failed"
                    db.commit()
                    print(f"[sora] Error scene {scene.id}: {e}")

            # Check if all scenes completed
            all_done = all(s.status == "completed" for s in episode.scenes)
            episode.status = "completed" if all_done else "failed"
            db.commit()

        # Update series status
        all_episodes_done = all(ep.status == "completed" for ep in series.episodes)
        series.status = "published" if all_episodes_done else "draft"
        db.commit()

    finally:
        db.close()


@router.post("/generate-videos/{series_id}", response_model=GenerateVideosResponse)
async def generate_videos(
    series_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    """Kick off Sora video generation for all scenes in a series."""
    series = db.get(Series, series_id)
    if not series:
        raise HTTPException(status_code=404, detail="Series not found")

    if not _get_openai_key():
        raise HTTPException(status_code=500, detail="OpenAI API key not configured")

    # Count scenes to queue
    scenes_queued = 0
    for episode in series.episodes:
        for scene in episode.scenes:
            if scene.status != "completed" or not scene.video_url:
                scene.status = "draft"
                scenes_queued += 1
    db.commit()

    background_tasks.add_task(_generate_videos_background, series_id)

    return GenerateVideosResponse(
        series_id=series_id,
        scenes_queued=scenes_queued,
        message=f"Video generation queued for {scenes_queued} scenes.",
    )


# ── 4. Generate Audio (ElevenLabs) ───────────────────────────────────


def _generate_audio_background(series_id: int):
    """Background task: generate ElevenLabs audio for all scenes in a series."""
    db = SessionLocal()
    try:
        el_key = _get_elevenlabs_key()
        voice_id = _get_elevenlabs_voice_id()
        if not el_key:
            return

        series = db.get(Series, series_id)
        if not series:
            return

        for episode in series.episodes:
            for scene in episode.scenes:
                if scene.audio_url:
                    continue
                if not scene.narration_text or not scene.narration_text.strip():
                    continue
                try:
                    scene.status = "generating_audio"
                    db.commit()

                    r = httpx.post(
                        f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
                        headers={
                            "xi-api-key": el_key,
                            "Content-Type": "application/json",
                        },
                        json={
                            "text": scene.narration_text[:5000],
                            "model_id": "eleven_multilingual_v2",
                            "voice_settings": {
                                "stability": 0.5,
                                "similarity_boost": 0.75,
                            },
                        },
                        timeout=120,
                    )
                    r.raise_for_status()

                    # Upload to Supabase
                    filename = f"series_{series_id}_ep_{episode.id}_scene_{scene.id}.mp3"
                    public_url = _upload_to_supabase(r.content, filename, "series-audio", "audio/mpeg")

                    if public_url:
                        scene.audio_url = public_url
                    else:
                        scene.audio_url = ""

                    # Only mark completed if video is also done
                    if scene.video_url:
                        scene.status = "completed"
                    else:
                        scene.status = "draft"
                    db.commit()

                except Exception as e:
                    scene.status = "failed"
                    db.commit()
                    print(f"[audio] Error scene {scene.id}: {e}")

    finally:
        db.close()


@router.post("/generate-audio/{series_id}", response_model=GenerateAudioResponse)
async def generate_audio(
    series_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    """Kick off ElevenLabs audio generation for all scenes in a series."""
    series = db.get(Series, series_id)
    if not series:
        raise HTTPException(status_code=404, detail="Series not found")

    if not _get_elevenlabs_key():
        raise HTTPException(status_code=500, detail="ElevenLabs API key not configured")

    scenes_queued = 0
    for episode in series.episodes:
        for scene in episode.scenes:
            if scene.narration_text and scene.narration_text.strip() and not scene.audio_url:
                scenes_queued += 1
    db.commit()

    background_tasks.add_task(_generate_audio_background, series_id)

    return GenerateAudioResponse(
        series_id=series_id,
        scenes_queued=scenes_queued,
        message=f"Audio generation queued for {scenes_queued} scenes.",
    )


# ── 5. Status ────────────────────────────────────────────────────────


@router.get("/status/{series_id}", response_model=SeriesStatusResponse)
async def get_series_status(
    series_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """Return generation status of all scenes in a series."""
    series = db.get(Series, series_id)
    if not series:
        raise HTTPException(status_code=404, detail="Series not found")

    episodes_status = []
    for ep in series.episodes:
        scenes_status = [
            SceneStatus(
                scene_id=sc.id,
                order=sc.order,
                status=sc.status,
                video_url=sc.video_url or "",
                audio_url=sc.audio_url or "",
            )
            for sc in ep.scenes
        ]
        episodes_status.append(EpisodeStatus(
            episode_id=ep.id,
            title=ep.title,
            status=ep.status,
            scenes=scenes_status,
        ))

    return SeriesStatusResponse(
        series_id=series.id,
        title=series.title,
        status=series.status,
        episodes=episodes_status,
    )


# ── 6. CRUD ──────────────────────────────────────────────────────────


@router.get("/", response_model=List[SeriesListItem])
async def list_series(
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """List all series."""
    return db.query(Series).order_by(Series.created_at.desc()).offset(skip).limit(limit).all()


@router.get("/{series_id}", response_model=SeriesResponse)
async def get_series(
    series_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """Get a series with all episodes and scenes."""
    series = db.get(Series, series_id)
    if not series:
        raise HTTPException(status_code=404, detail="Series not found")
    return series


@router.delete("/{series_id}", status_code=204)
async def delete_series(
    series_id: int,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    """Delete a series and all its episodes/scenes (cascade)."""
    series = db.get(Series, series_id)
    if not series:
        raise HTTPException(status_code=404, detail="Series not found")
    db.delete(series)
    db.commit()
