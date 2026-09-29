"""
User Videos Router — Manual course creation video uploads

Endpoints:
- POST /api/v1/videos/upload → Upload recorded video to Supabase Storage
- POST /api/v1/videos/compose → Compose video with slides (Picture-in-Picture)
- GET  /api/v1/videos/{video_id} → Get video metadata
- GET  /api/v1/videos → List videos by user or module
"""

import os
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import List, Optional

import httpx
from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_admin
from app.db.session import get_db
from app.db.models import Module, User, UserVideo
from app.schemas.legacy import UserVideoOut, VideoUploadResponse

router = APIRouter(prefix="/videos", tags=["videos"], dependencies=[Depends(require_admin)])

# ── Helpers ──────────────────────────────────────────────────────────


def _supabase_url() -> str:
    return os.getenv("SUPABASE_URL", "")


def _supabase_key() -> str:
    return os.getenv("SUPABASE_SERVICE_KEY", os.getenv("SUPABASE_KEY", ""))


def _upload_to_supabase(file_bytes: bytes, filename: str, bucket: str, content_type: str = "video/mp4") -> str:
    """Upload a file to Supabase Storage and return the public URL."""
    sb_url = _supabase_url()
    sb_key = _supabase_key()
    if not sb_url or not sb_key:
        raise HTTPException(500, "Supabase Storage not configured")

    headers = {
        "Authorization": f"Bearer {sb_key}",
        "apikey": sb_key,
    }

    # Ensure bucket exists (create if missing)
    try:
        httpx.post(
            f"{sb_url}/storage/v1/bucket",
            headers={**headers, "Content-Type": "application/json"},
            json={"id": bucket, "name": bucket, "public": True},
            timeout=10,
        )
    except Exception:
        pass  # Bucket might already exist

    # Upload file
    try:
        response = httpx.post(
            f"{sb_url}/storage/v1/object/{bucket}/{filename}",
            headers={**headers, "Content-Type": content_type, "x-upsert": "true"},
            content=file_bytes,
            timeout=120,  # 2min timeout for large videos
        )
        response.raise_for_status()
    except Exception as e:
        raise HTTPException(500, f"Failed to upload to Supabase: {str(e)}")

    return f"{sb_url}/storage/v1/object/public/{bucket}/{filename}"


# ── Endpoints ────────────────────────────────────────────────────────


@router.post("/upload", response_model=VideoUploadResponse)
async def upload_video(
    file: UploadFile = File(...),
    module_id: Optional[int] = Form(None),
    duration: Optional[int] = Form(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),  # ✅ Auth re-habilitado
):
    """
    Upload a user-recorded video.

    **Validations:**
    - Format: video/webm or video/mp4
    - Size: < 500MB
    - Duration: < 30 min (1800 seconds)

    **Returns:** Video metadata including storage URL
    """
    # Validate MIME type
    if file.content_type not in ["video/webm", "video/mp4"]:
        raise HTTPException(400, f"Invalid format: {file.content_type}. Allowed: video/webm, video/mp4")

    # Read file
    file_bytes = await file.read()
    file_size = len(file_bytes)

    # Validate size (500MB)
    max_size = 500 * 1024 * 1024  # 500MB
    if file_size > max_size:
        raise HTTPException(400, f"File too large: {file_size / (1024*1024):.1f}MB. Max: 500MB")

    # Validate duration
    if duration and duration > 1800:  # 30 minutes
        raise HTTPException(400, f"Duration too long: {duration}s. Max: 1800s (30 min)")

    # Determine format
    file_format = "webm" if file.content_type == "video/webm" else "mp4"
    extension = file_format

    # Generate unique ID and filename
    video_id = str(uuid.uuid4())
    filename = f"{video_id}.{extension}"

    # Upload to Supabase Storage
    storage_url = _upload_to_supabase(file_bytes, filename, "user-videos", file.content_type)

    # FIX: Ensure module_id is NULL if 0 or None (FK constraint requirement)
    effective_module_id = module_id if module_id and module_id > 0 else None

    # Save metadata to DB
    video = UserVideo(
        id=video_id,
        module_id=effective_module_id,  # NULL if no module associated
        user_id=current_user.id,  # ✅ Using authenticated user
        storage_url=storage_url,
        duration=duration,
        file_size=file_size,
        format=file_format,
        status="uploaded",
    )
    db.add(video)
    db.commit()
    db.refresh(video)

    return VideoUploadResponse(
        video_id=video.id,
        storage_url=video.storage_url,
        status=video.status,
        duration=video.duration,
        file_size=video.file_size,
        format=video.format,
    )


