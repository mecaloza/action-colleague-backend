"""
Object storage for course media.

- SupabaseStorage: a private bucket. Browsers upload directly with signed upload tokens (PUT for
  small files, resumable TUS for big ones) and play media through short-lived signed URLs.
- LocalStorage: files on disk for development and tests, served by /media/local routes with
  signed tokens, so the same flows work without Supabase.
"""

import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Protocol
from urllib.parse import parse_qs, quote, urlparse

import httpx
import jwt

from app.core.config import get_settings
from app.core.security import ALGORITHM

SMALL_UPLOAD_MAX_BYTES = 6 * 1024 * 1024  # Supabase recommends resumable uploads above 6 MB
TUS_CHUNK_BYTES = 6 * 1024 * 1024  # and requires exactly 6 MB chunks for them
REQUEST_TIMEOUT_SECONDS = 15  # API calls made while a request waits (transfers have their own)
TRANSFER_TIMEOUT_SECONDS = 900  # the worker moving a whole file (a video can be gigabytes)
DOWNLOAD_CHUNK_BYTES = 1024 * 1024
LOCAL_UPLOAD_LINK_SECONDS = 2 * 3600


@dataclass
class UploadTarget:
    """How the browser must upload one object."""

    method: str  # "PUT" | "TUS"
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, str] = field(default_factory=dict)  # TUS metadata
    chunk_size: int | None = None


class Storage(Protocol):
    bucket: str

    def upload_target(self, path: str, content_type: str, size_bytes: int) -> UploadTarget: ...
    def upload_file(self, path: str, file_path: Path, content_type: str) -> None: ...
    def download_file(self, path: str, dest: Path) -> None: ...
    def size(self, path: str) -> int | None: ...
    def signed_urls(self, paths: list[str], expires_in: int) -> dict[str, str]: ...
    def delete(self, paths: list[str]) -> None: ...


class StorageError(RuntimeError):
    pass


class SupabaseStorage:
    def __init__(self, url: str, key: str, bucket: str, client: httpx.Client | None = None):
        self.url = url.rstrip("/")
        self.base = f"{self.url}/storage/v1"
        self.bucket = bucket
        self.key = key
        self.client = client or httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS)
        self._bucket_ready = False

    @property
    def headers(self) -> dict[str, str]:
        headers = {"apikey": self.key}
        if self.key.startswith("eyJ"):  # legacy service_role JWT; new sb_secret_ keys only go in apikey
            headers["Authorization"] = f"Bearer {self.key}"
        return headers

    def _object(self, path: str) -> str:
        return f"{self.bucket}/{quote(path)}"

    def _check(self, response: httpx.Response, action: str) -> httpx.Response:
        if response.is_error:
            raise StorageError(f"Storage no pudo {action} ({response.status_code})")
        return response

    def _send(self, method: str, url: str, action: str, **kwargs) -> httpx.Response:
        """One request whose network failures surface as StorageError, like its HTTP errors."""
        try:
            return self._check(self.client.request(method, url, **kwargs), action)
        except httpx.HTTPError as exc:
            raise StorageError(f"Storage no pudo {action} (sin conexión)") from exc

    def ensure_bucket(self) -> None:
        """Create the private bucket on first use (an existing bucket is left as it is)."""
        if self._bucket_ready:
            return
        self.client.post(
            f"{self.base}/bucket", headers=self.headers, json={"id": self.bucket, "name": self.bucket, "public": False}
        )
        bucket = self._check(self.client.get(f"{self.base}/bucket/{self.bucket}", headers=self.headers), "leer el bucket")
        if bucket.json().get("public"):
            raise StorageError(f"El bucket {self.bucket} es público; los medios de los cursos deben ser privados")
        self._bucket_ready = True

    def _sign_upload(self, path: str) -> tuple[str, str]:
        """(relative upload URL, upload token) that let the browser create exactly this object, once.

        No upsert: once the object exists (and `complete` checked it) the token can't replace it.
        """
        signed = self._check(
            self.client.post(f"{self.base}/object/upload/sign/{self._object(path)}", headers=self.headers),
            "firmar la subida",
        ).json()
        relative_url = signed["url"]  # "/object/upload/sign/{bucket}/{path}?token=..."
        token = signed.get("token") or parse_qs(urlparse(relative_url).query)["token"][0]
        return relative_url, token

    def _tus_endpoint(self) -> str:
        """Signed resumable uploads live under /upload/resumable/sign (plain /upload/resumable wants a user JWT)."""
        host = urlparse(self.url).hostname or ""
        if host.endswith(".supabase.co"):  # the direct storage hostname is faster for big files
            return f"https://{host.split('.')[0]}.storage.supabase.co/storage/v1/upload/resumable/sign"
        return f"{self.base}/upload/resumable/sign"  # local Supabase CLI, self-hosted or a custom domain

    def upload_target(self, path: str, content_type: str, size_bytes: int) -> UploadTarget:
        self.ensure_bucket()
        relative_url, token = self._sign_upload(path)
        if size_bytes <= SMALL_UPLOAD_MAX_BYTES:
            return UploadTarget(method="PUT", url=f"{self.base}{relative_url}", headers={"content-type": content_type})
        return UploadTarget(
            method="TUS",
            url=self._tus_endpoint(),
            headers={"x-signature": token},
            metadata={"bucketName": self.bucket, "objectName": path, "contentType": content_type, "cacheControl": "3600"},
            chunk_size=TUS_CHUNK_BYTES,
        )

    def upload_file(self, path: str, file_path: Path, content_type: str) -> None:
        self.ensure_bucket()
        with file_path.open("rb") as fh:
            self._send(
                "POST", f"{self.base}/object/{self._object(path)}", "subir el archivo",
                headers={**self.headers, "content-type": content_type, "x-upsert": "true", "cache-control": "max-age=31536000"},
                content=fh,
                timeout=TRANSFER_TIMEOUT_SECONDS,
            )

    def download_file(self, path: str, dest: Path) -> None:
        url = f"{self.base}/object/authenticated/{self._object(path)}"
        try:
            with self.client.stream("GET", url, headers=self.headers, timeout=TRANSFER_TIMEOUT_SECONDS) as response:
                self._check(response, "descargar el archivo")
                with dest.open("wb") as fh:
                    for chunk in response.iter_bytes(DOWNLOAD_CHUNK_BYTES):
                        fh.write(chunk)
        except httpx.HTTPError as exc:  # a dropped connection mid-file, like an HTTP error, is the storage's
            raise StorageError("Storage no pudo descargar el archivo (sin conexión)") from exc

    def size(self, path: str) -> int | None:
        try:
            response = self.client.head(f"{self.base}/object/authenticated/{self._object(path)}", headers=self.headers)
        except httpx.HTTPError as exc:
            raise StorageError("Storage no pudo revisar el archivo (sin conexión)") from exc
        length = response.headers.get("content-length", "")
        return int(length) if response.status_code == 200 and length.isdigit() else None

    def signed_urls(self, paths: list[str], expires_in: int) -> dict[str, str]:
        if not paths:
            return {}
        response = self._send(
            "POST", f"{self.base}/object/sign/{self.bucket}", "firmar los enlaces",
            headers=self.headers, json={"expiresIn": expires_in, "paths": paths},
        )
        return {
            item["path"]: f"{self.base}{item['signedURL']}"
            for item in response.json()
            if item.get("signedURL") and not item.get("error")
        }

    def delete(self, paths: list[str]) -> None:
        if paths:  # a timeout must reach callers as StorageError too: they log it after committing
            self._send(
                "DELETE", f"{self.base}/object/{self.bucket}", "borrar archivos",
                headers=self.headers, json={"prefixes": paths},
            )


