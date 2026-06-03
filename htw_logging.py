"""Shared console logging for HTW micropayment services."""

from __future__ import annotations

import logging
import os
import sys
import time
from typing import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

_CONFIGURED: set[str] = set()


def setup_service_logging(service_name: str) -> logging.Logger:
    """Configure stdout logging once per service name."""
    if service_name not in _CONFIGURED:
        level_name = os.getenv("LOG_LEVEL", "INFO").upper()
        level = getattr(logging, level_name, logging.INFO)
        logging.basicConfig(
            level=level,
            format=f"%(asctime)s | {service_name:<20} | %(levelname)-7s | %(message)s",
            datefmt="%H:%M:%S",
            stream=sys.stdout,
            force=True,
        )
        logging.getLogger("uvicorn.error").setLevel(level)
        logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
        _CONFIGURED.add(service_name)
    logger = logging.getLogger(service_name)
    return logger


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Log each HTTP request with duration (skip /static)."""

    def __init__(self, app, service_name: str, logger: logging.Logger | None = None):
        super().__init__(app)
        self.logger = logger or logging.getLogger(service_name)

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        path = request.url.path
        if path.startswith("/static"):
            return await call_next(request)

        started = time.perf_counter()
        client = request.client.host if request.client else "?"
        self.logger.info("→ %s %s from %s", request.method, path, client)
        try:
            response = await call_next(request)
            elapsed_ms = (time.perf_counter() - started) * 1000
            self.logger.info(
                "← %s %s %s %.0fms",
                request.method,
                path,
                response.status_code,
                elapsed_ms,
            )
            return response
        except Exception:
            elapsed_ms = (time.perf_counter() - started) * 1000
            self.logger.exception(
                "← %s %s FAILED %.0fms",
                request.method,
                path,
                elapsed_ms,
            )
            raise


def attach_request_logging(app, service_name: str) -> logging.Logger:
    """Enable console logging and HTTP request middleware on a FastAPI app."""
    logger = setup_service_logging(service_name)
    app.add_middleware(RequestLoggingMiddleware, service_name=service_name, logger=logger)
    return logger
