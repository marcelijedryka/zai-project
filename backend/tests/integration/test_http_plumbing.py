"""Problem Details, 406/415, security headers, CORS, request ids and logging (plan §5.1-5.2)."""

import json
import logging
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI, Response
from pydantic import BaseModel

from app.config import Settings
from app.core.logging import JsonFormatter
from app.core.problems import MAX_VALIDATION_ERRORS, PROBLEM_MEDIA_TYPE, ProblemError
from app.main import create_app

ALLOWED_ORIGIN = "http://localhost:5173"
EVIL_ORIGIN = "https://evil.example"


class Item(BaseModel):
    name: str
    value: float


class Location(BaseModel):
    path: int
    query: int


class Nested(BaseModel):
    location: Location


def _build_app(settings: Settings) -> FastAPI:
    app = create_app(settings)

    @app.post("/api/_t/items", status_code=201)
    async def create_item(item: Item) -> dict[str, object]:
        return {"name": item.name, "value": item.value}

    @app.post("/api/_t/nested", status_code=201)
    async def create_nested(nested: Nested) -> dict[str, str]:
        return {"ok": "yes"}

    @app.post("/api/_t/many", status_code=201)
    async def create_many(items: list[Item]) -> dict[str, int]:
        return {"n": len(items)}

    @app.delete("/api/_t/items/1", status_code=204)
    async def delete_item() -> Response:
        return Response(status_code=204)

    @app.get("/api/_t/boom")
    async def boom() -> None:
        raise RuntimeError("secret internal detail: /etc/passwd")

    @app.get("/api/_t/conflict")
    async def conflict() -> None:
        raise ProblemError(
            409,
            "Range excludes stored data.",
            type_="urn:zai:problem:range-conflict",
            extensions={"dataMin": 1.5, "dataMax": 9.0},
        )

    @app.post("/api/auth/_t")
    async def auth_route() -> dict[str, str]:
        return {"ok": "yes"}

    return app


@pytest.fixture
async def api(settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    app = _build_app(settings)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://test") as client,
    ):
        yield client


def _assert_problem(response: httpx.Response, status: int) -> dict[str, object]:
    assert response.status_code == status
    assert response.headers["content-type"].startswith(PROBLEM_MEDIA_TYPE)
    body: dict[str, object] = response.json()
    assert body["status"] == status
    assert isinstance(body["title"], str)
    return body


# --- Problem Details -------------------------------------------------------------


async def test_unknown_route_is_a_404_problem(api: httpx.AsyncClient) -> None:
    body = _assert_problem(await api.get("/api/nope"), 404)
    assert body["title"] == "Not Found"
    assert body["instance"] == "/api/nope"


async def test_wrong_method_is_a_405_problem_with_allow_header(api: httpx.AsyncClient) -> None:
    response = await api.put("/api/health", json={})
    _assert_problem(response, 405)
    assert "GET" in response.headers["allow"]


async def test_problem_error_carries_type_detail_and_extensions(api: httpx.AsyncClient) -> None:
    response = await api.get("/api/_t/conflict")
    body = _assert_problem(response, 409)
    assert body["type"] == "urn:zai:problem:range-conflict"
    assert body["detail"] == "Range excludes stored data."
    assert (body["dataMin"], body["dataMax"]) == (1.5, 9.0)


async def test_validation_errors_are_400_with_field_messages(api: httpx.AsyncClient) -> None:
    response = await api.post("/api/_t/items", json={"value": "abc"})
    body = _assert_problem(response, 400)
    fields = {e["field"] for e in body["errors"]}  # type: ignore[attr-defined]
    assert fields == {"name", "value"}


async def test_nested_fields_named_like_request_parts_keep_their_names(
    api: httpx.AsyncClient,
) -> None:
    response = await api.post("/api/_t/nested", json={"location": {"path": "x", "query": "y"}})
    body = _assert_problem(response, 400)
    assert {e["field"] for e in body["errors"]} == {"location.path", "location.query"}  # type: ignore[attr-defined]


async def test_validation_error_list_is_capped(api: httpx.AsyncClient) -> None:
    response = await api.post("/api/_t/many", json=[{"value": "bad"}] * 200)
    body = _assert_problem(response, 400)
    assert len(body["errors"]) == MAX_VALIDATION_ERRORS  # type: ignore[arg-type]


