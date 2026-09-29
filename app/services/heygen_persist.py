"""
Copy HeyGen videos into Supabase Storage.

HeyGen retires its v1/v2 API on 2026-10-31. Modules still store
`heygen://video/{id}` / `heygen://pending/{id}` and resolve a fresh (expiring)
HeyGen URL on every request through v1, so they would stop playing that day.
This module downloads each finished video once and points `Module.video_url`
at a permanent public Supabase Storage URL.

Run manually with:  python -m app.services.heygen_persist
"""

import logging
import os
import re
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional

import httpx
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.db.models import Module
from app.db.session import SessionLocal

logger = logging.getLogger(__name__)

HEYGEN_STATUS_URL = "https://api.heygen.com/v1/video_status.get"
BUCKET = "course-videos"
PENDING_PREFIX = "heygen://pending/"
VIDEO_PREFIX = "heygen://video/"

# Supabase's default global upload limit; bigger videos are re-encoded to fit.
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024 * 1024
STATUS_TIMEOUT_S = 30
DOWNLOAD_TIMEOUT_S = 300
UPLOAD_TIMEOUT_S = 600
FFMPEG_TIMEOUT_S = 1800
SWEEP_INTERVAL_S = 30 * 60
MAX_SWEEPS = 48  # one day of retries per process; every deploy starts a new series

_VIDEO_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")

Outcome = Literal["persisted", "processing", "failed", "unavailable", "superseded", "skipped", "error"]


@dataclass(frozen=True)
class PersistConfig:
    heygen_key: str = field(repr=False)
    supabase_url: str
    supabase_key: str = field(repr=False)

    @classmethod
    def from_env(cls) -> Optional["PersistConfig"]:
        config = cls(
            heygen_key=os.getenv("HEYGEN_API_KEY", ""),
            supabase_url=os.getenv("SUPABASE_URL", "").rstrip("/"),
            supabase_key=os.getenv("SUPABASE_SERVICE_KEY", os.getenv("SUPABASE_KEY", "")),
        )
        return config if all((config.heygen_key, config.supabase_url, config.supabase_key)) else None

    @property
    def storage_headers(self) -> dict:
        return {"Authorization": f"Bearer {self.supabase_key}", "apikey": self.supabase_key}

    def public_url(self, object_path: str) -> str:
        return f"{self.supabase_url}/storage/v1/object/public/{BUCKET}/{object_path}"


class VideoTooLarge(RuntimeError):
    pass


def heygen_video_id(video_url: Optional[str]) -> Optional[str]:
    for prefix in (VIDEO_PREFIX, PENDING_PREFIX):
        if video_url and video_url.startswith(prefix):
            video_id = video_url[len(prefix):]
            return video_id if _VIDEO_ID_RE.fullmatch(video_id) else None
    return None


def _error_fields(exc: Exception) -> dict:
    """Loggable description of an error without secrets (presigned URLs lose their query string)."""
    fields: dict = {"error_type": type(exc).__name__}
    if isinstance(exc, httpx.HTTPStatusError):
        fields.update(http_status=exc.response.status_code, url=str(exc.request.url.copy_with(query=None)))
    elif isinstance(exc, httpx.RequestError):
        fields.update(url=str(exc.request.url.copy_with(query=None)))
    elif isinstance(exc, subprocess.CalledProcessError):
        fields.update(returncode=exc.returncode, stderr=(exc.stderr or b"")[-800:].decode(errors="replace"))
    else:
        fields["error"] = str(exc)[:500]
    return fields


def _ensure_public_bucket(client: httpx.Client, config: PersistConfig) -> None:
    # Creating an existing bucket returns an error we can ignore; what matters is that it is public.
    client.post(
        f"{config.supabase_url}/storage/v1/bucket",
        headers=config.storage_headers,
        json={"id": BUCKET, "name": BUCKET, "public": True},
    )
    response = client.get(f"{config.supabase_url}/storage/v1/bucket/{BUCKET}", headers=config.storage_headers)
    response.raise_for_status()
    if not response.json().get("public"):
        # Never flip an existing bucket to public: it may hold other content.
        raise RuntimeError(f"bucket '{BUCKET}' exists but is private")


def _public_size(client: httpx.Client, url: str) -> Optional[int]:
    response = client.head(url, timeout=STATUS_TIMEOUT_S)
    length = response.headers.get("content-length", "")
    return int(length) if response.status_code == 200 and length.isdigit() else None


