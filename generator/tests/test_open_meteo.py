from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx

from zai_sensor.sources.open_meteo import (
    AIR_QUALITY_URL,
    FORECAST_URL,
    OpenMeteo,
    SourceError,
    parse_hourly,
    parse_time,
)

NOW = datetime(2026, 10, 11, 12, 30, tzinfo=UTC)


def hourly(variable: str, times: Any, values: list[Any]) -> dict[str, Any]:
    return {"hourly": {"time": times, variable: values}}


def test_naive_times_become_aware_utc() -> None:
    assert parse_time("2026-10-11T12:00") == datetime(2026, 10, 11, 12, tzinfo=UTC)


def test_times_with_an_offset_are_converted_to_utc() -> None:
    assert parse_time("2026-10-11T14:00+02:00") == datetime(2026, 10, 11, 12, tzinfo=UTC)


@pytest.mark.parametrize("raw", [None, 5, "not a time", ""])
def test_unparseable_times_are_a_source_error(raw: object) -> None:
    with pytest.raises(SourceError):
        parse_time(raw)


def test_nulls_are_skipped_future_hours_dropped_and_points_sorted() -> None:
    body = hourly(
        "pm10",
        ["2026-10-11T12:00", "2026-10-11T10:00", "2026-10-11T11:00", "2026-10-11T13:00"],
        [20.5, 18, None, 99.0],  # 11:00 is null, 13:00 is after "now" (12:30)
    )
    points = parse_hourly(body, "pm10", NOW)
    assert [(p.timestamp, p.value) for p in points] == [
        (datetime(2026, 10, 11, 10, tzinfo=UTC), 18.0),
        (datetime(2026, 10, 11, 12, tzinfo=UTC), 20.5),
    ]
    assert all(p.timestamp is not None and p.timestamp.tzinfo is not None for p in points)


def test_non_numbers_are_skipped() -> None:
    body = hourly(
        "pm10",
        ["2026-10-11T10:00", "2026-10-11T11:00", "2026-10-11T09:00"],
        ["x", True, float("inf")],
    )
    assert parse_hourly(body, "pm10", NOW) == []


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"hourly": None},
        {"hourly": {"time": ["2026-10-11T10:00"]}},
        hourly("pm10", ["2026-10-11T10:00"], [1, 2]),
        hourly("pm10", "nope", [1]),
    ],
)
def test_malformed_hourly_documents_are_a_source_error(body: dict[str, Any]) -> None:
    with pytest.raises(SourceError):
        parse_hourly(body, "pm10", NOW)


@respx.mock
def test_temperature_uses_the_forecast_api_with_utc_and_past_days() -> None:
    route = respx.get(FORECAST_URL).mock(
        return_value=httpx.Response(
            200, json=hourly("temperature_2m", ["2026-10-11T11:00"], [12.3])
        )
    )
    points = OpenMeteo().hourly("temperature_2m", past_days=14, now=NOW)
    assert [p.value for p in points] == [12.3]
    query = route.calls.last.request.url.params
    assert query["hourly"] == "temperature_2m"
    assert query["past_days"] == "14"
    assert query["timezone"] == "UTC"
    assert (query["latitude"], query["longitude"]) == ("52.2297", "21.0122")


@respx.mock
@pytest.mark.parametrize("variable", ["pm2_5", "pm10", "nitrogen_dioxide"])
def test_air_quality_variables_use_the_air_quality_api(variable: str) -> None:
    route = respx.get(AIR_QUALITY_URL).mock(
        return_value=httpx.Response(200, json=hourly(variable, ["2026-10-11T11:00"], [7.0]))
    )
    assert OpenMeteo().hourly(variable, past_days=1, now=NOW)[0].value == 7.0
    assert route.calls.last.request.url.params["hourly"] == variable


@respx.mock
def test_current_value_has_no_timestamp_so_the_server_clock_is_used() -> None:
    respx.get(FORECAST_URL).mock(
        return_value=httpx.Response(
            200, json={"current": {"time": "2026-10-11T12:15", "temperature_2m": 9.5}}
        )
    )
    point = OpenMeteo().current("temperature_2m")
    assert point is not None
    assert (point.value, point.timestamp) == (9.5, None)


@respx.mock
def test_null_current_value_is_none() -> None:
    respx.get(AIR_QUALITY_URL).mock(
        return_value=httpx.Response(200, json={"current": {"time": "x", "pm10": None}})
    )
    assert OpenMeteo().current("pm10") is None


@respx.mock
def test_http_errors_carry_the_reason() -> None:
    respx.get(FORECAST_URL).mock(
        return_value=httpx.Response(400, json={"error": True, "reason": "past_days out of range"})
    )
    with pytest.raises(SourceError, match="HTTP 400: past_days out of range"):
        OpenMeteo().hourly("temperature_2m", past_days=5, now=NOW)


@respx.mock
def test_network_failures_and_bad_json_are_source_errors() -> None:
    respx.get(FORECAST_URL).mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(SourceError, match="unreachable"):
        OpenMeteo().hourly("temperature_2m", past_days=1, now=NOW)
    respx.get(FORECAST_URL).mock(return_value=httpx.Response(200, text="<html>"))
    with pytest.raises(SourceError, match="invalid JSON"):
        OpenMeteo().hourly("temperature_2m", past_days=1, now=NOW)
    respx.get(FORECAST_URL).mock(return_value=httpx.Response(200, json=[1, 2]))
    with pytest.raises(SourceError, match="unexpected"):
        OpenMeteo().hourly("temperature_2m", past_days=1, now=NOW)


def test_unknown_variable_and_past_days_limit_never_reach_the_network() -> None:
    meteo = OpenMeteo()
    with pytest.raises(SourceError, match="unknown Open-Meteo variable"):
        meteo.hourly("wind", past_days=1)
    with pytest.raises(SourceError, match="between 0 and 92"):
        meteo.hourly("temperature_2m", past_days=93)
