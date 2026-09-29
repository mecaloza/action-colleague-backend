from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.api.routes import (
    auth,
    course_wizard,
    courses,
    dashboard,
    learn,
    participants,
    slides,
    users,
    videos,
)
from app.core.config import get_settings
from app.core.logging import RequestLogMiddleware, configure_logging
from app.db.migrate import run_migrations
from app.services.heygen_persist import start_background_sweeps

API_PREFIX = "/api/v1"
ROUTERS = (
    auth,
    courses,
    participants,
    learn,
    users,
    dashboard,
    # Previous app, replaced in the next releases (AI wizard, manual video upload, slides).
    course_wizard,
    videos,
    slides,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    run_migrations()
    # HeyGen retires its v1/v2 API on 2026-10-31: copy finished videos to Storage now.
    start_background_sweeps()
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(title="Action Colleague API", version="2.0.0", lifespan=lifespan)
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

    @app.get("/")
    def root():
        return {"service": app.title, "version": app.version}

    @app.get("/health")
    def health():
        return {"status": "ok"}

    return app


app = create_app()
