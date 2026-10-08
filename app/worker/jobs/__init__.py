"""Job handlers. Importing this package registers every job type with the runner."""

from app.worker.jobs import activities, ai, media, recording, render, revisions  # noqa: F401