# ── Local storage (development and tests) ─────────────────────────────


def _local_token(bucket: str, path: str, purpose: str, expires_in: int) -> str:
    claims = {
        "bucket": bucket,
        "path": path,
        "purpose": purpose,
        "exp": datetime.now(timezone.utc) + timedelta(seconds=expires_in),
    }
    return jwt.encode(claims, get_settings().jwt_secret, algorithm=ALGORITHM)


def read_local_token(token: str, purpose: str) -> tuple[str, str]:
    """(bucket, path) of a local-storage token. Raises `jwt.InvalidTokenError` if invalid or expired."""
    claims = jwt.decode(token, get_settings().jwt_secret, algorithms=[ALGORITHM])
    if claims.get("purpose") != purpose:
        raise jwt.InvalidTokenError("wrong purpose")
    return claims["bucket"], claims["path"]


class LocalStorage:
    def __init__(self, root: Path, bucket: str, public_api_url: str):
        self.root = root
        self.bucket = bucket
        self.public_api_url = public_api_url.rstrip("/")

    def file_path(self, path: str) -> Path:
        base = (self.root / self.bucket).resolve()
        full = (base / path).resolve()
        if base not in full.parents:
            raise StorageError("Ruta inválida")
        return full

    def upload_target(self, path: str, content_type: str, size_bytes: int) -> UploadTarget:
        token = _local_token(self.bucket, path, "upload", LOCAL_UPLOAD_LINK_SECONDS)
        return UploadTarget(method="PUT", url=f"{self.public_api_url}/media/local/upload/{token}",
                            headers={"content-type": content_type})

    def upload_file(self, path: str, file_path: Path, content_type: str) -> None:
        dest = self.file_path(path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(file_path, dest)

    def download_file(self, path: str, dest: Path) -> None:
        source = self.file_path(path)
        if not source.exists():
            raise StorageError("El archivo no existe")
        shutil.copyfile(source, dest)

    def size(self, path: str) -> int | None:
        target = self.file_path(path)
        return target.stat().st_size if target.exists() else None

    def signed_urls(self, paths: list[str], expires_in: int) -> dict[str, str]:
        return {
            path: f"{self.public_api_url}/media/local/file/{_local_token(self.bucket, path, 'download', expires_in)}"
            for path in paths
            if self.file_path(path).exists()
        }

    def delete(self, paths: list[str]) -> None:
        for path in paths:
            self.file_path(path).unlink(missing_ok=True)


@lru_cache
def get_storage() -> Storage:
    settings = get_settings()
    use_supabase = settings.storage_backend == "supabase" or (
        settings.storage_backend == "auto" and settings.supabase_url and settings.supabase_service_key
    )
    if use_supabase:
        return SupabaseStorage(settings.supabase_url, settings.supabase_service_key, settings.media_bucket)
    if settings.is_deployed:
        # A container's disk is wiped on every deploy: never keep course media there.
        raise StorageError("Configura SUPABASE_URL y SUPABASE_SERVICE_KEY para guardar los medios")
    return LocalStorage(Path(settings.local_storage_dir), settings.media_bucket, settings.public_api_url)
