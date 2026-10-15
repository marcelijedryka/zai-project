"""Pure-ASGI middleware (no ``BaseHTTPMiddleware``: streaming/SSE safe).

Stack, outermost first (see ``app.main.create_app``)::

    RequestContext -> SecurityHeaders -> CORS -> CatchAllErrors -> Negotiation -> router

The catch-all sits *inside* CORS and the security headers so that even a 500 carries them
(Starlette's own server-error handler runs outside every user middleware).
"""

import logging
import re
import time
import uuid
from typing import Any

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.logging import redact, request_id_var
from app.core.paths import DOCS_PATH, STREAM_PATH
from app.core.problems import problem_response
from app.security.tokens import COOKIE_NAME

access_logger = logging.getLogger("zai.access")
error_logger = logging.getLogger("zai.errors")

JSON_TYPES = ("application/json", "application/problem+json")
STREAM_TYPES = ("text/event-stream",)
API_CSP = "default-src 'none'; frame-ancestors 'none'"
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def accepts(accept: str, offered: tuple[str, ...]) -> bool:
    """True if any media range of an ``Accept`` header with q > 0 matches an offered type."""
    for part in accept.split(","):
        media, *params = (p.strip() for p in part.split(";"))
        media = media.lower()
        if not media:
            continue
        quality = 1.0
        for param in params:
            key, _, value = param.partition("=")
            if key.strip().lower() == "q":
                try:
                    quality = float(value)
                except ValueError:
                    quality = 0.0
        if quality <= 0:
            continue
        if media == "*/*" or media in offered:
            return True
        if media.endswith("/*") and any(o.startswith(media[:-1]) for o in offered):
            return True
    return False


def is_json_media_type(content_type: str) -> bool:
    media = content_type.split(";")[0].strip().lower()
    is_plus_json = media.startswith("application/") and media.endswith("+json")
    return media == "application/json" or is_plus_json


def has_body(headers: Headers) -> bool:
    if "transfer-encoding" in headers:
        return True
    try:
        return int(headers.get("content-length", "0")) > 0
    except ValueError:
        return True


class NegotiationMiddleware:
    """406 for an unsatisfiable ``Accept``; 415 for a non-JSON body (only if a body exists)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] == DOCS_PATH:
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        path = scope["path"]
        offered = STREAM_TYPES if path == STREAM_PATH else JSON_TYPES
        accept = headers.get("accept", "").strip()
        if accept and not accepts(accept, offered):
            response = problem_response(
                406,
                instance=path,
                detail=f"Supported representations: {', '.join(offered)}.",
            )
            await response(scope, receive, send)
            return
        if has_body(headers) and not is_json_media_type(headers.get("content-type", "")):
            response = problem_response(
                415,
                instance=path,
                detail="The request body must be application/json.",
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


class CatchAllErrorsMiddleware:
    """Turns any unhandled exception into a generic 500 problem; details only go to the log."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, tracking_send)
        except Exception:
            error_logger.error(
                "unhandled exception",
                exc_info=True,
                extra={
                    "fields": {
                        "method": scope["method"],
                        "path": scope["path"],
                        "headers": redact(dict(Headers(scope=scope))),
                    }
                },
            )
            if started:
                raise
            response = problem_response(
                500, instance=scope["path"], detail="An unexpected error occurred."
            )
            await response(scope, receive, send)


class SecurityHeadersMiddleware:
    """Adds API security headers to every response, including errors (plan §6.4)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope["path"]
        request_headers = Headers(scope=scope)
        # A request that carries credentials gets answers meant for that user only (admin data,
        # the one-time sensor key): keep them out of shared caches (proxies, shared browsers).
        credentialed = "authorization" in request_headers or COOKIE_NAME in request_headers.get(
            "cookie", ""
        )

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("Referrer-Policy", "no-referrer")
                if path != DOCS_PATH:  # Swagger UI needs scripts/styles the strict CSP forbids
                    headers.setdefault("Content-Security-Policy", API_CSP)
                if path.startswith("/api/auth/"):
                    headers["Cache-Control"] = "no-store"
                elif credentialed:
                    headers.setdefault("Cache-Control", "no-store")  # e.g. the stream says no-cache
            await send(message)

        await self.app(scope, receive, send_with_headers)


class RequestContextMiddleware:
    """Assigns a request id (``X-Request-ID``) and writes one JSON access-log line."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = Headers(scope=scope).get("x-request-id", "")
        request_id = incoming if _REQUEST_ID.match(incoming) else uuid.uuid4().hex
        token = request_id_var.set(request_id)
        status: int | None = None  # stays None if the client goes away before a response starts
        started = time.perf_counter()

        async def send_with_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            fields: dict[str, Any] = {
                "method": scope["method"],
                "path": scope["path"],  # no query string: it may carry sensitive values
                "status": status,
                "durationMs": round((time.perf_counter() - started) * 1000, 1),
            }
            if status is None:
                fields["aborted"] = True
            access_logger.info("http.request", extra={"fields": fields})
            request_id_var.reset(token)
