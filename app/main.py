import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import update
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
from app.db.models import Job
from app.db.session import SessionLocal
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


logger = logging.getLogger(__name__)


def schedule_maintenance() -> None:
    """Background upkeep that must run once per deploy (idempotent)."""
    with SessionLocal() as db:
        job = queue.enqueue(db, "legacy.migrate", dedupe_key="legacy-migrate", max_attempts=5)
        now = queue.utcnow()  # a pass waiting for its next recheck runs now: every deploy looks again
        db.execute(
            update(Job)
            .where(Job.id == job.id, Job.status == "queued", Job.run_after > now)
            .values(run_after=now)
            .execution_options(synchronize_session=False)  # compared in the database, not in Python
        )
        db.commit()


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
    try:
        schedule_maintenance()  # among others, copies the previous app's HeyGen videos before HeyGen retires them
    except Exception:  # upkeep never keeps the app from starting: the next deploy schedules it again
        logger.exception("maintenance_not_scheduled")
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
        expose_headers=["x-request-id", "x-visual-pending"],  # the slide preview says its picture is coming
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
