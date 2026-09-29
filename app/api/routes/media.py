"""
Uploads (browser -> storage directly), processing status, and media attached to modules/courses.

1. POST /media/uploads           -> an asset + where to upload it (signed PUT or resumable TUS).
2. The browser uploads the file straight to storage (never through this API).
3. POST /media/{id}/complete     -> checks the file arrived and queues its processing; with a
                                    purpose it then becomes a module's video/document or a cover.
"""

import re
import unicodedata
from dataclasses import asdict
from pathlib import Path
from typing import NamedTuple

import jwt
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_admin
from app.api.lookups import course_or_404, module_or_404
from app.db.base import new_uuid
from app.db.models import Job, MediaAsset, Module, User
from app.schemas.courses import CourseDetail, ModuleAdmin
from app.schemas.media import (
    PURPOSES_NEEDING_MODULE,
    VIDEO_PURPOSES,
    CoverUpdate,
    JobOut,
    MediaAssetOut,
    UploadComplete,
    UploadCreate,
    UploadCreated,
)
from app.services.course_views import course_detail, module_admin
from app.services.media import MediaResolver, discard_assets
from app.services.media_views import asset_out, assets_out, job_out
from app.services.storage import LocalStorage, StorageError, get_storage, read_local_token
from app.worker import queue

router = APIRouter(tags=["media"], dependencies=[Depends(require_admin)])
local_router = APIRouter(prefix="/media/local", tags=["media"])  # token-protected, development only

MB = 1024 * 1024


class Limit(NamedTuple):
    accepted: tuple[str, ...]  # MIME types, or prefixes such as "video/"
    max_bytes: int


LIMITS = {
    "video": Limit(("video/",), 2048 * MB),
    "recording": Limit(("video/webm", "video/mp4"), 2048 * MB),
    "document": Limit(
        (
            "application/pdf",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "text/plain",
            "text/markdown",
        ),
        100 * MB,
    ),
    "deck": Limit(("application/pdf",), 100 * MB),
    "image": Limit(("image/png", "image/jpeg", "image/webp"), 15 * MB),
    "audio": Limit(("audio/",), 200 * MB),
}
# The development upload endpoint only sees a token, not the kind, so it enforces the largest limit.
MAX_UPLOAD_BYTES = max(limit.max_bytes for limit in LIMITS.values())
PURPOSE_KINDS = {
    "module_video": {"video"},
    "recording": {"recording"},
    "module_document": {"document", "deck"},
    "course_cover": {"image"},
    "course_material": {"document", "deck", "image", "video"},
    "deck": {"deck"},
}


def _ascii_slug(text: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9_-]+", "-", ascii_text).strip("-")


def _safe_filename(filename: str) -> str:
    """ASCII storage name that keeps the extension ("图片.png" -> "archivo.png")."""
    stem, dot, extension = filename.rpartition(".")
    if not dot:
        stem, extension = filename, ""
    stem, extension = _ascii_slug(stem)[-100:] or "archivo", _ascii_slug(extension)[:10]
    return f"{stem}.{extension}" if extension else stem


def _asset_or_404(db: Session, asset_id: str) -> MediaAsset:
    asset = db.get(MediaAsset, asset_id)
    if not asset:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Archivo no encontrado")
    return asset


def _storage():
    try:
        return get_storage()
    except StorageError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc


def _checked_mime(payload: UploadCreate) -> str:
    """The upload's MIME type (without parameters), once its type and size are known to be allowed."""
    limit = LIMITS[payload.kind]
    mime = payload.mime_type.split(";")[0].strip().lower()
    # Entries ending in "/" accept a family (video/*); the rest are exact types.
    if not any(mime.startswith(accepted) if accepted.endswith("/") else mime == accepted for accepted in limit.accepted):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Este tipo de archivo no se admite aquí")
    if payload.size_bytes > limit.max_bytes:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE, f"El archivo supera el máximo de {limit.max_bytes // MB} MB para este tipo"
        )
    return mime


