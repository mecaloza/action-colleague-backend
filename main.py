from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from database import create_tables
from routers import (
    auth,
    certificates,
    communications,
    course_wizard,
    courses,
    dashboards,
    documents,
    enrollments,
    evaluations,
    module_progress,
    modules,
    series_wizard,
    slides,
    users,
    videos,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_tables()
    yield


app = FastAPI(
    title="Action Colleague Backend",
    description="LMS & HR document platform for Action Colleague",
    version="1.0.0",
    lifespan=lifespan,

)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3001", "https://action-colleague.vercel.app", "https://action-colleague-mecalozas-projects.vercel.app"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

API_PREFIX = "/api/v1"

app.include_router(auth.router, prefix=API_PREFIX)
app.include_router(users.router, prefix=API_PREFIX)
app.include_router(courses.router, prefix=API_PREFIX)
app.include_router(course_wizard.router, prefix=API_PREFIX)
app.include_router(modules.router, prefix=API_PREFIX)
app.include_router(evaluations.router, prefix=API_PREFIX)
app.include_router(enrollments.router, prefix=API_PREFIX)
app.include_router(module_progress.router, prefix=API_PREFIX)
app.include_router(certificates.router, prefix=API_PREFIX)
app.include_router(documents.router, prefix=API_PREFIX)
app.include_router(dashboards.router, prefix=API_PREFIX)
app.include_router(series_wizard.router, prefix=API_PREFIX)
app.include_router(communications.router, prefix=API_PREFIX)
app.include_router(videos.router, prefix=API_PREFIX)
app.include_router(slides.router, prefix=API_PREFIX)


@app.get("/")
def root():
    return {"service": "Action Colleague Backend", "version": "1.0.0"}


@app.get("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8001, reload=True)
