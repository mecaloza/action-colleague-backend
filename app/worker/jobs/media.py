"""
media.process: turn an uploaded file into something the platform can use, then attach it.

- video / recording: web MP4 (H.264/AAC, max 1080p, aspect ratio preserved) + poster.
- document: extracted text (for AI features and search).
- deck: one image per PDF page (recording studio) + extracted text.
- image: resized JPEG.
Then, depending on the purpose, it becomes a module's video or document, or a course's cover.
A module's new video also gets its captions queued.
"""

import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from sqlalchemy.orm import Session

from app.db.models import Course, Job, MediaAsset, Module
from app.schemas.media import PURPOSES_NEEDING_MODULE, VIDEO_PURPOSES
from app.services import media_processing as mp
from app.services.media import discard_assets
from app.services.storage import StorageError, get_storage
from app.worker.jobs.recording import LEGACY_VIDEO, enqueue_captions
from app.worker.runner import JobCancelled, JobContext, JobError, handler, holds_job, open_session

logger = logging.getLogger(__name__)

POSTER_AT_SECONDS = 1.0  # the poster frame is taken here, or a third of the way in for shorter videos


@dataclass(frozen=True)
class AssetSnapshot:
    """The asset columns the processors need, read up front so no DB session stays open during FFmpeg."""

    kind: str
    path: str
    mime_type: str
    owner_id: int | None
    course_id: int | None
    original_filename: str | None

    @classmethod
    def of(cls, asset: MediaAsset) -> "AssetSnapshot":
        return cls(
            kind=asset.kind,
            path=asset.path,
            mime_type=asset.mime_type,
            owner_id=asset.owner_id,
            course_id=asset.course_id,
            original_filename=asset.original_filename,
        )

    @property
    def folder(self) -> str:
        """Processed files are stored next to the upload."""
        return str(PurePosixPath(self.path).parent / "derived")


def _save_poster(asset: AssetSnapshot, video: Path, work: Path, at_seconds: float) -> str:
    """Grab a frame, store it as an image asset of its own and return that asset's id."""
    poster_file = work / "poster.jpg"
    mp.poster(video, poster_file, at_seconds=at_seconds)
    storage = get_storage()
    poster_path = f"{asset.folder}/poster.jpg"
    storage.upload_file(poster_path, poster_file, "image/jpeg")
    width, height = mp.image_size(poster_file)
    with open_session() as db:
        # A retry reuses the poster row written by a previous attempt instead of orphaning it.
        poster = db.query(MediaAsset).filter(MediaAsset.path == poster_path).first() or MediaAsset(
            kind="image", status="ready", bucket=storage.bucket, path=poster_path, mime_type="image/jpeg",
            owner_id=asset.owner_id, course_id=asset.course_id, original_filename="poster.jpg",
        )
        poster.size_bytes, poster.width, poster.height = poster_file.stat().st_size, width, height
        db.add(poster)
        db.commit()
        return poster.id


def _process_video(ctx: JobContext, asset: AssetSnapshot, source: Path, work: Path) -> dict:
    served_as_is = asset.mime_type == "video/mp4"  # only then does the upload's own duration matter
    info = mp.probe(source, need_duration=served_as_is)
    ctx.progress(20, "Revisando el video")
    video, path, mime = source, asset.path, asset.mime_type
    if not (served_as_is and info.is_web_ready):
        ctx.progress(30, "Optimizando el video para la web")
        video = work / "video.mp4"
        mp.to_web_mp4(source, video, info)
        info = mp.probe(video)
        path, mime = f"{asset.folder}/video.mp4", "video/mp4"
        get_storage().upload_file(path, video, mime)
    ctx.progress(80, "Creando la portada")
    poster_id = _save_poster(asset, video, work, at_seconds=min(POSTER_AT_SECONDS, info.duration / 3))
    return {
        "path": path,
        "mime_type": mime,
        "size_bytes": video.stat().st_size,
        "duration_seconds": round(info.duration, 2),
        "width": info.width,
        "height": info.height,
        "meta": {"poster_asset_id": poster_id},
    }


def _text_meta(text: str) -> dict:
    return {"text": text, "text_chars": len(text)}


