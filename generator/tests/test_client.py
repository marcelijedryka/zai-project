import httpx
import pytest
import respx

from zai_sensor.client import ApiClient, ApiError, parse_problem
from zai_sensor.models import Kind, Point

URL = "http://api.test"
MEASUREMENTS = f"{URL}/api/measurements"
KEY = "zai_secret-key-value"
PROBLEM = {"title": "Unprocessable", "status": 422, "detail": "value 612 outside [0, 500]"}


def make(retries: int = 3, log: list[str] | None = None) -> ApiClient:
    return ApiClient(URL, retries=retries, log=(log.append if log is not None else None))


@respx.mock
def test_201_is_accepted_and_carries_the_id_and_series() -> None:
    route = respx.post(MEASUREMENTS).mock(
        return_value=httpx.Response(
            201, json={"id": 7, "seriesId": 3, "value": 1.5, "timestamp": "2026-10-11T12:00:00Z"}
        )
    )
    outcome = make().post_measurement(KEY, Point(1.5))
    assert (outcome.kind, outcome.measurement_id, outcome.series_id) == (Kind.ACCEPTED, 7, 3)
    request = route.calls.last.request
    assert request.headers["x-api-key"] == KEY
    assert request.content == b'{"value":1.5}'  # no timestamp: the server's clock is used


@respx.mock
def test_timestamps_are_sent_as_utc_with_z() -> None:
    from datetime import UTC, datetime

    route = respx.post(MEASUREMENTS).mock(return_value=httpx.Response(201, json={"id": 1}))
    make().post_measurement(KEY, Point(2, datetime(2026, 10, 1, 8, 30, tzinfo=UTC)))
    assert b'"timestamp":"2026-10-01T08:30:00Z"' in route.calls.last.request.content


@respx.mock
def test_422_is_rejected_with_the_problem_detail_and_never_retried() -> None:
    route = respx.post(MEASUREMENTS).mock(
        return_value=httpx.Response(
            422, json=PROBLEM, headers={"content-type": "application/problem+json"}
        )
    )
    outcome = make().post_measurement(KEY, Point(612))
    assert (outcome.kind, outcome.status, outcome.message) == (
        Kind.REJECTED,
        422,
        "value 612 outside [0, 500]",
    )
    assert route.call_count == 1


@respx.mock
def test_401_is_key_rejected_and_never_retried() -> None:
    route = respx.post(MEASUREMENTS).mock(
        return_value=httpx.Response(401, json={"title": "Unauthorized"})
    )
    assert make().post_measurement(KEY, Point(1)).kind is Kind.KEY_REJECTED
    assert route.call_count == 1


@respx.mock
def test_429_honours_retry_after_then_succeeds(no_sleeping: list[float]) -> None:
    route = respx.post(MEASUREMENTS).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(201, json={"id": 1}),
        ]
    )
    assert make().post_measurement(KEY, Point(1)).kind is Kind.ACCEPTED
    assert route.call_count == 2
    assert no_sleeping == [7.0]


@respx.mock
def test_503_without_retry_after_backs_off_exponentially(no_sleeping: list[float]) -> None:
    respx.post(MEASUREMENTS).mock(
        side_effect=[httpx.Response(503), httpx.Response(503), httpx.Response(201, json={"id": 1})]
    )
    assert make().post_measurement(KEY, Point(1)).kind is Kind.ACCEPTED
    assert no_sleeping == [0.5, 1.0]


@respx.mock
def test_retry_after_is_capped(no_sleeping: list[float]) -> None:
    respx.post(MEASUREMENTS).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "86400"}),
            httpx.Response(201, json={"id": 1}),
        ]
    )
    make().post_measurement(KEY, Point(1))
    assert no_sleeping == [60.0]


@respx.mock
def test_retries_are_bounded_then_reported_as_failed() -> None:
    route = respx.post(MEASUREMENTS).mock(return_value=httpx.Response(503, json={"title": "Busy"}))
    outcome = make(retries=2).post_measurement(KEY, Point(1))
    assert (outcome.kind, outcome.status) == (Kind.FAILED, 503)
    assert route.call_count == 3


