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
import logging
import os
import uuid
from typing import List, Optional

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_admin
from app.db.session import SessionLocal, get_db
from app.db.models import Episode, Scene, Series, User
from app.schemas import (
    EpisodeResponse,
    SceneResponse,
    SeriesGenerateRequest,
    SeriesListItem,
    SeriesResponse,
)

router = APIRouter(prefix="/series/ai", tags=["series-wizard"])
logger = logging.getLogger(__name__)


# ── Helpers ──────────────────────────────────────────────────────────


def _get_openai_key() -> str:
    return os.getenv("OPENAI_API_KEY", "")


def _get_elevenlabs_key() -> str:
    return os.getenv("ELEVENLABS_API_KEY", "")


def _get_elevenlabs_voice_id() -> str:
    # Default: "Daniel" - Steady Broadcaster, professional narration voice
    # Works great in Spanish with eleven_multilingual_v2
    return os.getenv("ELEVENLABS_VOICE_ID", "onwK4e9ZLuTAKqWnGpdt")


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
        "You create cinematic narrative episodes for workplace training.\n\n"
        "IMPORTANT RULES:\n"
        "- Each episode MUST have 15-20 scenes to create long, engaging content (aim for 18).\n"
        "- This is critical: more scenes = longer episodes = better engagement and context.\n"
        "- ALL narration_text MUST be written in SPANISH (Latin American Spanish).\n"
        "- Episode titles and synopses MUST also be in SPANISH.\n"
        "- sora_prompt MUST be in ENGLISH (Sora works best with English prompts).\n"
        "- ALL scenes MUST use duration_seconds of 12 (maximum allowed).\n"
        "- Each scene will be generated as a short video using OpenAI Sora, "
        "so visual descriptions must be detailed and cinematic.\n\n"
        "For each scene provide:\n"
        "- sora_prompt: A detailed cinematic prompt IN ENGLISH describing shot type, subjects, "
        "action, setting, lighting, and mood. Be specific about camera angles and movements.\n"
        "- narration_text: Voiceover narration IN SPANISH (3-5 sentences, engaging and educational). "
        "Build a narrative arc — start with context, develop tension, and resolve with learning.\n"
        "- duration_seconds: ALWAYS 12 seconds.\n\n"
        "Return ONLY valid JSON with this structure:\n"
        '{"episodes": [{"title": "...", "synopsis": "...", "order": 1, '
        '"scenes": [{"order": 1, "sora_prompt": "...", "narration_text": "...", '
        '"duration_seconds": 12}]}]}'
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


# ── 3. Generate All Content (Video + Audio + Merge) ─────────────────


def _merge_video_audio(video_bytes: bytes, audio_bytes: bytes, output_path: str) -> bytes:
    """Merge Sora video (mute) + ElevenLabs audio into one MP4 using FFmpeg."""
    import subprocess
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as vf:
        vf.write(video_bytes)
        video_path = vf.name
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as af:
        af.write(audio_bytes)
        audio_path = af.name

    try:
        # Merge: use video duration, loop/pad audio if shorter, cut if longer
        subprocess.run([
            "ffmpeg", "-y",
            "-i", video_path,
            "-i", audio_path,
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "128k",
            "-shortest",
            "-map", "0:v:0", "-map", "1:a:0",
            output_path,
        ], check=True, capture_output=True, timeout=60)

        with open(output_path, "rb") as f:
            return f.read()
    finally:
        import os
        os.unlink(video_path)
        os.unlink(audio_path)
        if os.path.exists(output_path):
            os.unlink(output_path)


