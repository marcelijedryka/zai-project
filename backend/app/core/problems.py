"""RFC 9457 Problem Details: response builder, exception type and handlers."""

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
    return problem_response(
        exc.status_code,
        instance=request.url.path,
        detail=detail or _DEFAULT_DETAILS.get(exc.status_code),
        headers=dict(exc.headers) if exc.headers else None,
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
