"""The official ``contract/zai-tests.mjs`` suite, ported to pytest (implementation plan §13).

Each official test is ported (same requests, same assertions) and registered with
``@official("<unique part of the official name>")``. ``test_every_official_test_is_mapped``
parses the official script, so a new, renamed or removed official test fails the suite until this
map is updated. Three checks need the deployed HTTPS/CloudFront setup and are listed in
``DEPLOY_ONLY`` with the reason; they are covered by the production run (Phase 3).
"""

import hashlib
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.conftest import Admin
from tests.generator_utils import seed

OFFICIAL_SCRIPT = Path(__file__).resolve().parents[3] / "contract" / "zai-tests.mjs"
# SHA-256 of the official script this module was ported from. If the lecturer publishes a revised
# script (same names, changed assertions, dates or limits), the ports below must be re-checked.
OFFICIAL_SCRIPT_SHA256 = "9e0effcb6a460bb7f03af1a4a35cd56d270c979660ea8a63f40b3983df2f48bf"
T0 = "2026-01-10T10:00:00Z"
T1 = "2026-01-10T11:00:00Z"
T2 = "2026-01-10T12:00:00Z"
TEST_PREFIX = "ZAI-TEST-"
FORGED_JWT = "eyJhbGciOiJub25lIn0.eyJzdWIiOiJhZG1pbiJ9."

REGISTRY: dict[str, str] = {}
DEPLOY_ONLY = {
    "działają przez HTTPS": "needs the public HTTPS deployment (Terraform/Caddy, Phase 3)",
    "przekierowuje na https://": "CloudFront viewer protocol policy (Phase 3)",
    "nagłówek Content-Security-Policy": "served by the SPA's CloudFront response headers policy",
}