def _download(client: httpx.Client, url: str, dest: Path) -> None:
    written = 0
    with client.stream("GET", url, follow_redirects=True, timeout=DOWNLOAD_TIMEOUT_S) as response:
        response.raise_for_status()
        with dest.open("wb") as fh:
            for chunk in response.iter_bytes(1024 * 1024):
                written += len(chunk)
                if written > MAX_DOWNLOAD_BYTES:
                    raise VideoTooLarge(f"download exceeded {MAX_DOWNLOAD_BYTES} bytes")
                fh.write(chunk)


def _upload(client: httpx.Client, config: PersistConfig, object_path: str, file_path: Path) -> httpx.Response:
    with file_path.open("rb") as fh:
        return client.post(
            f"{config.supabase_url}/storage/v1/object/{BUCKET}/{object_path}",
            headers={
                **config.storage_headers,
                "Content-Type": "video/mp4",
                "x-upsert": "true",
                "cache-control": "max-age=31536000",
            },
            content=fh,
            timeout=UPLOAD_TIMEOUT_S,
        )


def _is_size_rejection(response: httpx.Response) -> bool:
    body = response.text.lower()
    return response.status_code == 413 or "payload too large" in body or "maximum allowed size" in body


def _compress(src: Path, dest: Path, max_bytes: int = MAX_UPLOAD_BYTES) -> None:
    """Re-encode to 720p H.264/AAC with a bitrate cap aimed at `max_bytes`."""
    duration_s = float(
        subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(src)],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        ).stdout.strip()
    )
    video_kbps = max(int(max_bytes * 8 * 0.9 / max(duration_s, 1) / 1000) - 96, 250)
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-i", str(src),
            "-vf", "scale=-2:'min(720,ih)'",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
            "-maxrate", f"{video_kbps}k", "-bufsize", f"{2 * video_kbps}k", "-threads", "2",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "96k",
            "-movflags", "+faststart",
            str(dest),
        ],
        check=True,
        capture_output=True,
        timeout=FFMPEG_TIMEOUT_S,
    )


def _store(client: httpx.Client, config: PersistConfig, object_path: str, source_url: str) -> None:
    """Download from HeyGen and upload to Storage, re-encoding when the file would not fit."""
    with tempfile.TemporaryDirectory() as tmp:
        original = Path(tmp) / "original.mp4"
        compressed = Path(tmp) / "compressed.mp4"
        _download(client, source_url, original)

        upload_file = original
        if original.stat().st_size > MAX_UPLOAD_BYTES:
            _compress(original, compressed)
            upload_file = compressed
        response = _upload(client, config, object_path, upload_file)
        if response.is_error and _is_size_rejection(response) and upload_file is original:
            _compress(original, compressed)
            upload_file = compressed
            response = _upload(client, config, object_path, upload_file)
        if response.is_error and _is_size_rejection(response):
            raise VideoTooLarge(f"{upload_file.stat().st_size} bytes still exceed the upload limit")
        response.raise_for_status()

        # Only swap the reference once the public URL really serves the file.
        expected = upload_file.stat().st_size
        size = _public_size(client, config.public_url(object_path))
        if size != expected:
            raise RuntimeError(f"public object reports {size} bytes, expected {expected}")


def _update_if_unchanged(db: Session, module_id: int, video_id: str, **values) -> bool:
    """Write only if the module still points at THIS HeyGen video (an admin may have regenerated it)."""
    result = db.execute(
        update(Module)
        .where(
            Module.id == module_id,
            Module.video_url.in_([PENDING_PREFIX + video_id, VIDEO_PREFIX + video_id]),
        )
        .values(**values)
    )
    db.commit()
    return result.rowcount == 1


