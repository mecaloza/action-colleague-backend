"""Compatibility entrypoint: `uvicorn main:app` and `python main.py` keep working."""

import os

from app.main import app  # noqa: F401

if __name__ == "__main__":
    import uvicorn

    # log_config=None: keep the JSON logging configured when the app was imported.
    uvicorn.run("app.main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8001")), log_config=None)
