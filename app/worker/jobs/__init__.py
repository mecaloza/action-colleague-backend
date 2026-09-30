"""Job handlers. Importing this package registers every job type with the runner."""

from app.worker.jobs import ai, media, recording, render  # noqa: F401