def persist_module_video(
    db: Session, module_id: int, video_url: str, client: httpx.Client, config: PersistConfig
) -> Outcome:
    log_ctx: dict = {"module_id": module_id}
    try:
        video_id = heygen_video_id(video_url)
        if not video_id:
            logger.warning("heygen_reference_invalid", extra=log_ctx)
            return "skipped"
        log_ctx["heygen_video_id"] = video_id
        object_path = f"modules/{module_id}/{video_id}.mp4"
        public_url = config.public_url(object_path)

        # A previous pass (or an overlapping deploy) may already have copied it.
        if not _public_size(client, public_url):
            response = client.get(
                HEYGEN_STATUS_URL,
                params={"video_id": video_id},
                headers={"X-Api-Key": config.heygen_key},
                timeout=STATUS_TIMEOUT_S,
            )
            response.raise_for_status()
            body = response.json()
            data = body.get("data")
            if not data:
                logger.warning(
                    "heygen_video_unavailable",
                    extra={**log_ctx, "code": body.get("code"), "heygen_message": body.get("message")},
                )
                return "unavailable"

            status = data.get("status")
            if status == "failed":
                logger.warning("heygen_video_failed", extra={**log_ctx, "heygen_error": data.get("error")})
                # Only a pending render is abandoned; a stored reference keeps its id for manual follow-up.
                if video_url.startswith(PENDING_PREFIX):
                    _update_if_unchanged(db, module_id, video_id, video_url="", generation_status="failed")
                return "failed"
            if status != "completed" or not data.get("video_url"):
                logger.info("heygen_video_not_ready", extra={**log_ctx, "heygen_status": status})
                return "processing"

            _store(client, config, object_path, data["video_url"])

        if not _update_if_unchanged(db, module_id, video_id, video_url=public_url, generation_status="completed"):
            logger.warning("heygen_video_superseded", extra=log_ctx)
            return "superseded"
        logger.info("heygen_video_persisted", extra={**log_ctx, "storage_url": public_url})
        return "persisted"
    except Exception as exc:  # keep going with the other modules; the next pass retries this one
        db.rollback()
        logger.error("heygen_video_persist_error", extra={**log_ctx, **_error_fields(exc)})
        return "error"


def persist_all_heygen_videos(
    db: Session,
    config: Optional[PersistConfig] = None,
    client: Optional[httpx.Client] = None,
) -> dict[str, int]:
    """Persist every module that still points at HeyGen. Idempotent: persisted modules are skipped."""
    config = config or PersistConfig.from_env()
    if config is None:
        logger.warning("heygen_persist_skipped", extra={"reason": "HEYGEN_API_KEY or Supabase storage not configured"})
        return {}

    rows = db.execute(
        select(Module.id, Module.video_url).where(Module.video_url.like("heygen://%")).order_by(Module.id)
    ).all()
    db.rollback()  # end the read transaction before any network I/O
    outcomes: dict[str, int] = {}
    if not rows:
        return outcomes

    owns_client = client is None
    client = client or httpx.Client()
    try:
        _ensure_public_bucket(client, config)
        for module_id, video_url in rows:
            outcome = persist_module_video(db, module_id, video_url, client, config)
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
    except Exception as exc:
        logger.error("heygen_persist_aborted", extra=_error_fields(exc))
    finally:
        if owns_client:
            client.close()

    logger.info("heygen_persist_finished", extra={"modules": len(rows), "outcomes": outcomes})
    return outcomes


_run_lock = threading.Lock()
_rerun_requested = threading.Event()


def _run() -> None:
    while True:
        if not _run_lock.acquire(blocking=False):
            # A pass is in progress: ask it for one more so modules that just finished are not missed.
            _rerun_requested.set()
            return
        try:
            _rerun_requested.clear()
            with SessionLocal() as db:
                persist_all_heygen_videos(db)
        except Exception as exc:
            logger.error("heygen_persist_crashed", extra=_error_fields(exc), exc_info=True)
        finally:
            _run_lock.release()
        if not _rerun_requested.is_set():
            return


def start_background_persist() -> None:
    """Run `persist_all_heygen_videos` in a daemon thread without blocking the caller."""
    threading.Thread(target=_run, name="heygen-persist", daemon=True).start()


def _sweep() -> None:
    # Retries transient failures and renders that finish without anyone polling their status.
    # Once every module is migrated each pass is a single empty SELECT.
    for _ in range(MAX_SWEEPS):
        _run()
        time.sleep(SWEEP_INTERVAL_S)


def start_background_sweeps() -> None:
    """Persist now, then again every SWEEP_INTERVAL_S (bounded), in a daemon thread."""
    threading.Thread(target=_sweep, name="heygen-persist-sweeps", daemon=True).start()


if __name__ == "__main__":
    from app.core.logging import configure_logging

    configure_logging()
    with SessionLocal() as db:
        persist_all_heygen_videos(db)
