"""A small stateful stand-in for the ZAI API (series, sensors, measurements, admin login)."""

import json
from typing import Any

import httpx

ADMIN_TOKEN = "admin-token"


class FakeApi:
    def __init__(self, *, password: str = "pw") -> None:
        self.password = password
        self.series: list[dict[str, Any]] = []
        self.sensors: dict[str, int] = {}  # key -> series id
        self.measurements: list[dict[str, Any]] = []
        self.logged_out = False
        self.calls: list[tuple[str, str]] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def count(self, series_id: int) -> int:
        return sum(1 for m in self.measurements if m["seriesId"] == series_id)

    def _admin(self, request: httpx.Request) -> bool:
        return bool(request.headers.get("authorization") == f"Bearer {ADMIN_TOKEN}")

    def handle(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        self.calls.append((method, path))
        body = json.loads(request.content) if request.content else {}
        if (method, path) == ("POST", "/api/auth/login"):
            if body.get("password") != self.password:
                return httpx.Response(
                    401, json={"title": "Unauthorized", "detail": "Invalid credentials."}
                )
            return httpx.Response(200, json={"accessToken": ADMIN_TOKEN, "tokenType": "Bearer"})
        if (method, path) == ("POST", "/api/auth/logout"):
            self.logged_out = True
            return httpx.Response(204)
        if (method, path) == ("GET", "/api/series"):
            return httpx.Response(200, json=self.series)
        if (method, path) == ("POST", "/api/series"):
            if not self._admin(request):
                return httpx.Response(401, json={"title": "Unauthorized"})
            item = {"id": len(self.series) + 1, **body}
            self.series.append(item)
            return httpx.Response(201, json=item)
        if method == "GET" and path.startswith("/api/series/"):
            parts = path.split("/")
            found: dict[str, Any] | None = next(
                (s for s in self.series if str(s["id"]) == parts[3]), None
            )
            if found is None:
                return httpx.Response(404, json={"title": "Not Found"})
            if len(parts) == 5:
                return httpx.Response(200, json={"count": self.count(found["id"])})
            return httpx.Response(200, json=found)
        if (method, path) == ("POST", "/api/sensors"):
            key = f"zai_key_{len(self.sensors) + 1}"
            self.sensors[key] = body["seriesId"]
            return httpx.Response(201, json={"id": len(self.sensors), "apiKey": key, **body})
        if (method, path) == ("POST", "/api/measurements"):
            series_id = self.sensors.get(request.headers.get("x-api-key", ""))
            if series_id is None:
                return httpx.Response(401, json={"title": "Unauthorized"})
            series = self.series[series_id - 1]
            if not series["minValue"] <= body["value"] <= series["maxValue"]:
                return httpx.Response(422, json={"title": "Out of range", "detail": "out of range"})
            stored = {"id": len(self.measurements) + 1, "seriesId": series_id, **body}
            self.measurements.append(stored)
            return httpx.Response(201, json=stored)
        return httpx.Response(404, json={"title": "Not Found"})