async def test_validation_errors_never_echo_the_input(api: httpx.AsyncClient) -> None:
    response = await api.post("/api/_t/items", json={"name": "n", "value": "hunter2-secret"})
    assert "hunter2-secret" not in response.text


async def test_malformed_json_is_400(api: httpx.AsyncClient) -> None:
    response = await api.post(
        "/api/_t/items", content='{"name":', headers={"Content-Type": "application/json"}
    )
    body = _assert_problem(response, 400)
    assert body["errors"] == [{"field": "body", "message": "Request body is not valid JSON"}]


async def test_unhandled_exception_is_a_generic_500(api: httpx.AsyncClient) -> None:
    response = await api.get("/api/_t/boom")
    _assert_problem(response, 500)
    assert "passwd" not in response.text
    assert "Traceback" not in response.text
    assert "RuntimeError" not in response.text


async def test_unhandled_exception_is_logged_without_leaking_secrets(
    api: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    key = "zai_" + "Zz9Yy8Xx7Ww6Vv5Uu4Tt3Ss2Rr1Qq0Pp"
    with caplog.at_level(logging.ERROR, logger="zai.errors"):
        await api.get("/api/_t/boom", headers={"X-API-Key": key, "Authorization": "Bearer tok"})
    record = next(r for r in caplog.records if r.name == "zai.errors")
    assert record.exc_info is not None  # the traceback is kept server-side
    logged = record.fields["headers"]  # type: ignore[attr-defined]
    assert logged["x-api-key"] == "[REDACTED]"  # masked before it even reaches the record
    assert logged["authorization"] == "[REDACTED]"
    line = JsonFormatter().format(record)
    assert key not in line
    assert "Bearer tok" not in line
    assert "passwd" in line  # the traceback is kept server-side for debugging


# --- Content negotiation ---------------------------------------------------------


async def test_unacceptable_accept_is_406(api: httpx.AsyncClient) -> None:
    _assert_problem(await api.get("/api/health", headers={"Accept": "application/xml"}), 406)


async def test_acceptable_accept_values_pass(api: httpx.AsyncClient) -> None:
    for accept in ("application/json", "*/*", "application/*", "text/html, application/json;q=0.5"):
        response = await api.get("/api/nope", headers={"Accept": accept})
        assert response.status_code == 404, accept  # reached the router


async def test_non_json_body_is_415_even_with_a_valid_route(api: httpx.AsyncClient) -> None:
    response = await api.post(
        "/api/_t/items", content="value=1", headers={"Content-Type": "text/plain"}
    )
    _assert_problem(response, 415)


async def test_body_without_content_type_is_415(api: httpx.AsyncClient) -> None:
    request = api.build_request("POST", "/api/_t/items", content=b'{"name":"a","value":1}')
    request.headers.pop("content-type", None)
    _assert_problem(await api.send(request), 415)


async def test_406_takes_precedence_over_415(api: httpx.AsyncClient) -> None:
    response = await api.post(
        "/api/_t/items",
        content="x",
        headers={"Content-Type": "text/plain", "Accept": "application/xml"},
    )
    assert response.status_code == 406


async def test_bodyless_requests_are_never_415(api: httpx.AsyncClient) -> None:
    assert (await api.post("/api/auth/_t")).status_code == 200
    assert (await api.delete("/api/_t/items/1")).status_code == 204


async def test_json_charset_and_plus_json_bodies_are_accepted(api: httpx.AsyncClient) -> None:
    for content_type in ("application/json; charset=utf-8", "application/vnd.api+json"):
        response = await api.post(
            "/api/_t/items",
            content=b'{"name":"a","value":21.5}',
            headers={"Content-Type": content_type},
        )
        assert response.status_code == 201, content_type
        assert response.json()["value"] == 21.5


# --- Security headers, request id ------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "kwargs", "status"),
    [
        ("GET", "/api/health", {}, None),
        ("GET", "/api/nope", {}, 404),
        ("GET", "/api/health", {"headers": {"Accept": "application/xml"}}, 406),
        ("GET", "/api/_t/boom", {}, 500),
    ],
)
async def test_security_headers_on_every_response(
    api: httpx.AsyncClient, method: str, path: str, kwargs: dict[str, object], status: int | None
) -> None:
    response = await api.request(method, path, **kwargs)  # type: ignore[arg-type]
    if status:
        assert response.status_code == status
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["content-security-policy"] == (
        "default-src 'none'; frame-ancestors 'none'"
    )


