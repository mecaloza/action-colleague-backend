from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.api.routes import (
    auth,
    courses,
    dashboard,
    learn,
    media,
    participants,
    studio,
    users,
)
from app.core.config import get_settings
from app.core.logging import RequestLogMiddleware, configure_logging
from app.db.migrate import run_migrations
from app.db.session import SessionLocal
from app.services.heygen_persist import start_background_sweeps
from app.worker import queue
from app.worker.runner import WorkerPool

API_PREFIX = "/api/v1"
ROUTERS = (
    auth,
    courses,
    participants,
    learn,
    users,
    dashboard,
    media,
    studio,
)


async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    """422 with where and what is wrong, never echoing the values sent (passwords, answers)."""
    errors = [{"loc": list(error.get("loc", ())), "msg": error.get("msg", ""), "type": error.get("type", "")} for error in exc.errors()]
    return JSONResponse({"detail": errors}, status_code=status.HTTP_422_UNPROCESSABLE_CONTENT)


def schedule_maintenance() -> None:
    """Background upkeep that must run once per deploy (idempotent)."""
    with SessionLocal() as db:
        queue.enqueue(db, "legacy.migrate", dedupe_key="legacy-migrate", max_attempts=5)


def start_worker() -> WorkerPool | None:
    settings = get_settings()
    if not settings.worker_enabled:
        return None
    pool = WorkerPool(settings.worker_concurrency)
    pool.start()
    return pool


@asynccontextmanager
async def lifespan(app: FastAPI):
    run_migrations()
    # HeyGen retires its v1/v2 API on 2026-10-31: copy finished videos to Storage now.
    start_background_sweeps()
    schedule_maintenance()
    worker = start_worker()
    yield
    if worker:
        worker.stop()  # running jobs go back to the queue, without counting an attempt


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(title="Action Colleague API", version="2.0.0", lifespan=lifespan)
    app.add_exception_handler(RequestValidationError, validation_error)
    # Middleware added last runs first: proxy headers -> CORS -> request logging -> routes.
    app.add_middleware(RequestLogMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_origin_regex=settings.cors_origin_regex or None,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["x-request-id"],
    )
    # Railway terminates TLS; trust X-Forwarded-Proto so redirects keep https.
    app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")

    for module in ROUTERS:
        app.include_router(module.router, prefix=API_PREFIX)
    app.include_router(media.local_router, prefix=API_PREFIX)

    @app.get("/")
    def root():
        return {"service": app.title, "version": app.version}

    @app.get("/health")
    def health():
        return {"status": "ok"}

    return app


app = create_app()
