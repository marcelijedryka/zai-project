"""RFC 9457 Problem Details: response builder, exception type and handlers."""

import re
from http import HTTPStatus
from typing import Any, cast

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse, Response

PROBLEM_MEDIA_TYPE = "application/problem+json"

_DEFAULT_DETAILS = {
    404: "The requested resource does not exist.",
    405: "This method is not allowed for the resource.",
}
_LOCATION_PREFIXES = {"body", "query", "path", "header", "cookie"}
MAX_VALIDATION_ERRORS = 50  # a huge invalid array must not amplify into a huge response


def _route_patterns(app: Any) -> list[tuple[re.Pattern[str], frozenset[str]]]:
    """Compiled path patterns with their methods, built once from the OpenAPI schema."""
    cached: list[tuple[re.Pattern[str], frozenset[str]]] | None = getattr(
        app.state, "route_patterns", None
    )
    if cached is None:
        cached = [
            (
                re.compile(re.sub(r"\{[^}/]+\}", "[^/]+", template)),
                frozenset(method.upper() for method in operations),
            )
            for template, operations in app.openapi().get("paths", {}).items()
        ]
        app.state.route_patterns = cached
    return cached


def allowed_methods(request: Request) -> str | None:
    """Every method the app serves for the request path, for the ``Allow`` header.

    Starlette's own header lists only the first partially matching route, which is misleading
    when a path has several (``GET``/``PUT``/``DELETE`` on ``/api/series/{id}``). The OpenAPI
    schema lists them all. HEAD is deliberately not advertised: FastAPI routes do not serve it.
    """
    methods: set[str] = set()
    for pattern, route_methods in _route_patterns(request.app):
        if pattern.fullmatch(request.url.path):
            methods |= route_methods
    return ", ".join(sorted(methods)) or None


class ProblemResponse(JSONResponse):
    media_type = PROBLEM_MEDIA_TYPE


def _phrase(status: int) -> str:
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return "Error"


def problem_response(
    status: int,
    *,
    instance: str | None = None,
    detail: str | None = None,
    title: str | None = None,
    type_: str = "about:blank",
    errors: list[dict[str, str]] | None = None,
    extensions: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> ProblemResponse:
    # Extensions go first so they can never overwrite the standard members.
    body: dict[str, Any] = dict(extensions or {})
    body |= {"type": type_, "title": title or _phrase(status), "status": status}
    if detail:
        body["detail"] = detail
    if instance:
        body["instance"] = instance
    if errors:
        body["errors"] = errors
    return ProblemResponse(body, status_code=status, headers=headers)


class ProblemError(Exception):
    """Raise anywhere (services included) to produce a Problem Details response."""

    def __init__(
        self,
        status: int,
        detail: str | None = None,
        *,
        title: str | None = None,
        type_: str = "about:blank",
        errors: list[dict[str, str]] | None = None,
        extensions: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(detail or title or _phrase(status))
        self.status = status
        self.detail = detail
        self.title = title
        self.type_ = type_
        self.errors = errors
        self.extensions = extensions
        self.headers = headers


async def problem_error_handler(request: Request, exc: Exception) -> Response:
    exc = cast(ProblemError, exc)
    return problem_response(
        exc.status,
        instance=request.url.path,
        detail=exc.detail,
        title=exc.title,
        type_=exc.type_,
        errors=exc.errors,
        extensions=exc.extensions,
        headers=exc.headers,
    )


async def http_exception_handler(request: Request, exc: Exception) -> Response:
    """Routing 404/405 and any ``HTTPException`` (``Allow`` and other headers are kept)."""
    exc = cast(StarletteHTTPException, exc)
    detail = exc.detail if exc.detail != _phrase(exc.status_code) else None
    headers = dict(exc.headers) if exc.headers else {}
    if exc.status_code == 405 and (allow := allowed_methods(request)):
        headers["Allow"] = allow
    return problem_response(
        exc.status_code,
        instance=request.url.path,
        detail=detail or _DEFAULT_DETAILS.get(exc.status_code),
        headers=headers or None,
    )


def _field(loc: tuple[Any, ...]) -> str:
    """Dotted path of the failing field; only the leading request-part marker is dropped."""
    parts = loc[1:] if loc and loc[0] in _LOCATION_PREFIXES else loc
    return ".".join(str(p) for p in parts) or "body"


async def validation_exception_handler(request: Request, exc: Exception) -> Response:
    """Every request-validation failure is a 400 with per-field ``errors``.

    Only the pydantic message is reported, never the offending input, so secrets
    (passwords, keys) sent in a malformed body are not echoed back. That holds for pydantic's
    built-in messages; custom validators must not put the value into their error text.
    """
    exc = cast(RequestValidationError, exc)
    if any(err["loc"] and err["loc"][0] == "path" for err in exc.errors()):
        # An id that does not parse or is out of range cannot name an existing resource.
        return problem_response(404, instance=request.url.path, detail=_DEFAULT_DETAILS[404])
    errors = [
        {"field": "body", "message": "Request body is not valid JSON"}
        if err["type"] == "json_invalid"
        else {"field": _field(tuple(err["loc"])), "message": str(err["msg"])}
        for err in exc.errors()[:MAX_VALIDATION_ERRORS]
    ]
    return problem_response(
        400,
        instance=request.url.path,
        detail="The request is invalid.",
        errors=errors,
    )
