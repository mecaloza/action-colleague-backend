"""Structured logging: one JSON object per line on stdout, plus request logging."""

import json
import logging
import re
import sys
import time
import uuid
from datetime import datetime, timezone

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

_STANDARD_ATTRS = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime", "color_message"}

logger = logging.getLogger("app.request")

# Client-supplied request ids are echoed and logged: accept only short, safe ones.
_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update({k: v for k, v in record.__dict__.items() if k not in _STANDARD_ATTRS})
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    """Route every logger (including uvicorn's) through the JSON formatter. Idempotent."""
    root = logging.getLogger()
    if not any(isinstance(h.formatter, JsonFormatter) for h in root.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        root.addHandler(handler)
    root.setLevel(level.upper())
    for name in ("uvicorn", "uvicorn.error"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True
    # Access logs are replaced by RequestLogMiddleware; httpx logs full URLs (possibly signed).
    logging.getLogger("uvicorn.access").disabled = True
    logging.getLogger("httpx").setLevel(logging.WARNING)


class RequestLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        incoming = request.headers.get("x-request-id", "")
        request_id = incoming if _REQUEST_ID.fullmatch(incoming) else uuid.uuid4().hex[:12]
        context = {"request_id": request_id, "method": request.method, "path": request.url.path}
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            logger.exception("request_failed", extra=context)
            # Answer here (inside CORSMiddleware) so the browser sees a 500, not a CORS error.
            response = JSONResponse({"detail": "Error interno del servidor", "request_id": request_id}, status_code=500)
        response.headers["x-request-id"] = request_id
        if request.url.path != "/health":
            duration_ms = round((time.perf_counter() - start) * 1000, 1)
            logger.info("request", extra={**context, "status": response.status_code, "duration_ms": duration_ms})
        return response