def _concatenate_scenes(scene_urls: list, output_path: str) -> bytes:
    """Concatenate multiple scene videos into one episode video using FFmpeg."""
    import subprocess
    import tempfile

    # Download all scene files
    scene_files = []
    for i, url in enumerate(scene_urls):
        r = httpx.get(url, timeout=120)
        r.raise_for_status()
        tf = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
        tf.write(r.content)
        tf.close()
        scene_files.append(tf.name)

    try:
        # Create concat file list
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as lf:
            for sf in scene_files:
                lf.write(f"file '{sf}'\n")
            list_path = lf.name

        # Re-encode all to same format then concat
        normalized = []
        for sf in scene_files:
            norm_path = sf.replace(".mp4", "_norm.mp4")
            subprocess.run([
                "ffmpeg", "-y", "-i", sf,
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k",
                "-r", "24",
                "-s", "1280x720",
                "-ar", "44100", "-ac", "2",
                norm_path,
            ], check=True, capture_output=True, timeout=120)
            normalized.append(norm_path)

        # Update concat list with normalized files
        with open(list_path, "w") as lf:
            for nf in normalized:
                lf.write(f"file '{nf}'\n")

        subprocess.run([
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", list_path,
            "-c", "copy",
            output_path,
        ], check=True, capture_output=True, timeout=180)

        with open(output_path, "rb") as f:
            return f.read()
    finally:
        import os
        for sf in scene_files:
            if os.path.exists(sf):
                os.unlink(sf)
            norm = sf.replace(".mp4", "_norm.mp4")
            if os.path.exists(norm):
                os.unlink(norm)
        if os.path.exists(list_path):
            os.unlink(list_path)
        if os.path.exists(output_path):
            os.unlink(output_path)


