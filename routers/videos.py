"""
User Videos Router — Manual course creation video uploads

Endpoints:
- POST /api/v1/videos/upload → Upload recorded video to Supabase Storage
- GET  /api/v1/videos/{video_id} → Get video metadata
- GET  /api/v1/videos → List videos by user or module
"""

import os
import uuid
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from auth import get_current_user
from database import get_db
from models import User, UserVideo
from schemas import UserVideoOut, VideoUploadResponse

router = APIRouter(prefix="/videos", tags=["videos"])

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
    current_user: User = Depends(get_current_user),
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

    # Save metadata to DB
    video = UserVideo(
        id=video_id,
        module_id=module_id,
        user_id=current_user.id,
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