@router.post("/media/uploads", response_model=UploadCreated, status_code=status.HTTP_201_CREATED)
def create_upload(payload: UploadCreate, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    mime = _checked_mime(payload)
    if payload.course_id is not None:
        course_or_404(db, payload.course_id)

    storage = _storage()
    asset_id = new_uuid()
    folder = f"courses/{payload.course_id}" if payload.course_id else "library"
    asset = MediaAsset(
        id=asset_id,
        kind=payload.kind,
        status="pending",
        bucket=storage.bucket,
        path=f"{folder}/{asset_id}/{_safe_filename(payload.filename)}",
        mime_type=mime,
        size_bytes=payload.size_bytes,
        original_filename=payload.filename[:300],
        owner_id=admin.id,
        course_id=payload.course_id,
    )
    target = _upload_target(storage, asset)
    db.add(asset)
    db.commit()
    return UploadCreated(asset=asset_out(db, asset), upload=asdict(target))


def _upload_target(storage, asset: MediaAsset):
    try:
        return storage.upload_target(asset.path, asset.mime_type, asset.size_bytes or 0)
    except StorageError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


@router.post("/media/{asset_id}/upload-target", response_model=UploadCreated)
def renew_upload_target(asset_id: str, db: Session = Depends(get_db)):
    """A fresh signature for an upload still in progress (Supabase's expire after 2 hours)."""
    asset = _asset_or_404(db, asset_id)
    storage = _storage()
    # Arrived but not confirmed yet: a new signature can't replace it (no upsert); `complete` is what's missing.
    if asset.status != "pending" or storage.size(asset.path):
        raise HTTPException(status.HTTP_409_CONFLICT, "Este archivo ya se recibió; confirma la subida")
    return UploadCreated(asset=asset_out(db, asset), upload=asdict(_upload_target(storage, asset)))


def _target_module(db: Session, asset: MediaAsset, payload: UploadComplete) -> Module | None:
    """Check the asset can serve the requested purpose and return the module it is meant for, if any."""
    if payload.purpose and asset.kind not in PURPOSE_KINDS[payload.purpose]:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Este archivo no sirve para ese uso")
    if payload.purpose in PURPOSES_NEEDING_MODULE and payload.module_id is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Indica el módulo")
    if payload.module_id is None:
        return None
    module = module_or_404(db, payload.module_id)
    if asset.course_id not in (None, module.course_id):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "El archivo pertenece a otro curso")
    return module


@router.post("/media/{asset_id}/complete", response_model=MediaAssetOut)
def complete_upload(
    asset_id: str, payload: UploadComplete, db: Session = Depends(get_db), admin: User = Depends(require_admin)
):
    asset = _asset_or_404(db, asset_id)
    if asset.status not in ("pending", "failed"):  # a failed one can be processed again
        raise HTTPException(status.HTTP_409_CONFLICT, "Este archivo ya se recibió")
    module = _target_module(db, asset, payload)

    storage = _storage()
    size = storage.size(asset.path)
    if not size:
        raise HTTPException(status.HTTP_409_CONFLICT, "Todavía no recibimos el archivo; espera a que termine de subir")
    if size > LIMITS[asset.kind].max_bytes:  # the declared size is not proof: check what arrived
        storage.delete([asset.path])
        asset.status, asset.error = "failed", "El archivo supera el tamaño permitido"
        db.commit()
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "El archivo supera el tamaño permitido")
    asset.size_bytes, asset.status, asset.error = size, "uploaded", None
    if module is not None and payload.purpose in VIDEO_PURPOSES:
        # The editor shows the module's video as "processing" until the job attaches it.
        module.generation_status, module.generation_error = "queued", None
    queue.enqueue(
        db,
        "media.process",
        {"asset_id": asset.id, "purpose": payload.purpose, "module_id": payload.module_id},
        course_id=module.course_id if module else asset.course_id,
        module_id=payload.module_id,
        created_by=admin.id,
        dedupe_key=f"media:{asset.id}",
        commit=False,
    )
    db.commit()
    return asset_out(db, asset)


@router.get("/media/{asset_id}", response_model=MediaAssetOut)
def get_asset(asset_id: str, db: Session = Depends(get_db)):
    return asset_out(db, _asset_or_404(db, asset_id))


def _module_out(db: Session, module: Module) -> ModuleAdmin:
    return module_admin(module, MediaResolver(db).prepare(modules=[module]), module.evaluation)