def official(key: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register a pytest function as the port of the official test whose name contains ``key``."""

    def register(function: Callable[..., Any]) -> Callable[..., Any]:
        assert key not in REGISTRY, f"duplicate key {key!r}"
        REGISTRY[key] = function.__name__
        return function

    return register


# --- helpers (mirrors of the official script's helpers) --------------------------------------


def series_input(**over: Any) -> dict[str, Any]:
    name = f"{TEST_PREFIX}{datetime.now(UTC).timestamp()}"
    return {"name": name, "minValue": 0, "maxValue": 100, "color": "#1f77b4"} | over


def same_instant(a: str, b: str) -> bool:
    def parse(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    return parse(a) == parse(b)


def is_id(value: Any) -> bool:
    return (isinstance(value, int) and not isinstance(value, bool)) or (
        isinstance(value, str) and len(value) > 0
    )


def check_series(series: dict[str, Any]) -> None:
    assert is_id(series["id"])
    assert isinstance(series["name"], str)
    assert isinstance(series["minValue"], int | float)
    assert isinstance(series["maxValue"], int | float)
    assert re.fullmatch(r"#[0-9a-fA-F]{6}", series["color"])


def check_measurement(m: dict[str, Any]) -> None:
    assert is_id(m["id"]) and is_id(m["seriesId"])
    assert isinstance(m["value"], int | float)
    datetime.fromisoformat(m["timestamp"].replace("Z", "+00:00"))


def check_problem(response: httpx.Response) -> None:
    content_type = response.headers["content-type"]
    assert "application/problem+json" in content_type or "application/json" in content_type
    body = response.json()
    assert isinstance(body["status"], int) and isinstance(body["title"], str)


def ascending(items: list[dict[str, Any]]) -> bool:
    stamps = [datetime.fromisoformat(i["timestamp"].replace("Z", "+00:00")) for i in items]
    return all(a <= b for a, b in pairwise(stamps))


async def new_series(
    client: httpx.AsyncClient, auth: dict[str, str], **over: Any
) -> dict[str, Any]:
    response = await client.post("/api/series", json=series_input(**over), headers=auth)
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


async def series_with_sensor(
    client: httpx.AsyncClient, auth: dict[str, str], **over: Any
) -> tuple[dict[str, Any], dict[str, Any], str]:
    series = await new_series(client, auth, **over)
    response = await client.post(
        "/api/sensors",
        json={"name": f"{TEST_PREFIX}sensor", "seriesId": series["id"]},
        headers=auth,
    )
    assert response.status_code == 201, response.text
    sensor: dict[str, Any] = response.json()
    return series, sensor, sensor["apiKey"]


async def send(
    client: httpx.AsyncClient, key: str | None, body: Any, extra: dict[str, str] | None = None
) -> httpx.Response:
    headers = {"X-API-Key": key} if key else {}
    return await client.post("/api/measurements", json=body, headers={**headers, **(extra or {})})


async def send_ok(client: httpx.AsyncClient, key: str, body: dict[str, Any]) -> dict[str, Any]:
    response = await send(client, key, body)
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


async def official_scenario(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> tuple[dict[str, Any], dict[str, Any], str]:
    """The shared state the official kontrakt tests build up: 4 measurements in one series."""
    series, sensor, key = await series_with_sensor(client, auth)
    await send_ok(client, key, {"value": 21.5, "timestamp": T1})
    await send_ok(client, key, {"value": 5})  # no timestamp: server now
    await send_ok(client, key, {"value": 10, "timestamp": T2})
    await send_ok(client, key, {"value": 30, "timestamp": T0})
    return series, sensor, key


# === kontrakt (A1/A2) ======================================================================


@official("GET /api/health zwraca 200")
async def test_kontrakt_01_health(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@official("GET /api/series zwraca tablicę JSON")
async def test_kontrakt_02_series_list_is_a_public_json_array(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    await new_series(client, auth)
    response = await client.get("/api/series")
    assert response.status_code == 200
    assert "application/json" in response.headers["content-type"]
    assert isinstance(response.json(), list) and response.json()
    for item in response.json():
        check_series(item)


@official("Dane przykładowe")
async def test_kontrakt_03_sample_data_three_series_with_fifteen_points(
    client: httpx.AsyncClient, admin: Admin, server: str
) -> None:
    # F11: the sample data comes from the generator, through the API (here: a real server).
    result = await seed(server, admin, days=1)
    assert result.exit_code == 0, result.output
    series = (await client.get("/api/series")).json()
    own = [s for s in series if not str(s["name"]).startswith(TEST_PREFIX)]
    full = 0
    for item in own:
        listed = await client.get(f"/api/measurements?series={item['id']}&limit=10000")
        if listed.status_code == 200 and len(listed.json()) >= 15:
            full += 1
    assert full >= 3


@official("POST /api/auth/login zwraca token Bearer")
async def test_kontrakt_04_login_returns_a_bearer_token(
    client: httpx.AsyncClient, admin: Admin
) -> None:
    response = await client.post(
        "/api/auth/login", json={"username": admin.username, "password": admin.password}
    )
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body["accessToken"], str) and body["accessToken"]
    assert body["tokenType"] == "Bearer"
    assert isinstance(body["expiresIn"], int)


@official("POST /api/series tworzy serię")
async def test_kontrakt_05_post_series_creates_201_with_location(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    payload = series_input(unit="°C")
    response = await client.post("/api/series", json=payload, headers=auth)
    assert response.status_code == 201
    check_series(response.json())
    assert response.json()["name"] == payload["name"]
    assert (response.json()["minValue"], response.json()["maxValue"]) == (0, 100)
    assert response.headers["location"]


@official("GET na adres z Location zwraca utworzoną serię")
async def test_kontrakt_06_location_of_a_series_resolves(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    created = await client.post("/api/series", json=series_input(), headers=auth)
    response = await client.get(created.headers["location"])
    assert response.status_code == 200
    check_series(response.json())
    assert str(response.json()["id"]) == str(created.json()["id"])


@official("PUT /api/series/{id} aktualizuje serię")
async def test_kontrakt_07_put_series_updates(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    series = await new_series(client, auth)
    update = series_input(name=series["name"] + "-upd", color="#ff7f0e")
    response = await client.put(f"/api/series/{series['id']}", json=update, headers=auth)
    assert response.status_code == 200
    check_series(response.json())
    assert response.json()["name"] == update["name"]
    assert response.json()["color"].lower() == "#ff7f0e"


@official("POST /api/sensors rejestruje czujnik")
async def test_kontrakt_08_post_sensors_returns_the_key_once_with_location(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    series = await new_series(client, auth)
    response = await client.post(
        "/api/sensors", json={"name": f"{TEST_PREFIX}s", "seriesId": series["id"]}, headers=auth
    )
    assert response.status_code == 201
    assert is_id(response.json()["id"])
    assert str(response.json()["seriesId"]) == str(series["id"])
    assert isinstance(response.json()["apiKey"], str) and len(response.json()["apiKey"]) >= 32
    assert response.headers["location"]


@official("GET /api/sensors i /api/sensors/{id}")
async def test_kontrakt_09_sensor_reads_never_contain_the_key(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    _, sensor, key = await series_with_sensor(client, auth)
    listed = await client.get("/api/sensors", headers=auth)
    assert listed.status_code == 200
    assert any(str(s["id"]) == str(sensor["id"]) for s in listed.json())
    one = await client.get(f"/api/sensors/{sensor['id']}", headers=auth)
    assert one.status_code == 200
    assert key not in listed.text and key not in one.text


@official("POST /api/measurements kluczem czujnika zapisuje wynik")
async def test_kontrakt_10_post_measurement_with_a_sensor_key(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    series, _, key = await series_with_sensor(client, auth)
    response = await send(client, key, {"value": 21.5, "timestamp": T1})
    assert response.status_code == 201
    check_measurement(response.json())
    assert response.json()["value"] == 21.5
    assert same_instant(response.json()["timestamp"], T1)
    assert str(response.json()["seriesId"]) == str(series["id"])
    assert response.headers["location"]


@official("Wynik bez timestamp")
async def test_kontrakt_11_missing_timestamp_means_server_now(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    _, _, key = await series_with_sensor(client, auth)
    created = await send_ok(client, key, {"value": 5})
    check_measurement(created)
    stamp = datetime.fromisoformat(created["timestamp"].replace("Z", "+00:00"))
    assert abs(datetime.now(UTC) - stamp) < timedelta(minutes=10)


@official("GET na adres z Location zwraca zapisany wynik")
async def test_kontrakt_12_location_of_a_measurement_resolves(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    _, _, key = await series_with_sensor(client, auth)
    posted = await send(client, key, {"value": 21.5, "timestamp": T1})
    response = await client.get(posted.headers["location"])
    assert response.status_code == 200
    check_measurement(response.json())
    assert response.json()["value"] == 21.5


@official("zwraca wyniki posortowane rosnąco")
async def test_kontrakt_13_list_is_ascending_with_exactly_the_four_points(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    series, _, _ = await official_scenario(client, auth)
    response = await client.get(f"/api/measurements?series={series['id']}")
    assert response.status_code == 200
    items = response.json()
    for item in items:
        check_measurement(item)
    assert len(items) == 4  # no implicit time window: old and new points are all there
    assert all(str(i["seriesId"]) == str(series["id"]) for i in items)
    assert ascending(items)


@official("Filtrowanie from/to jest domknięte")
async def test_kontrakt_14_from_to_is_a_closed_interval(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    series, _, _ = await official_scenario(client, auth)
    response = await client.get(
        f"/api/measurements?series={series['id']}&from=2026-01-10T10%3A00%3A00Z&to=2026-01-10T11%3A00%3A00Z"
    )
    assert response.status_code == 200
    assert len(response.json()) == 2


@official("Filtr series obsługuje kilka serii")
async def test_kontrakt_15_series_filter_takes_several_series(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    series, _, _ = await official_scenario(client, auth)
    other, _, other_key = await series_with_sensor(client, auth)
    await send_ok(client, other_key, {"value": 1, "timestamp": T0})
    response = await client.get(f"/api/measurements?series={series['id']},{other['id']}")
    assert response.status_code == 200
    assert len(response.json()) == 5


@official("sort=-timestamp")
async def test_kontrakt_16_sort_descending(client: httpx.AsyncClient, auth: dict[str, str]) -> None:
    series, _, _ = await official_scenario(client, auth)
    response = await client.get(f"/api/measurements?series={series['id']}&sort=-timestamp")
    assert response.status_code == 200
    items = response.json()
    assert len(items) >= 2 and ascending(list(reversed(items)))


@official("PUT serii z przedziałem wykluczającym")
async def test_kontrakt_17_range_excluding_stored_data_is_409(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    series, _, _ = await official_scenario(client, auth)
    response = await client.put(
        f"/api/series/{series['id']}",
        json=series_input(name=series["name"], minValue=50, maxValue=60),
        headers=auth,
    )
    assert response.status_code == 409


@official("Nieistniejąca seria")
async def test_kontrakt_18_missing_series_is_a_404_problem(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/series/999999999")
    assert response.status_code == 404
    check_problem(response)


@official("Accept: application/xml")
async def test_kontrakt_19_unacceptable_accept_is_406(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/series", headers={"Accept": "application/xml"})
    assert response.status_code == 406


@official("Treść text/plain")
async def test_kontrakt_20_text_plain_body_is_415(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    _, _, key = await series_with_sensor(client, auth)
    response = await client.post(
        "/api/measurements",
        content="value=1",
        headers={"Content-Type": "text/plain", "X-API-Key": key},
    )
    assert response.status_code == 415


@official("Wyrejestrowanie czujnika")
async def test_kontrakt_21_unregistering_stops_the_key_but_keeps_the_data(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    _, sensor, key = await series_with_sensor(client, auth)
    posted = await send(client, key, {"value": 21.5, "timestamp": T1})
    deleted = await client.delete(f"/api/sensors/{sensor['id']}", headers=auth)
    assert deleted.status_code == 204
    assert (await send(client, key, {"value": 1})).status_code == 401
    assert (await client.get(posted.headers["location"])).status_code == 200


@official("DELETE serii usuwa")
async def test_kontrakt_22_deleting_a_series_removes_data_sensors_and_keys(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    series, sensor, key = await series_with_sensor(client, auth)
    measurement = await send_ok(client, key, {"value": 1, "timestamp": T0})
    assert (await client.delete(f"/api/series/{series['id']}", headers=auth)).status_code == 204
    assert (await client.get(f"/api/series/{series['id']}")).status_code == 404
    assert (await client.get(f"/api/measurements/{measurement['id']}")).status_code == 404
    assert (await client.get(f"/api/sensors/{sensor['id']}", headers=auth)).status_code == 404
    assert (await send(client, key, {"value": 1})).status_code == 401


@official("POST /api/auth/logout unieważnia token")
async def test_kontrakt_23_logout_invalidates_the_token(
    client: httpx.AsyncClient, admin: Admin
) -> None:
    login = await client.post(
        "/api/auth/login", json={"username": admin.username, "password": admin.password}
    )
    token = login.json()["accessToken"]
    headers = {"Authorization": f"Bearer {token}"}
    assert (await client.post("/api/auth/logout", headers=headers)).status_code == 204  # no body
    after = await client.post("/api/series", json=series_input(), headers=headers)
    assert after.status_code == 401


# === walidacja (A3) ========================================================================


@official("Wartość spoza przedziału serii")
async def test_walidacja_01_out_of_range_is_422_and_not_stored(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    series, _, key = await series_with_sensor(client, auth, minValue=0, maxValue=10)
    response = await send(client, key, {"value": 11, "timestamp": T0})
    assert response.status_code == 422
    check_problem(response)
    assert (await client.get(f"/api/measurements?series={series['id']}")).json() == []


@official("Wartości graniczne")
async def test_walidacja_02_bounds_are_inclusive(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    _, _, key = await series_with_sensor(client, auth, minValue=-5, maxValue=5)
    await send_ok(client, key, {"value": -5, "timestamp": T0})
    await send_ok(client, key, {"value": 5, "timestamp": T1})


@official("Znacznik czasu z przyszłości")
async def test_walidacja_03_one_hour_ahead_is_422(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    _, _, key = await series_with_sensor(client, auth)
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    assert (await send(client, key, {"value": 1, "timestamp": future})).status_code == 422


@official("minValue ≥ maxValue")
async def test_walidacja_04_min_equal_to_max_is_422(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    response = await client.post(
        "/api/series", json=series_input(minValue=10, maxValue=10), headers=auth
    )
    assert response.status_code == 422


@official("Niepoprawne typy i brak pól")
async def test_walidacja_05_bad_types_and_missing_fields(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    _, _, key = await series_with_sensor(client, auth)
    for body in ({"value": "abc"}, {}, {"value": 1, "timestamp": "wczoraj"}):
        assert (await send(client, key, body)).status_code in (400, 422), body
    no_name = await client.post(
        "/api/series", json={"minValue": 0, "maxValue": 1, "color": "#000000"}, headers=auth
    )
    assert no_name.status_code in (400, 422)


@official("Niepoprawny JSON")
async def test_walidacja_06_malformed_json_is_400(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    response = await client.post(
        "/api/series", content='{"name":', headers={**auth, "Content-Type": "application/json"}
    )
    assert response.status_code == 400


@official("Niepoprawny parametr from")
async def test_walidacja_07_bad_from_parameter_is_400(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/measurements?from=nie-data")).status_code == 400


@official("Rejestracja czujnika dla nieistniejącej serii")
async def test_walidacja_08_sensor_for_a_missing_series_is_422(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    response = await client.post(
        "/api/sensors", json={"name": f"{TEST_PREFIX}s", "seriesId": 999999999}, headers=auth
    )
    assert response.status_code in (422, 404)
    assert response.status_code == 422  # the contract says 422; 404 is only tolerated by the script


@official("Operacje administracyjne bez tokenu")
async def test_walidacja_09_admin_operations_without_a_token_are_401(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    series, sensor, _ = await series_with_sensor(client, auth)
    checks: list[tuple[str, str, Any]] = [
        ("POST", "/api/series", series_input()),
        ("PUT", f"/api/series/{series['id']}", series_input()),
        ("DELETE", f"/api/series/{series['id']}", None),
        ("GET", "/api/sensors", None),
        ("GET", f"/api/sensors/{sensor['id']}", None),
        ("POST", "/api/sensors", {"name": "x", "seriesId": series["id"]}),
        ("DELETE", f"/api/sensors/{sensor['id']}", None),
    ]
    for method, path, body in checks:
        response = await client.request(method, path, json=body)
        assert response.status_code == 401, f"{method} {path}"


@official("Wynik bez klucza")
async def test_walidacja_10_measurement_auth_needs_the_sensor_key(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    await series_with_sensor(client, auth)
    assert (await send(client, None, {"value": 1})).status_code == 401
    assert (await send(client, "x" * 40, {"value": 1})).status_code == 401
    admin_token_only = await client.post("/api/measurements", json={"value": 1}, headers=auth)
    assert admin_token_only.status_code == 401


@official("Wyników nie można modyfikować")
async def test_walidacja_11_measurements_are_immutable(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    _, _, key = await series_with_sensor(client, auth)
    measurement = await send_ok(client, key, {"value": 7, "timestamp": T0})
    for method in ("PUT", "PATCH", "DELETE"):
        body = None if method == "DELETE" else {"value": 8, "timestamp": T0}
        response = await client.request(
            method, f"/api/measurements/{measurement['id']}", json=body, headers=auth
        )
        assert response.status_code in (404, 405), method
    after = await client.get(f"/api/measurements/{measurement['id']}")
    assert after.status_code == 200 and after.json()["value"] == 7


@official("Sfałszowany token")
async def test_walidacja_12_forged_token_is_401(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/series", json=series_input(), headers={"Authorization": f"Bearer {FORGED_JWT}"}
    )
    assert response.status_code == 401


@official("Logowanie błędnym hasłem")
async def test_walidacja_13_wrong_password_is_401(client: httpx.AsyncClient, admin: Admin) -> None:
    response = await client.post(
        "/api/auth/login", json={"username": admin.username, "password": admin.password + "-zle"}
    )
    assert response.status_code == 401


@official("Zmiana hasła z błędnym")
async def test_walidacja_14_password_change_with_a_wrong_current_password(
    client: httpx.AsyncClient, admin: Admin, auth: dict[str, str]
) -> None:
    body = {"currentPassword": admin.password + "-zle", "newPassword": "NoweHaslo-123456"}
    assert (await client.put("/api/auth/password", json=body, headers=auth)).status_code == 403
    assert (await client.put("/api/auth/password", json=body)).status_code == 401


# === bezpieczenstwo (A4) ==================================================================


@official("X-Content-Type-Options")
async def test_bezpieczenstwo_04_api_sends_nosniff(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/series")
    assert response.headers["x-content-type-options"].lower() == "nosniff"


@official("Ciasteczka ustawiane przy logowaniu")
async def test_bezpieczenstwo_05_session_cookie_flags(
    client: httpx.AsyncClient, admin: Admin
) -> None:
    response = await client.post(
        "/api/auth/login", json={"username": admin.username, "password": admin.password}
    )
    cookies = response.headers.get_list("set-cookie")
    assert cookies
    for cookie in cookies:
        lowered = cookie.lower()
        assert "httponly" in lowered and "secure" in lowered and "samesite" in lowered, cookie


@official("nie ujawniają haseł")
async def test_bezpieczenstwo_06_responses_never_contain_secrets(
    client: httpx.AsyncClient, admin: Admin, auth: dict[str, str]
) -> None:
    series, sensor, key = await series_with_sensor(client, auth)
    await send_ok(client, key, {"value": 1, "timestamp": T0})
    forbidden = re.compile(
        r'"(password|passwordHash|password_hash|hash|salt|apiKeyHash|api_key_hash|keyHash)"\s*:',
        re.I,
    )
    responses = [
        await client.post(
            "/api/auth/login", json={"username": admin.username, "password": admin.password}
        ),
        await client.get("/api/series"),
        await client.get("/api/sensors", headers=auth),
        await client.get(f"/api/sensors/{sensor['id']}", headers=auth),
        await client.get(f"/api/measurements?series={series['id']}"),
    ]
    for response in responses:
        assert not forbidden.search(response.text)
        assert admin.password not in response.text
        assert key not in response.text


@official("CORS nie wpuszcza")
async def test_bezpieczenstwo_07_cors_does_not_reflect_an_arbitrary_origin(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/api/series", headers={"Origin": "https://evil.example"})
    allow_origin = response.headers.get("access-control-allow-origin")
    credentials = response.headers.get("access-control-allow-credentials", "").lower() == "true"
    assert not (credentials and allow_origin in ("https://evil.example", "*"))
    assert allow_origin is None


@official("nie zawierają śladów stosu")
async def test_bezpieczenstwo_08_error_bodies_have_no_stack_traces(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    trace = re.compile(
        r'(\n\s+at .+:\d+:\d+)|Traceback \(most recent|Exception in thread|"stack"\s*:', re.I
    )
    first = await client.get("/api/series/999999999")
    second = await client.post(
        "/api/series", content='{"name":', headers={**auth, "Content-Type": "application/json"}
    )
    third = await send(client, "x" * 40, {"value": "abc"})
    for response in (first, second, third):
        assert not trace.search(response.text)


# === the map itself ==========================================================================


_TEST_CALL = re.compile(r"^test\(\s*(['\"`])(\w+)\1\s*,\s*(['\"`])(.*?)\3\s*,", re.M)


def official_tests() -> list[tuple[str, str]]:
    source = OFFICIAL_SCRIPT.read_text(encoding="utf-8")
    parsed = [(m.group(2), m.group(4)) for m in _TEST_CALL.finditer(source)]
    # Every test(...) call must be understood: a quoting style the parser misses would
    # otherwise let a new official test slip through unmapped.
    assert len(parsed) == len(re.findall(r"^test\(", source, flags=re.M)), "unparsed test() call"
    return parsed


def test_the_official_script_is_the_version_that_was_ported() -> None:
    digest = hashlib.sha256(OFFICIAL_SCRIPT.read_bytes()).hexdigest()
    assert digest == OFFICIAL_SCRIPT_SHA256, (
        "contract/zai-tests.mjs changed: re-port the changed tests in this module "
        "(same requests, same assertions), then update OFFICIAL_SCRIPT_SHA256"
    )


def test_the_official_script_has_the_planned_groups_and_test_count() -> None:
    names = official_tests()
    assert {group for group, _ in names} == {"kontrakt", "walidacja", "bezpieczenstwo"}
    assert len(names) == 23 + 14 + 8  # plan §13


def test_every_official_test_is_mapped_exactly_once() -> None:
    """A new, renamed or removed official test fails here until the map above is updated."""
    keys = list(REGISTRY) + list(DEPLOY_ONLY)
    for _, name in official_tests():
        matches = [key for key in keys if key in name]
        assert len(matches) == 1, f"official test {name!r} matches {matches}"
    for key in keys:
        matching = [name for _, name in official_tests() if key in name]
        assert len(matching) == 1, f"key {key!r} matches official tests {matching}"
    assert len(REGISTRY) + len(DEPLOY_ONLY) == len(official_tests())


def test_every_registered_port_exists_in_this_module() -> None:
    module = globals()
    for function_name in REGISTRY.values():
        assert callable(module.get(function_name)), function_name
    assert set(DEPLOY_ONLY) and all(DEPLOY_ONLY.values())  # each carries its reason


@pytest.mark.parametrize("key", sorted(DEPLOY_ONLY))
def test_deploy_only_checks_are_the_https_and_spa_ones(key: str) -> None:
    assert any(word in key for word in ("HTTPS", "https", "Content-Security-Policy"))
