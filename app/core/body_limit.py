"""Cap request body size while it streams in.

Starlette parses multipart bodies, spooling files to disk, BEFORE the endpoint runs, so a
size check inside the endpoint cannot stop a huge upload. This ASGI middleware rejects
oversized bodies up front (Content-Length) or as soon as the streamed byte count crosses
the limit (chunked uploads, lying Content-Length).
"""

from starlette.datastructures import Headers
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.errors import error_body

# Room for multipart boundaries and part headers on top of the file itself.
MULTIPART_OVERHEAD_BYTES = 64 * 1024
_MESSAGE = "Request body exceeds the upload size limit"


class BodySizeLimitMiddleware:
    def __init__(self, app: ASGIApp, max_upload_bytes: int) -> None:
        self.app = app
        self.max_upload_bytes = max_upload_bytes
        self.max_body_bytes = max_upload_bytes + MULTIPART_OVERHEAD_BYTES

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        details = {"max_upload_bytes": self.max_upload_bytes}
        content_length = Headers(scope=scope).get("content-length", "")
        if content_length.isdigit() and int(content_length) > self.max_body_bytes:
            response = JSONResponse(
                error_body("payload_too_large", _MESSAGE, details), status_code=413
            )
            await response(scope, receive, send)
            return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body_bytes:
                    # FastAPI re-raises HTTPException from body parsing, and the app's
                    # handler renders it in the standard error envelope.
                    raise HTTPException(status_code=413, detail=_MESSAGE)
            return message

        await self.app(scope, limited_receive, send)