def _process_document(ctx: JobContext, asset: AssetSnapshot, source: Path, work: Path) -> dict:
    ctx.progress(40, "Leyendo el documento")
    text = mp.extract_text_safely(source, asset.mime_type, asset.original_filename or "")
    return {"meta": _text_meta(text)}


def _process_deck(ctx: JobContext, asset: AssetSnapshot, source: Path, work: Path) -> dict:
    ctx.progress(30, "Preparando las diapositivas")
    pages_dir = work / "pages"
    pages_dir.mkdir()
    page_files = mp.render_pdf_pages(source, pages_dir)
    storage = get_storage()
    page_paths = []
    for number, page_file in enumerate(page_files, start=1):
        path = f"{asset.folder}/pages/{page_file.name}"
        storage.upload_file(path, page_file, "image/png")
        page_paths.append(path)
        ctx.progress(30 + int(60 * number / len(page_files)), f"Diapositiva {number} de {len(page_files)}")
    text = mp.extract_text_safely(source, "application/pdf", asset.original_filename or "slides.pdf")
    return {"meta": {"pages": page_paths, **_text_meta(text)}}


def _process_image(ctx: JobContext, asset: AssetSnapshot, source: Path, work: Path) -> dict:
    image = work / "image.jpg"
    width, height = mp.normalize_image(source, image)
    path = f"{asset.folder}/image.jpg"
    get_storage().upload_file(path, image, "image/jpeg")
    return {"path": path, "mime_type": "image/jpeg", "size_bytes": image.stat().st_size, "width": width, "height": height}


PROCESSORS = {
    "video": _process_video,
    "recording": _process_video,
    "document": _process_document,
    "deck": _process_deck,
    "image": _process_image,
}


def _attach_video(module: Module, asset: MediaAsset, source: str) -> list[str | None]:
    replaced = [module.video_asset_id, module.poster_asset_id, module.captions_asset_id]
    module.video_asset_id = asset.id
    module.poster_asset_id = (asset.meta or {}).get("poster_asset_id")
    module.captions_asset_id = None
    module.duration_seconds = asset.duration_seconds
    module.video_url = ""  # a previous app's video no longer applies
    module.source = source
    module.generation_status = "completed"
    module.generation_error = None
    return replaced


def _attach_document(module: Module, asset: MediaAsset) -> list[str | None]:
    replaced = [module.document_asset_id]
    module.document_asset_id = asset.id
    if not module.video_asset_id and not module.video_url:
        module.source = "document"
    return replaced


def _attach(db: Session, asset: MediaAsset, purpose: str | None, module_id: int | None) -> list[str | None] | None:
    """
    Make the processed asset a module's video or document, or a course's cover. Returns what it
    replaced, or None when that module or course was deleted meanwhile. The row is locked: two uploads
    for one module finishing together must each see what the other attached.
    """
    if purpose in PURPOSES_NEEDING_MODULE or purpose == LEGACY_VIDEO:
        module = db.get(Module, module_id, with_for_update=True) if module_id else None
        if module is None:
            return None
        if purpose == "module_document":
            return _attach_document(module, asset)
        # A migrated video keeps the module's source (it was made with AI or uploaded before).
        replaced = _attach_video(module, asset, VIDEO_PURPOSES.get(purpose) or module.source)
        enqueue_captions(db, module)
        return replaced
    if purpose == "course_cover" and asset.course_id:
        course = db.get(Course, asset.course_id, with_for_update=True)
        if course is None:
            return None
        replaced = [course.cover_asset_id]
        course.cover_asset_id = asset.id
        return replaced
    return []


def _media_failed(db: Session, job: Job, error: str) -> None:
    """The job gave up: show the failure on the asset and, for a module's video, on the module."""
    payload = job.payload or {}
    asset = db.get(MediaAsset, payload.get("asset_id"))
    if asset:
        asset.status, asset.error = "failed", error
    module_id, purpose = payload.get("module_id"), payload.get("purpose")
    module = db.get(Module, module_id) if module_id else None
    if module and purpose in VIDEO_PURPOSES:
        module.generation_status, module.generation_error = "failed", error
    elif module and purpose == LEGACY_VIDEO:
        module.generation_status = "completed"  # its previous video still plays from where it was


