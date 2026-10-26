"""Response security headers as a pure ASGI middleware.

Only the response start message is touched (headers added unless the endpoint set them),
so request bodies, multipart uploads and streaming responses pass through unchanged.

- X-Content-Type-Options: nosniff, X-Frame-Options: DENY, Referrer-Policy: no-referrer.
- Content-Security-Policy "default-src 'none'; frame-ancestors 'none'" for the API: its
  responses are JSON and must never render as a page. The interactive docs (/docs,
  /redoc) load Swagger UI scripts from a CDN, so they get no CSP from here (they are off in
  prod by default).
- Cache-Control: no-store on /api responses: they carry tokens and private document text.
- Strict-Transport-Security only when enabled: it is only correct behind HTTPS (TLS is
  terminated by a reverse proxy, never by this app).
"""

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_API_CSP = "default-src 'none'; frame-ancestors 'none'"
_DOCS_PREFIXES = ("/docs", "/redoc")


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp, api_prefix: str, hsts: bool) -> None:
        self.app = app
        self.api_prefix = api_prefix
        self.hsts = hsts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope.get("path", "")

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("X-Frame-Options", "DENY")
                headers.setdefault("Referrer-Policy", "no-referrer")
                if not path.startswith(_DOCS_PREFIXES):
                    headers.setdefault("Content-Security-Policy", _API_CSP)
                if path.startswith(self.api_prefix):
                    headers.setdefault("Cache-Control", "no-store")
                if self.hsts:
                    headers.setdefault(
                        "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
                    )
            await send(message)

        await self.app(scope, receive, send_with_headers)