@respx.mock
def test_connect_errors_are_retried(no_sleeping: list[float]) -> None:
    route = respx.post(MEASUREMENTS).mock(
        side_effect=[httpx.ConnectError("refused"), httpx.Response(201, json={"id": 1})]
    )
    assert make().post_measurement(KEY, Point(1)).kind is Kind.ACCEPTED
    assert route.call_count == 2
    assert no_sleeping == [0.5]


@respx.mock
def test_unreachable_api_fails_after_the_retries() -> None:
    route = respx.post(MEASUREMENTS).mock(side_effect=httpx.ConnectError("refused"))
    outcome = make(retries=1).post_measurement(KEY, Point(1))
    assert outcome.kind is Kind.FAILED
    assert "cannot reach the API" in outcome.message
    assert route.call_count == 2


@respx.mock
@pytest.mark.parametrize("error", [httpx.ReadTimeout("slow"), httpx.RemoteProtocolError("cut")])
def test_errors_after_sending_are_not_retried_the_value_may_be_stored(error: Exception) -> None:
    route = respx.post(MEASUREMENTS).mock(side_effect=error)
    outcome = make().post_measurement(KEY, Point(1))
    assert outcome.kind is Kind.FAILED
    assert route.call_count == 1


@respx.mock
@pytest.mark.parametrize("status", [400, 404, 500, 502, 504])
def test_other_statuses_fail_without_retrying(status: int) -> None:
    route = respx.post(MEASUREMENTS).mock(return_value=httpx.Response(status, text="oops"))
    outcome = make().post_measurement(KEY, Point(1))
    assert (outcome.kind, outcome.status) == (Kind.FAILED, status)
    assert route.call_count == 1


@respx.mock
def test_the_key_never_appears_in_the_log() -> None:
    lines: list[str] = []
    respx.post(MEASUREMENTS).mock(
        side_effect=[httpx.Response(503), httpx.Response(201, json={"id": 1})]
    )
    make(log=lines).post_measurement(KEY, Point(1))
    assert lines
    assert not any(KEY in line for line in lines)


def test_problem_parsing_handles_non_problem_bodies() -> None:
    assert parse_problem(httpx.Response(502, text="<html>bad gateway</html>")).text == "Bad Gateway"
    assert parse_problem(httpx.Response(500, json=[1])).status == 500
    problem = parse_problem(
        httpx.Response(
            409,
            json={
                "title": "Conflict",
                "detail": "range",
                "errors": [{"field": "x", "message": "m"}],
                "dataMin": 1,
            },
        )
    )
    assert (problem.detail, problem.errors, problem.extra) == (
        "range",
        [{"field": "x", "message": "m"}],
        {"dataMin": 1},
    )


@respx.mock
def test_admin_calls_raise_api_error_with_the_problem_detail() -> None:
    respx.post(f"{URL}/api/auth/login").mock(
        return_value=httpx.Response(
            401, json={"title": "Unauthorized", "detail": "Invalid credentials."}
        )
    )
    with pytest.raises(ApiError, match="Invalid credentials") as raised:
        make().login("admin", "wrong")
    assert raised.value.status == 401


@respx.mock
def test_admin_transport_errors_become_api_errors() -> None:
    respx.get(f"{URL}/api/series").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(ApiError, match="cannot reach"):
        make().list_series()


@respx.mock
def test_series_unit_is_best_effort() -> None:
    respx.get(f"{URL}/api/series/3").mock(
        return_value=httpx.Response(200, json={"id": 3, "unit": "°C"})
    )
    respx.get(f"{URL}/api/series/4").mock(
        return_value=httpx.Response(404, json={"title": "Not Found"})
    )
    assert make().series_unit(3) == "°C"
    assert make().series_unit(4) == ""