def _start_processing(ctx: JobContext) -> AssetSnapshot | None:
    """Mark the asset as processing, and the module waiting for it as generating; None if the asset was deleted."""
    payload = ctx.payload
    with open_session() as db:
        asset = db.get(MediaAsset, payload["asset_id"])
        if asset is None or asset.status == "ready":  # deleted, or a retry after the result was saved
            return None
        asset.status = "processing"
        module = db.get(Module, payload["module_id"]) if payload.get("module_id") else None
        if module is not None and payload.get("purpose") in VIDEO_PURPOSES and module.generation_status == "queued":
            module.generation_status = "generating"  # the editor shows "Procesando" instead of "En cola"
        snapshot = AssetSnapshot.of(asset)
        db.commit()
        return snapshot


def _process_file(ctx: JobContext, asset: AssetSnapshot) -> dict:
    """Download the upload to a scratch folder and run the processor for its kind. Returns what changed."""
    processor = PROCESSORS.get(asset.kind)
    if processor is None:
        raise JobError(f"No se procesan archivos de tipo {asset.kind}", permanent=True)
    with tempfile.TemporaryDirectory(prefix="media-") as tmp:
        work = Path(tmp)
        source = work / "source"
        ctx.progress(5, "Descargando el archivo")
        get_storage().download_file(asset.path, source)
        try:
            return processor(ctx, asset, source, work)
        except mp.MediaError as exc:
            raise JobError(str(exc), permanent=True) from exc


def _delete_files(paths: list[str], asset_id: str) -> None:
    try:
        get_storage().delete(paths)
    except StorageError as exc:  # an orphaned file costs little; the job did its work
        logger.warning("media_cleanup_failed", extra={"asset_id": asset_id, "error": str(exc)[:300]})


def _finish_processing(ctx: JobContext, snapshot: AssetSnapshot, changes: dict) -> dict:
    """
    Store the outcome, mark the asset ready and attach it, only while this worker still holds the job
    (after a shutdown hand-back another worker owns the result). Then discard what it replaced, or
    everything it made when the asset, or the module or course it was for, was deleted meanwhile.
    """
    asset_id, purpose = ctx.payload["asset_id"], ctx.payload.get("purpose")
    new_meta = changes.get("meta", {})
    with open_session() as db:
        if not holds_job(db, ctx):
            raise JobCancelled(ctx.job_id)
        asset = db.get(MediaAsset, asset_id)
        if asset is None:
            db.rollback()
            derived = [changes["path"]] if changes.get("path", snapshot.path) != snapshot.path else []
            _delete_files([*derived, *new_meta.get("pages", [])], asset_id)
            discard_assets(db, [new_meta.get("poster_asset_id")])
            return {"skipped": "asset deleted"}
        for column, value in changes.items():
            if column != "meta":
                setattr(asset, column, value)
        asset.meta, asset.status, asset.error = {**(asset.meta or {}), **new_meta}, "ready", None
        replaced = _attach(db, asset, purpose, ctx.payload.get("module_id"))
        db.commit()
        logger.info("media_processed", extra={"asset_id": asset_id, "kind": asset.kind, "purpose": purpose})
        orphaned = replaced is None
        leftovers = [asset_id, new_meta.get("poster_asset_id")] if orphaned else [i for i in replaced if i != asset_id]
        try:
            discard_assets(db, leftovers)
        except Exception:  # the new media is in place; leftovers only cost storage
            db.rollback()
            logger.exception("media_replaced_cleanup_failed", extra={"asset_id": asset_id})
        return {"skipped": "module or course deleted"} if orphaned else {"asset_id": asset_id}


@handler("media.process", on_failure=_media_failed)
def process_media(ctx: JobContext) -> dict:
    asset_id = ctx.payload["asset_id"]
    snapshot = _start_processing(ctx)
    if snapshot is None:
        return {"skipped": "asset deleted or already processed"}
    changes = _process_file(ctx, snapshot)
    result = _finish_processing(ctx, snapshot, changes)
    if changes.get("path", snapshot.path) != snapshot.path:
        # Only now that the database points at the processed copy: a retry before this still finds the upload.
        _delete_files([snapshot.path], asset_id)
    return result
