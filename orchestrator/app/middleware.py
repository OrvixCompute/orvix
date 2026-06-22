"""HTTP middleware: CORS and per-request logging with a generated request ID."""

import time
import uuid

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings
from app.logger import logger


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Assign each request a UUID, time it, and log a one-line summary."""

    async def dispatch(self, request: Request, call_next):
        request_id = str(uuid.uuid4())
        request.state.request_id = request_id

        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # Let exception handlers produce the body; still record timing.
            duration_ms = (time.perf_counter() - start) * 1000
            logger.exception(
                "request_failed method={} path={} duration_ms={:.1f} request_id={}",
                request.method,
                request.url.path,
                duration_ms,
                request_id,
            )
            raise

        duration_ms = (time.perf_counter() - start) * 1000
        response.headers["X-Request-ID"] = request_id
        logger.info(
            "request method={} path={} status={} duration_ms={:.1f} request_id={}",
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
            request_id,
        )
        return response


def register_middleware(app: FastAPI) -> None:
    """Register all middleware. CORS is added last so it runs outermost."""
    app.add_middleware(RequestLoggingMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID", "X-Orvix-Tier", "X-Orvix-Cost"],
    )