@router.get("/{video_id}", response_model=UserVideoOut)
def get_video(
    video_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get video metadata by ID."""
    video = db.query(UserVideo).filter(UserVideo.id == video_id).first()
    if not video:
        raise HTTPException(404, "Video not found")

    # Users can only access their own videos (unless admin)
    if video.user_id != current_user.id and current_user.role != "admin":
        raise HTTPException(403, "Access denied")

    return video


@router.get("/", response_model=list[UserVideoOut])
def list_videos(
    module_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    List videos.

    - If `module_id` is provided: list videos for that module
    - Otherwise: list all videos by the current user
    """
    query = db.query(UserVideo)

    if module_id:
        # Filter by module
        query = query.filter(UserVideo.module_id == module_id)
    else:
        # Filter by current user (unless admin)
        if current_user.role != "admin":
            query = query.filter(UserVideo.user_id == current_user.id)

    videos = query.order_by(UserVideo.created_at.desc()).all()
    return videos


# ── Video Composition (Task #126) ───────────────────────────────────


class ComposeVideoRequest(BaseModel):
    video_id: str
    slide_images: List[str]  # URLs de slides (PNGs)
    layout: str = "pip-medium"  # Solo Picture-in-Picture por ahora


class ComposeVideoResponse(BaseModel):
    status: str
    video_url: str
    video_id: str


def _download_file(url: str, dest_path: Path) -> None:
    """Download a file from URL to local path."""
    try:
        response = httpx.get(url, timeout=60, follow_redirects=True)
        response.raise_for_status()
        dest_path.write_bytes(response.content)
    except Exception as e:
        raise HTTPException(500, f"Failed to download file from {url}: {str(e)}")


def _get_video_duration(video_path: Path) -> float:
    """Get video duration in seconds using ffprobe with multiple fallbacks."""
    import logging

    logger = logging.getLogger(__name__)

    try:
        # Attempt 1: Try format duration
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(video_path),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        duration_str = result.stdout.strip()

        if duration_str and duration_str != "N/A":
            return float(duration_str)

        # Attempt 2: Try video stream duration
        logger.warning(f"Format duration is N/A, trying stream duration for {video_path}")
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(video_path),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        duration_str = result.stdout.strip()

        if duration_str and duration_str != "N/A":
            return float(duration_str)

        # Fallback: Use default duration
        logger.warning(f"Could not get duration for {video_path}, using default 60s")
        return 60.0

    except ValueError as e:
        logger.error(f"Failed to parse duration for {video_path}: {e}")
        return 60.0
    except Exception as e:
        logger.error(f"Error getting video duration for {video_path}: {e}")
        return 60.0


@router.post("/compose", response_model=ComposeVideoResponse)
async def compose_video(
    video_id: str = Form(...),
    slides: List[UploadFile] = File(...),
    layout: str = Form("pip-medium"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Compose video with slides using Picture-in-Picture layout.

    **MVP SIMPLE:**
    - Layout: Picture-in-Picture básico (video pequeño sobre slide)
    - Slides: Usa PRIMER slide como fondo fijo (no timeline)
    - Processing: Sync (sin background jobs por ahora)

    **Parameters:**
    - `video_id`: ID del video subido (Form)
    - `slides`: Archivos de slides (PNGs) - solo se usa el primero (multipart/form-data)
    - `layout`: Layout ("pip-medium" por defecto)

    **Returns:** Video combinado y URL final
    """
    # 1. Validar que el video existe y pertenece al usuario
    video = db.query(UserVideo).filter(UserVideo.id == video_id).first()
    if not video:
        raise HTTPException(404, "Video not found")
    if video.user_id != current_user.id and current_user.role != "admin":
        raise HTTPException(403, "Access denied")

    # 2. Validar que hay al menos un slide
    if not slides:
        raise HTTPException(400, "At least one slide image is required")

    # 3. Update status del video
    video.status = "processing"
    db.commit()

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)

            # 4. Download video from Supabase
            video_path = tmp_path / f"input_video.{video.format}"
            _download_file(video.storage_url, video_path)

            # 5. Save PRIMER slide (MVP: solo un slide fijo)
            slide_path = tmp_path / "slide.png"
            slide_bytes = await slides[0].read()
            slide_path.write_bytes(slide_bytes)

            # 6. Get video duration (prefer DB metadata, fallback to FFprobe)
            if video.duration:
                duration = video.duration
            else:
                duration = _get_video_duration(video_path)

            # 7. FFmpeg processing - Picture-in-Picture
            output_path = tmp_path / "composed.mp4"

            ffmpeg_cmd = [
                "ffmpeg",
                "-y",  # Overwrite output
                "-loop",
                "1",  # Loop slide
                "-t",
                str(duration),  # Match video duration
                "-i",
                str(slide_path),  # Slide background
                "-i",
                str(video_path),  # User video
                "-filter_complex",
                # Scale user video to 480x270 (small PiP) and overlay bottom-right
                "[1:v]scale=480:270[pip];[0:v][pip]overlay=W-w-20:H-h-20",
                "-c:v",
                "libx264",  # H.264 codec
                "-preset",
                "medium",  # Balance between speed and quality
                "-crf",
                "23",  # Quality (lower = better, 23 is good default)
                "-c:a",
                "copy",  # Copy audio without re-encoding
                "-shortest",  # Finish when shortest input ends
                str(output_path),
            ]

            # Execute FFmpeg
            result = subprocess.run(
                ffmpeg_cmd,
                capture_output=True,
                text=True,
                timeout=300,  # 5 min timeout
            )

            if result.returncode != 0:
                raise HTTPException(500, f"FFmpeg failed: {result.stderr}")

            # 8. Upload composed video to Supabase Storage (PERMANENTE)
            composed_bytes = output_path.read_bytes()
            composed_filename = f"composed/{video_id}.mp4"
            composed_url = _upload_to_supabase(composed_bytes, composed_filename, "user-videos", "video/mp4")

            # 9. Update video record with PERMANENT storage URL
            video.storage_url = composed_url
            video.status = "ready"
            video.format = "mp4"
            db.commit()

            # 10. Update module video_url if associated with a module
            if video.module_id:
                module = db.query(Module).filter(Module.id == video.module_id).first()
                if module:
                    module.video_url = composed_url
                    module.generation_status = "completed"
                    db.commit()

            # 11. Cleanup temp file
            output_path.unlink(missing_ok=True)

            return ComposeVideoResponse(status="completed", video_url=composed_url, video_id=video.id)

    except subprocess.TimeoutExpired:
        video.status = "failed"
        db.commit()
        raise HTTPException(500, "Video processing timeout (>5 minutes)")
    except Exception as e:
        video.status = "failed"
        db.commit()
        raise HTTPException(500, f"Failed to compose video: {str(e)}")
