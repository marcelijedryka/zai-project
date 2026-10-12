"""Real Warsaw data from Open-Meteo (no API key).

* temperature: the forecast API (``hourly=temperature_2m``, ``past_days`` back to 92 days);
* PM2.5 / PM10 / NO2: the air-quality API (modelled values, often ``null`` for the latest hours).

Open-Meteo returns *naive* local times (``2026-10-11T12:00``) even with ``timezone=UTC``; they
are converted to aware UTC here, ``null`` values are skipped and hours later than now are dropped
(the server rejects timestamps more than 5 minutes ahead).
"""

import math
from datetime import UTC, datetime
from typing import Any

import httpx

from zai_sensor.models import Point

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
AIR_QUALITY_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
MAX_PAST_DAYS = 92  # Open-Meteo's limit for ``past_days``
WARSAW = (52.2297, 21.0122)

VARIABLES = {
    "temperature_2m": FORECAST_URL,
    "pm2_5": AIR_QUALITY_URL,
    "pm10": AIR_QUALITY_URL,
    "nitrogen_dioxide": AIR_QUALITY_URL,
}


class SourceError(Exception):
    """Open-Meteo was unreachable or answered with something unusable."""


def _url(variable: str) -> str:
    try:
        return VARIABLES[variable]
    except KeyError:
        known = ", ".join(sorted(VARIABLES))
        raise SourceError(f"unknown Open-Meteo variable (use one of: {known})") from None


def parse_time(raw: object) -> datetime:
    """``"2026-10-11T12:00"`` (naive, UTC as requested) or with an offset -> aware UTC."""
    if not isinstance(raw, str):
        raise SourceError("a time value is not a string")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        raise SourceError("a time value is not an ISO 8601 timestamp") from None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _number(raw: object) -> float | None:
    if isinstance(raw, bool) or not isinstance(raw, int | float) or not math.isfinite(raw):
        return None
    return float(raw)


def parse_hourly(body: dict[str, Any], variable: str, now: datetime) -> list[Point]:
    """Hourly points oldest first: nulls skipped, hours after ``now`` dropped."""
    try:
        times = body["hourly"]["time"]
        values = body["hourly"][variable]
    except (KeyError, TypeError):
        raise SourceError(f"the response has no hourly data for {variable}") from None
    if not isinstance(times, list) or not isinstance(values, list) or len(times) != len(values):
        raise SourceError("the hourly time and value lists do not match")
    points = []
    for raw_time, raw_value in zip(times, values, strict=True):
        value = _number(raw_value)
        if value is None:
            continue
        when = parse_time(raw_time)
        if when <= now:
            points.append(Point(value, when))
    return sorted(points, key=lambda p: p.timestamp or now)


def parse_current(body: dict[str, Any], variable: str) -> Point | None:
    """The ``current`` observation, or ``None`` when its value is null. Sent without a timestamp
    (server time): the observation updates only every ~15 minutes, so its own time would repeat."""
    current = body.get("current")
    if not isinstance(current, dict):
        raise SourceError(f"the response has no current data for {variable}")
    value = _number(current.get(variable))
    return None if value is None else Point(value)


class OpenMeteo:
    def __init__(
        self,
        *,
        latitude: float = WARSAW[0],
        longitude: float = WARSAW[1],
        timeout: float = 20.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._latitude, self._longitude = latitude, longitude
        self._http = client or httpx.Client(timeout=timeout, headers={"User-Agent": "zai-sensor"})

    def close(self) -> None:
        self._http.close()

    def _get(self, variable: str, params: dict[str, Any]) -> dict[str, Any]:
        query = {
            "latitude": self._latitude,
            "longitude": self._longitude,
            "timezone": "UTC",
            **params,
        }
        try:
            response = self._http.get(_url(variable), params=query)
        except httpx.HTTPError as exc:
            raise SourceError(f"Open-Meteo is unreachable ({type(exc).__name__})") from exc
        if response.status_code != 200:
            try:
                reason = str(response.json().get("reason", ""))
            except (ValueError, AttributeError):
                reason = ""
            raise SourceError(f"Open-Meteo answered HTTP {response.status_code}: {reason}".strip())
        try:
            body = response.json()
        except ValueError:
            raise SourceError("Open-Meteo returned invalid JSON") from None
        if not isinstance(body, dict):
            raise SourceError("Open-Meteo returned an unexpected document")
        return body

    def hourly(self, variable: str, *, past_days: int, now: datetime | None = None) -> list[Point]:
        if not 0 <= past_days <= MAX_PAST_DAYS:
            raise SourceError(f"past days must be between 0 and {MAX_PAST_DAYS}")
        body = self._get(
            variable,
            {"hourly": variable, "past_days": past_days, "forecast_days": 1},
        )
        return parse_hourly(body, variable, now or datetime.now(UTC))

    def current(self, variable: str) -> Point | None:
        return parse_current(self._get(variable, {"current": variable}), variable)
