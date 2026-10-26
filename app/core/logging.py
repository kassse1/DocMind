"""Structured (JSON) logging and request ids for the API and the worker.

One JSON object per line on stdout: ts (UTC, ms), level, service ("api" / "worker"),
logger, msg, request_id (when inside a request), plus the structured fields passed in
`extra` (http_method, http_route, http_status, elapsed_ms, ...).

What never goes in: Authorization headers, passwords, tokens, emails, document text and
raw query strings. Application code only logs ids, counts and error classes; the access
log uses the route template ("/api/v1/documents/{document_id}"), never the raw URL; the
uvicorn access logger (which prints the raw path and query) is disabled. Exceptions are
logged as their class and stack frames WITHOUT the exception message, because driver and
library messages can quote SQL parameters or document content.

Request ids: X-Request-ID from the client is reused only if it is 1-64 characters of
[A-Za-z0-9._-] (no log injection); otherwise a new one is generated. It is returned in the
response header and attached to every log line of that request.
"""

import contextvars
import json
import logging
import re
import sys
import time
import traceback
import uuid
from datetime import UTC, datetime

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_FIELDS = (
    "http_method", "http_route", "http_status", "elapsed_ms", "client_request_id_rejected",
)


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        data: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "service": self.service,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        request_id = request_id_var.get()
        if request_id:
            data["request_id"] = request_id
        for field in _FIELDS:
            if hasattr(record, field):
                data[field] = getattr(record, field)
        if record.exc_info and record.exc_info[0] is not None:
            data["exc_type"] = record.exc_info[0].__name__
            data["stack"] = "".join(traceback.format_tb(record.exc_info[2]))[-4000:]
        return json.dumps(data, ensure_ascii=False, default=str)


def configure_logging(level: str, fmt: str, service: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        JsonFormatter(service)
        if fmt == "json"
        else logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root = logging.getLogger()
    for existing in list(root.handlers):
        # Keep pytest's capture handlers; replace console handlers set up earlier.
        if type(existing) is logging.StreamHandler:
            root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)
    # uvicorn installs its own handlers; route its server logs through ours instead and
    # silence its access log (raw path + query string; ours logs the route template).
    for name in ("uvicorn", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
    access = logging.getLogger("uvicorn.access")
    access.handlers.clear()
    access.propagate = False
    access.disabled = True
    # Per-request "HTTP Request: POST <full URL>" lines from httpx: noise for Ollama calls,
    # and raw URLs (with ids) for test clients.
    for name in ("httpx", "httpx2"):
        logging.getLogger(name).setLevel(logging.WARNING)


access_logger = logging.getLogger("app.access")


class RequestContextMiddleware:
    """Request id + one access log line per request. Pure ASGI: bodies pass through."""

    def __init__(self, app: ASGIApp, api_prefix: str = "") -> None:
        self.app = app
        self.api_prefix = api_prefix

    def _route_template(self, scope: Scope) -> str:
        route = getattr(scope.get("route"), "path", None)
        if not route:
            return "unmatched"
        # Routes of the included API router report their path without its prefix.
        path = scope.get("path", "")
        if self.api_prefix and path.startswith(self.api_prefix) and not route.startswith(
            self.api_prefix
        ):
            return self.api_prefix + route
        return route

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        supplied = None
        for name, value in scope.get("headers", []):
            if name == b"x-request-id":
                supplied = value.decode("latin-1")
                break
        valid = supplied is not None and _REQUEST_ID.match(supplied) is not None
        request_id = supplied if valid else uuid.uuid4().hex
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        status_code = 500

        async def send_with_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            access_logger.info(
                "request",
                extra={
                    "http_method": scope.get("method"),
                    "http_route": self._route_template(scope),
                    "http_status": status_code,
                    "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
                    **({"client_request_id_rejected": True} if supplied and not valid else {}),
                },
            )
            request_id_var.reset(token)