@router.delete("/modules/{module_id}/video", response_model=ModuleAdmin)
def remove_module_video(module_id: int, db: Session = Depends(get_db)):
    module = module_or_404(db, module_id)
    removed = [module.video_asset_id, module.poster_asset_id, module.captions_asset_id]
    module.video_asset_id = module.poster_asset_id = module.captions_asset_id = None
    module.video_url, module.duration_seconds = "", None
    if module.generation_status in ("completed", "failed"):
        module.generation_status, module.generation_error = "pending", None
    db.flush()
    discard_assets(db, removed)
    return _module_out(db, module)


@router.delete("/modules/{module_id}/document", response_model=ModuleAdmin)
def remove_module_document(module_id: int, db: Session = Depends(get_db)):
    module = module_or_404(db, module_id)
    removed = [module.document_asset_id]
    module.document_asset_id = None
    db.flush()
    discard_assets(db, removed)
    return _module_out(db, module)


@router.put("/courses/{course_id}/cover", response_model=CourseDetail)
def set_cover(course_id: int, payload: CoverUpdate, db: Session = Depends(get_db)):
    course = course_or_404(db, course_id)
    asset = _asset_or_404(db, payload.asset_id)
    if asset.kind != "image" or asset.status != "ready":
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Elige una imagen ya procesada")
    if asset.course_id not in (None, course.id):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "La imagen pertenece a otro curso")
    replaced = course.cover_asset_id
    course.cover_asset_id = asset.id
    db.flush()
    if replaced != asset.id:
        discard_assets(db, [replaced])
    db.commit()
    return course_detail(db, course)


@router.get("/courses/{course_id}/materials", response_model=list[MediaAssetOut])
def course_materials(course_id: int, db: Session = Depends(get_db)):
    """Documents and decks uploaded for a course (the AI studio reads their text)."""
    course_or_404(db, course_id)
    assets = (
        db.query(MediaAsset)
        .filter(MediaAsset.course_id == course_id, MediaAsset.kind.in_(("document", "deck")))
        .order_by(MediaAsset.created_at, MediaAsset.id)
        .all()
    )
    return assets_out(db, assets)


# ── Jobs ──────────────────────────────────────────────────────────────


@router.get("/jobs", response_model=list[JobOut])
def list_jobs(
    course_id: int | None = None,
    module_id: int | None = None,
    active: bool = False,
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    query = db.query(Job)
    if course_id is not None:
        query = query.filter(Job.course_id == course_id)
    if module_id is not None:
        query = query.filter(Job.module_id == module_id)
    if active:
        query = query.filter(Job.status.in_(queue.ACTIVE))
    return [job_out(job) for job in query.order_by(Job.created_at.desc()).limit(limit).all()]


@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: str, db: Session = Depends(get_db)):
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Trabajo no encontrado")
    return job_out(job)


# ── Local storage (development and tests only) ────────────────────────


def _local_storage() -> LocalStorage:
    storage = _storage()
    if not isinstance(storage, LocalStorage):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No disponible")
    return storage


def _local_path(token: str, purpose: str) -> Path:
    storage = _local_storage()
    try:
        bucket, path = read_local_token(token, purpose)
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Enlace inválido o vencido") from exc
    if bucket != storage.bucket:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Enlace inválido")
    return storage.file_path(path)


@local_router.put("/upload/{token}", status_code=status.HTTP_200_OK)
async def local_upload(token: str, request: Request):
    target = _local_path(token, "upload")
    if target.exists():  # like Supabase without upsert: a checked upload can't be replaced
        raise HTTPException(status.HTTP_409_CONFLICT, "El archivo ya se subió")
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(f"{target.name}.part")  # never expose half an upload to `complete`
    written = 0
    with partial.open("wb") as fh:
        async for chunk in request.stream():
            written += len(chunk)
            if written > MAX_UPLOAD_BYTES:
                break
            fh.write(chunk)
    if written > MAX_UPLOAD_BYTES:
        partial.unlink(missing_ok=True)
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "Archivo demasiado grande")
    partial.replace(target)
    return Response(status_code=status.HTTP_200_OK)


@local_router.get("/file/{token}")
def local_file(token: str):
    target = _local_path(token, "download")
    if not target.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Archivo no encontrado")
    return FileResponse(target)