def _generate_all_background(series_id: int):
    """
    Full pipeline per scene: Sora video → ElevenLabs audio → FFmpeg merge.
    Then concatenate all scenes into episode final video.
    """
    import time
    import tempfile

    db = SessionLocal()
    try:
        openai_key = _get_openai_key()
        el_key = _get_elevenlabs_key()
        voice_id = _get_elevenlabs_voice_id()

        series = db.get(Series, series_id)
        if not series:
            return

        series.status = "generating"
        db.commit()

        for episode in series.episodes:
            episode.status = "generating"
            db.commit()
            scene_final_urls = []

            for scene in sorted(episode.scenes, key=lambda s: s.order):
                if scene.status == "completed" and scene.video_url and scene.audio_url:
                    scene_final_urls.append(scene.video_url)
                    continue

                try:
                    # ── Step 1: Generate Sora video ──
                    scene.status = "generating_video"
                    db.commit()

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
                    db.commit()

                    # Poll until completed
                    for _ in range(120):
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
                            raise Exception(f"Sora failed: {status_data.get('error')}")
                    else:
                        raise Exception("Sora generation timed out")

                    # Download video
                    dl_r = httpx.get(
                        f"https://api.openai.com/v1/videos/{video_id}/content",
                        headers={"Authorization": f"Bearer {openai_key}"},
                        timeout=120,
                    )
                    dl_r.raise_for_status()
                    raw_video_bytes = dl_r.content

                    # ── Step 2: Generate ElevenLabs audio ──
                    scene.status = "generating_audio"
                    db.commit()

                    audio_bytes = None
                    if el_key and scene.narration_text and scene.narration_text.strip():
                        ar = httpx.post(
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
                        ar.raise_for_status()
                        audio_bytes = ar.content

                    # ── Step 3: Merge video + audio with FFmpeg ──
                    scene.status = "merging"
                    db.commit()

                    if audio_bytes:
                        merge_out = tempfile.mktemp(suffix=".mp4")
                        final_bytes = _merge_video_audio(raw_video_bytes, audio_bytes, merge_out)
                    else:
                        final_bytes = raw_video_bytes

                    # Upload merged video to Supabase
                    filename = f"series_{series_id}_ep_{episode.id}_scene_{scene.id}.mp4"
                    public_url = _upload_to_supabase(final_bytes, filename, "series-videos", "video/mp4")

                    # Also upload raw audio separately (for subtitle/reference)
                    if audio_bytes:
                        audio_filename = f"series_{series_id}_ep_{episode.id}_scene_{scene.id}.mp3"
                        audio_url = _upload_to_supabase(audio_bytes, audio_filename, "series-audio", "audio/mpeg")
                        scene.audio_url = audio_url or ""

                    scene.video_url = public_url or ""
                    scene.status = "completed"
                    db.commit()

                    if public_url:
                        scene_final_urls.append(public_url)

                    logger.info("series_scene_completed", extra={"scene_id": scene.id})

                except Exception as e:
                    scene.status = "failed"
                    db.commit()
                    logger.error("series_scene_error", extra={"scene_id": scene.id, "error": str(e)[:300]})

            # ── Step 4: Concatenate all scenes into episode video ──
            if scene_final_urls and len(scene_final_urls) == len(episode.scenes):
                try:
                    logger.info("series_episode_concatenating", extra={"episode_id": episode.id, "scenes": len(scene_final_urls)})
                    concat_out = tempfile.mktemp(suffix=".mp4")
                    episode_bytes = _concatenate_scenes(scene_final_urls, concat_out)

                    ep_filename = f"series_{series_id}_ep_{episode.id}_full.mp4"
                    ep_url = _upload_to_supabase(episode_bytes, ep_filename, "series-videos", "video/mp4")

                    if ep_url:
                        episode.final_video_url = ep_url
                    episode.status = "completed"
                    total_dur = sum(s.duration_seconds for s in episode.scenes)
                    episode.duration_seconds = total_dur
                    db.commit()
                    logger.info("series_episode_assembled", extra={"episode_id": episode.id, "duration_s": total_dur})

                except Exception as e:
                    episode.status = "completed"  # scenes are done, just concat failed
                    db.commit()
                    logger.warning("series_episode_concat_error", extra={"episode_id": episode.id, "error": str(e)[:300]})
            else:
                all_done = all(s.status == "completed" for s in episode.scenes)
                episode.status = "completed" if all_done else "failed"
                db.commit()

        # Update series status
        all_episodes_done = all(ep.status == "completed" for ep in series.episodes)
        series.status = "published" if all_episodes_done else "draft"
        db.commit()
        logger.info("series_pipeline_finished", extra={"series_id": series_id, "published": all_episodes_done})

    finally:
        db.close()


@router.post("/generate-videos/{series_id}", response_model=GenerateVideosResponse)
async def generate_videos(
    series_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    """Kick off FULL pipeline: Sora video → ElevenLabs audio → FFmpeg merge → episode concat."""
    series = db.get(Series, series_id)
    if not series:
        raise HTTPException(status_code=404, detail="Series not found")

    if not _get_openai_key():
        raise HTTPException(status_code=500, detail="OpenAI API key not configured")

    scenes_queued = 0
    for episode in series.episodes:
        for scene in episode.scenes:
            if scene.status != "completed" or not scene.video_url:
                scene.status = "draft"
                scenes_queued += 1
    db.commit()

    background_tasks.add_task(_generate_all_background, series_id)

    return GenerateVideosResponse(
        series_id=series_id,
        scenes_queued=scenes_queued,
        message=f"Full pipeline (video+audio+merge) queued for {scenes_queued} scenes.",
    )


# ── 4. Generate Audio (kept for standalone re-generation) ────────────


def _generate_audio_background(series_id: int):
    """Background task: generate ElevenLabs audio for scenes missing audio only."""
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
                    filename = f"series_{series_id}_ep_{episode.id}_scene_{scene.id}.mp3"
                    public_url = _upload_to_supabase(r.content, filename, "series-audio", "audio/mpeg")
                    if public_url:
                        scene.audio_url = public_url
                    db.commit()
                except Exception as e:
                    logger.error("series_audio_error", extra={"scene_id": scene.id, "error": str(e)[:300]})

    finally:
        db.close()


@router.post("/generate-audio/{series_id}", response_model=GenerateAudioResponse)
async def generate_audio(
    series_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    """Kick off ElevenLabs audio generation for scenes missing audio."""
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