async def test_auth_routes_are_not_cacheable(api: httpx.AsyncClient) -> None:
    response = await api.post("/api/auth/_t")
    assert response.headers["cache-control"] == "no-store"
    assert "cache-control" not in (await api.get("/api/nope")).headers


async def test_request_id_is_generated_and_client_values_are_validated(
    api: httpx.AsyncClient,
) -> None:
    generated = (await api.get("/api/health")).headers["x-request-id"]
    assert len(generated) == 32
    echoed = await api.get("/api/health", headers={"X-Request-ID": "trace-123"})
    assert echoed.headers["x-request-id"] == "trace-123"
    hostile = await api.get("/api/health", headers={"X-Request-ID": "bad id\twith spaces"})
    assert hostile.headers["x-request-id"] != "bad id\twith spaces"


async def test_access_log_has_no_query_string(
    api: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="zai.access"):
        await api.get("/api/health?token=secret-value")
    record = next(r for r in caplog.records if r.name == "zai.access")
    assert record.fields["path"] == "/api/health"  # type: ignore[attr-defined]
    assert record.fields["status"] == 200  # type: ignore[attr-defined]
    assert "secret-value" not in json.dumps(record.fields)  # type: ignore[attr-defined]


# --- CORS --------------------------------------------------------------------------


async def test_cors_reflects_only_allow_listed_origins(api: httpx.AsyncClient) -> None:
    ok = await api.get("/api/health", headers={"Origin": ALLOWED_ORIGIN})
    assert ok.headers["access-control-allow-origin"] == ALLOWED_ORIGIN
    assert ok.headers["access-control-allow-credentials"] == "true"
    evil = await api.get("/api/health", headers={"Origin": EVIL_ORIGIN})
    # Starlette still sends Allow-Credentials for disallowed origins, but without an
    # Allow-Origin the browser blocks the response; what must never happen is reflection.
    assert "access-control-allow-origin" not in evil.headers


async def test_cors_headers_are_present_on_error_responses(api: httpx.AsyncClient) -> None:
    for path in ("/api/nope", "/api/_t/boom"):
        response = await api.get(path, headers={"Origin": ALLOWED_ORIGIN})
        assert response.headers["access-control-allow-origin"] == ALLOWED_ORIGIN, path


async def test_cors_preflight(api: httpx.AsyncClient) -> None:
    preflight = {
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type,x-api-key",
    }
    ok = await api.options("/api/measurements", headers={"Origin": ALLOWED_ORIGIN, **preflight})
    assert ok.status_code == 200
    assert ok.headers["access-control-allow-origin"] == ALLOWED_ORIGIN
    evil = await api.options("/api/measurements", headers={"Origin": EVIL_ORIGIN, **preflight})
    assert "access-control-allow-origin" not in evil.headers


async def test_cors_exposes_location_and_total_count(api: httpx.AsyncClient) -> None:
    response = await api.get("/api/health", headers={"Origin": ALLOWED_ORIGIN})
    exposed = response.headers["access-control-expose-headers"].lower()
    assert "location" in exposed
    assert "x-total-count" in exposed


# --- Health uses problem details too ------------------------------------------------


async def test_health_503_is_a_problem(settings: Settings) -> None:
    bad = settings.model_copy(update={"postgres_host": "127.0.0.1", "postgres_port": 1})
    app = create_app(bad)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c,
    ):
        _assert_problem(await c.get("/api/health"), 503)


async def test_aborted_request_is_logged_without_a_fake_status(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import asyncio

    from app.core.middleware import RequestContextMiddleware

    async def cancelled_app(scope: dict[str, object], receive: object, send: object) -> None:
        raise asyncio.CancelledError

    middleware = RequestContextMiddleware(cancelled_app)  # type: ignore[arg-type]
    scope = {"type": "http", "method": "GET", "path": "/api/slow", "headers": []}

    async def receive() -> dict[str, object]:
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        raise AssertionError("no response should be sent")

    with caplog.at_level(logging.INFO, logger="zai.access"), pytest.raises(asyncio.CancelledError):
        await middleware(scope, receive, send)  # type: ignore[arg-type]
    record = next(r for r in caplog.records if r.name == "zai.access")
    assert record.fields["status"] is None  # type: ignore[attr-defined]
    assert record.fields["aborted"] is True  # type: ignore[attr-defined]
