"""Unified error envelope: {"error": {"code", "message", "details"}}.

Every error response, including framework-level ones (404, 405, validation),
is rendered through this module so clients can rely on a single shape.
"""

import logging
from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import FastAPI, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy.exc import OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


class AppError(Exception):
    """Base class for domain errors raised by services and mapped to HTTP responses."""

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = status.HTTP_400_BAD_REQUEST,
        details: Any = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details
        # E.g. WWW-Authenticate on 401 responses.
        self.headers = headers


def error_body(code: str, message: str, details: Any = None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "details": details}}


_HTTP_CODES: dict[int, str] = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    415: "unsupported_media_type",
    422: "validation_error",
    429: "too_many_requests",
}


async def _app_error_handler(_: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=error_body(exc.code, exc.message, exc.details),
        headers=exc.headers,
    )


async def _http_error_handler(_: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = _HTTP_CODES.get(exc.status_code, "http_error")
    message = exc.detail if isinstance(exc.detail, str) else code.replace("_", " ")
    return JSONResponse(
        status_code=exc.status_code,
        content=error_body(code, message),
        headers=getattr(exc, "headers", None),
    )


async def _validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    # Drop "input" from each error: it echoes the request payload, which may contain
    # document text or other sensitive data that must not be reflected back or logged.
    details = [
        {"loc": list(err.get("loc", ())), "msg": err.get("msg"), "type": err.get("type")}
        for err in exc.errors()
    ]
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content=error_body("validation_error", "Request validation failed", details),
    )


class DatabaseUnavailableRoute(APIRoute):
    """Route class mapping transient database failures to 503 database_unavailable.

    Connection refused/lost, statement timeout and pool exhaustion are reported as
    "try again later" instead of a generic 500. It wraps the whole route, so failures in
    dependencies (e.g. the current-user lookup) are covered too. Opt-in per router, so
    existing endpoints keep their documented behavior.
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def route_handler(request: Request) -> Response:
            try:
                return await handler(request)
            except (OperationalError, PoolTimeoutError) as exc:
                # Class name only: driver messages can include host, port, user and SQL.
                logger.warning("Database unavailable: %s", type(exc).__name__)
                raise AppError(
                    "database_unavailable",
                    "The database is unavailable. Try again later.",
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                ) from None

        return route_handler


async def _unhandled_error_handler(_: Request, exc: Exception) -> JSONResponse:
    # Log the exception type and traceback server-side; never return internals to the client.
    logger.exception("Unhandled error: %s", type(exc).__name__)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=error_body("internal_error", "Internal server error"),
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _app_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(Exception, _unhandled_error_handler)
