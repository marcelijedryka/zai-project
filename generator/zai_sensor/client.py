"""HTTP client for the ZAI API: Problem Details parsing, retry policy, admin helpers.

Retry rules for ``POST /api/measurements`` (the API has no idempotency key, so a blind retry of
a request that may have been stored would create a duplicate):

* retried: connection errors (the request never reached the server), 429 and 503 (both are
  refused before anything is stored); ``Retry-After`` is honoured, otherwise exponential backoff;
* never retried: read timeouts and other transport errors after sending, any other status.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from zai_sensor.models import Kind, Outcome, Point


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


MAX_BACKOFF_SECONDS = 30.0
MAX_RETRY_AFTER_SECONDS = 60.0
RETRYABLE_STATUSES = frozenset({429, 503})


@dataclass(frozen=True)
class Problem:
    """An RFC 9457 problem document (or a stand-in for a non-problem error response)."""

    status: int
    title: str = ""
    detail: str = ""
    errors: list[dict[str, str]] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return self.detail or self.title or f"HTTP {self.status}"


def parse_problem(response: httpx.Response) -> Problem:
    try:
        body = response.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        return Problem(response.status_code, title=response.reason_phrase)
    known = {"type", "title", "status", "detail", "instance", "errors"}
    errors = body.get("errors")
    return Problem(
        response.status_code,
        title=str(body.get("title") or ""),
        detail=str(body.get("detail") or ""),
        errors=[e for e in errors if isinstance(e, dict)] if isinstance(errors, list) else [],
        extra={k: v for k, v in body.items() if k not in known},
    )


class ApiError(Exception):
    """A non-2xx response to an admin call, or a transport failure."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        return min(max(float(raw), 0.0), MAX_RETRY_AFTER_SECONDS)
    except ValueError:
        return None  # an HTTP-date: fall back to the exponential backoff


class ApiClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 10.0,
        retries: int = 3,
        sleep: Callable[[float], None] | None = None,
        log: Callable[[str], None] | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
            headers={"Accept": "application/json", "User-Agent": "zai-sensor"},
        )
        self._retries = retries
        self._sleep = sleep or _sleep  # looked up here so tests can patch ``_sleep``
        self._log = log or (lambda _line: None)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "ApiClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # --- sensor side -------------------------------------------------------------------------

    def post_measurement(self, api_key: str, point: Point) -> Outcome:
        body: dict[str, Any] = {"value": point.value}
        if point.timestamp is not None:
            body["timestamp"] = point.timestamp.isoformat().replace("+00:00", "Z")
        attempt = 0
        while True:
            try:
                self._log("POST /api/measurements")
                response = self._http.post(
                    "/api/measurements", json=body, headers={"X-API-Key": api_key}
                )
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                # Nothing was sent: safe to try again.
                if attempt < self._retries:
                    self._backoff(attempt, None, f"connection failed ({type(exc).__name__})")
                    attempt += 1
                    continue
                return Outcome(Kind.FAILED, message=f"cannot reach the API: {exc}")
            except httpx.TimeoutException:
                return Outcome(
                    Kind.FAILED,
                    message="timed out after sending; the value may have been stored",
                )
            except httpx.TransportError as exc:
                return Outcome(
                    Kind.FAILED,
                    message=f"connection lost after sending ({type(exc).__name__})",
                )
            self._log(f"-> {response.status_code}")
            if response.status_code in RETRYABLE_STATUSES and attempt < self._retries:
                self._backoff(attempt, response, f"HTTP {response.status_code}")
                attempt += 1
                continue
            return self._outcome(response)

    def _backoff(self, attempt: int, response: httpx.Response | None, why: str) -> None:
        delay = retry_after_seconds(response) if response is not None else None
        if delay is None:
            delay = min(0.5 * 2**attempt, MAX_BACKOFF_SECONDS)
        self._log(f"{why}; retrying in {delay:g}s ({attempt + 1}/{self._retries})")
        self._sleep(delay)

    @staticmethod
    def _outcome(response: httpx.Response) -> Outcome:
        status = response.status_code
        if status == 201:
            body = response.json()
            return Outcome(
                Kind.ACCEPTED,
                status,
                measurement_id=body.get("id"),
                series_id=body.get("seriesId"),
                timestamp=body.get("timestamp"),
            )
        problem = parse_problem(response)
        if status == 401:
            return Outcome(Kind.KEY_REJECTED, status, message=problem.text)
        if status == 422:
            return Outcome(Kind.REJECTED, status, message=problem.text)
        return Outcome(Kind.FAILED, status, message=f"HTTP {status}: {problem.text}")

    # --- admin side (seed) -------------------------------------------------------------------

    def _admin(
        self, method: str, path: str, token: str | None = None, **kwargs: Any
    ) -> httpx.Response:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            self._log(f"{method} {path}")
            response = self._http.request(method, path, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise ApiError(f"cannot reach the API: {exc}") from exc
        self._log(f"-> {response.status_code}")
        if response.status_code >= 400:
            problem = parse_problem(response)
            raise ApiError(f"{method} {path} failed: {problem.text}", response.status_code)
        return response

    def login(self, username: str, password: str) -> str:
        response = self._admin(
            "POST", "/api/auth/login", json={"username": username, "password": password}
        )
        token = response.json()["accessToken"]
        if not isinstance(token, str):
            raise ApiError("the login response has no access token")
        return token

    def logout(self, token: str) -> None:
        self._admin("POST", "/api/auth/logout", token)

    def list_series(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = self._admin("GET", "/api/series").json()
        return result

    def create_series(self, token: str, body: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = self._admin("POST", "/api/series", token, json=body).json()
        return result

    def series_unit(self, series_id: int) -> str:
        """The unit of a series (public endpoint); empty if unknown or unreachable."""
        try:
            unit = self._admin("GET", f"/api/series/{series_id}").json().get("unit")
        except ApiError:
            return ""
        return unit if isinstance(unit, str) else ""

    def series_stats(self, series_id: int) -> dict[str, Any]:
        result: dict[str, Any] = self._admin("GET", f"/api/series/{series_id}/stats").json()
        return result

    def create_sensor(self, token: str, name: str, series_id: int) -> dict[str, Any]:
        body = {"name": name, "seriesId": series_id}
        result: dict[str, Any] = self._admin("POST", "/api/sensors", token, json=body).json()
        return result
