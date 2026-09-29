"""Job handlers. Importing this package registers every job type with the runner."""

from app.worker.jobs import ai, media, render  # noqa: F401
