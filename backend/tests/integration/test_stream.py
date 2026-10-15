"""``GET /api/measurements/stream`` against a real server (ASGITransport buffers whole responses,
so it cannot test an endless stream). Every read has a timeout: a bug must fail, not hang."""

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from sse_starlette.sse import AppStatus

from app.realtime.notify import MEASUREMENTS_CHANNEL
from tests.conftest import Admin

STREAM = "/api/measurements/stream"
HEARTBEAT = 0.3
CAP = 3


@pytest.fixture
def server_options() -> dict[str, Any]:
    return {"sse_heartbeat_seconds": HEARTBEAT, "sse_max_streams_per_client": CAP}


@dataclass
class Event:
    event: str | None = None
    id: str | None = None
    data: str | None = None
    comment: str | None = None

    def json(self) -> Any:
        assert self.data is not None
        return json.loads(self.data)


async def events(response: httpx.Response) -> AsyncIterator[Event]:
    """Parse the SSE wire format: blank line = end of event, ``:`` = comment."""
    current = Event()
    async for line in response.aiter_lines():
        if line == "":
            if current != Event():
                yield current
            current = Event()
        elif line.startswith(":"):
            yield Event(comment=line[1:].strip())
        else:
            name, _, value = line.partition(":")
            setattr(current, name, value.removeprefix(" "))


async def next_event(
    stream: AsyncIterator[Event], *, seconds: float = 5, comments: bool = False
) -> Event:
    async with asyncio.timeout(seconds):
        while True:
            event = await anext(stream)
            if comments or event.comment is None:
                return event


@asynccontextmanager
async def open_stream(url: str, query: str = "") -> AsyncIterator[AsyncIterator[Event]]:
    async with (
        httpx.AsyncClient(base_url=url, timeout=httpx.Timeout(10, read=10)) as client,
        client.stream("GET", STREAM + query, headers={"Accept": "text/event-stream"}) as response,
    ):
        assert response.status_code == 200, await response.aread()
        yield events(response)


@dataclass
class World:
    url: str
    headers: dict[str, str]
    series: list[dict[str, Any]]
    keys: list[str]

    async def post(self, index: int, value: float, **extra: Any) -> httpx.Response:
        async with httpx.AsyncClient(base_url=self.url) as client:
            return await client.post(
                "/api/measurements",
                json={"value": value, **extra},
                headers={"X-API-Key": self.keys[index]},
            )


@pytest.fixture
async def world(server: str, admin: Admin) -> World:
    async with httpx.AsyncClient(base_url=server) as client:
        login = await client.post(
            "/api/auth/login", json={"username": admin.username, "password": admin.password}
        )
        headers = {"Authorization": f"Bearer {login.json()['accessToken']}"}
        series, keys = [], []
        for name in ("Stream A", "Stream B"):
            created = await client.post(
                "/api/series",
                json={"name": name, "minValue": 0, "maxValue": 100, "color": "#112233"},
                headers=headers,
            )
            series.append(created.json())
            sensor = await client.post(
                "/api/sensors",
                json={"name": f"s {name}", "seriesId": series[-1]["id"]},
                headers=headers,
            )
            keys.append(sensor.json()["apiKey"])
    return World(server, headers, series, keys)


async def test_a_stored_measurement_arrives_live_in_exactly_the_rest_shape(world: World) -> None:
    async with open_stream(world.url) as stream:
        # Series creation events (from the fixture) happened before we subscribed: none queued.
        posted = await world.post(0, 21.5, timestamp="2026-10-01T10:30:00+02:00")
        assert posted.status_code == 201
        event = await next_event(stream)
        async with httpx.AsyncClient(base_url=world.url) as client:
            rest = (await client.get(posted.headers["location"])).json()
    assert event.event == "measurement"
    assert event.id == str(rest["id"])
    assert event.json() == rest  # same fields, same UTC timestamp format
    assert event.json()["timestamp"] == "2026-10-01T08:30:00Z"


async def test_the_response_headers_suit_a_proxied_event_stream(world: World) -> None:
    async with (
        httpx.AsyncClient(base_url=world.url) as client,
        client.stream("GET", STREAM, headers={"Accept": "text/event-stream"}) as response,
    ):
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-cache"
        assert response.headers["x-accel-buffering"] == "no"
        assert response.headers["x-content-type-options"] == "nosniff"


async def test_a_credentialed_stream_request_keeps_the_stream_cache_header(world: World) -> None:
    async with (
        httpx.AsyncClient(base_url=world.url) as client,
        client.stream(
            "GET", STREAM, headers={"Accept": "text/event-stream", **world.headers}
        ) as response,
    ):
        assert response.headers["cache-control"] == "no-cache"  # not overwritten by no-store


async def test_the_series_filter_only_lets_those_series_through(world: World) -> None:
    a, b = world.series
    async with (
        open_stream(world.url, f"?series={a['id']}") as only_a,
        open_stream(world.url) as everything,
    ):
        await world.post(1, 10)  # series B
        await world.post(0, 20)  # series A
        first_all, second_all = await next_event(everything), await next_event(everything)
        only = await next_event(only_a)
    assert [first_all.json()["seriesId"], second_all.json()["seriesId"]] == [b["id"], a["id"]]
    assert (only.json()["seriesId"], only.json()["value"]) == (a["id"], 20)


async def test_series_changes_are_announced_with_id_and_action(world: World) -> None:
    async with open_stream(world.url) as stream, httpx.AsyncClient(base_url=world.url) as client:
        created = (
            await client.post(
                "/api/series",
                json={"name": "Stream C", "minValue": 0, "maxValue": 1, "color": "#445566"},
                headers=world.headers,
            )
        ).json()
        updated = await client.put(
            f"/api/series/{created['id']}",
            json={"name": "Stream C", "minValue": 0, "maxValue": 2, "color": "#445566"},
            headers=world.headers,
        )
        assert updated.status_code == 200
        await client.delete(f"/api/series/{created['id']}", headers=world.headers)
        got = [await next_event(stream) for _ in range(3)]
    assert [e.event for e in got] == ["series-changed"] * 3
    assert [e.json() for e in got] == [
        {"id": created["id"], "action": action} for action in ("created", "updated", "deleted")
    ]


async def test_rollbacks_and_rejected_measurements_emit_nothing(
    world: World, engine: AsyncEngine
) -> None:
    async with open_stream(world.url) as stream:
        async with engine.connect() as conn:  # a notification inside a transaction that rolls back
            await conn.execute(
                text("SELECT pg_notify(:c, :p)"),
                {
                    "c": MEASUREMENTS_CHANNEL,
                    "p": '{"id":1,"seriesId":1,"value":1,"timestamp":"2026-01-01T00:00:00Z"}',
                },
            )
            await conn.rollback()
        assert (await world.post(0, 999)).status_code == 422  # out of range: rejected
        assert (await world.post(0, 1, timestamp="2999-01-01T00:00:00Z")).status_code == 422
        assert (await world.post(0, 42)).status_code == 201  # the first thing that is real
        event = await next_event(stream)
    assert event.json()["value"] == 42


async def test_a_heartbeat_comment_keeps_an_idle_stream_alive(world: World) -> None:
    async with open_stream(world.url) as stream:
        first = await next_event(stream, seconds=3, comments=True)
        second = await next_event(stream, seconds=3, comments=True)
    assert first.comment == "ping"
    assert second.comment == "ping"


async def test_a_bad_series_filter_is_a_400_problem(server: str) -> None:
    async with httpx.AsyncClient(base_url=server) as client:
        response = await client.get(STREAM + "?series=a,b", headers={"Accept": "text/event-stream"})
    assert response.status_code == 400
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["errors"][0]["field"] == "series"


async def test_a_json_accept_header_is_a_406(server: str) -> None:
    async with httpx.AsyncClient(base_url=server) as client:
        response = await client.get(STREAM, headers={"Accept": "application/json"})
    assert response.status_code == 406


async def test_the_stream_is_public(server: str) -> None:
    async with open_stream(server):
        pass  # no credentials were sent


async def test_the_per_client_cap_gives_429_and_a_closed_stream_frees_its_slot(
    server: str, server_app: FastAPI
) -> None:
    broadcaster = server_app.state.broadcaster
    async with open_stream(server), open_stream(server):
        async with open_stream(server):
            async with httpx.AsyncClient(base_url=server) as client:
                refused = await client.get(STREAM, headers={"Accept": "text/event-stream"})
            assert refused.status_code == 429
            assert refused.headers["content-type"].startswith("application/problem+json")
            assert refused.headers["retry-after"] == "5"
        await wait_for(lambda: broadcaster.subscriber_count == 2)  # the third closed
        async with open_stream(server):  # its slot is free again
            assert broadcaster.subscriber_count == 3
    await wait_for(lambda: broadcaster.subscriber_count == 0)


async def ends(stream: AsyncIterator[Event], *, seconds: float) -> None:
    """Consume the stream until the server ends it; fail if it is still open after ``seconds``."""
    async with asyncio.timeout(seconds):
        async for _ in stream:
            pass


async def wait_for(condition: Any, seconds: float = 5) -> None:
    async with asyncio.timeout(seconds):
        while not condition():  # noqa: ASYNC110 - polls state owned by the server thread
            await asyncio.sleep(0.02)


async def test_a_disconnecting_client_is_cleaned_up(server: str, server_app: FastAPI) -> None:
    broadcaster = server_app.state.broadcaster
    async with open_stream(server):
        assert broadcaster.subscriber_count == 1
    await wait_for(lambda: broadcaster.subscriber_count == 0)


async def test_open_streams_hold_no_database_connection(
    server: str, server_app: FastAPI, world: World
) -> None:
    pool = server_app.state.engine.pool
    async with open_stream(server), open_stream(server), open_stream(server):
        async with httpx.AsyncClient(base_url=server) as client:
            assert (await client.get("/api/series")).status_code == 200
        # The request's own session is released just after its response; streams never take one.
        await wait_for(lambda: pool.checkedout() == 0, seconds=3)


async def test_a_dropped_listener_connection_ends_streams_then_recovers(
    world: World, engine: AsyncEngine, server_app: FastAPI
) -> None:
    async with open_stream(world.url) as stream:
        async with engine.begin() as conn:  # kill the broadcaster's LISTEN connection
            killed = await conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE query LIKE 'LISTEN%' AND datname = current_database()"
                )
            )
            assert killed.rowcount >= 1
        await ends(stream, seconds=5)  # nothing fake in between: the stream just ends
    await wait_for(lambda: server_app.state.broadcaster.is_connected, seconds=10)
    async with open_stream(world.url) as fresh:
        await world.post(0, 7)
        assert (await next_event(fresh)).json()["value"] == 7


async def test_shutdown_ends_open_streams_instead_of_hanging(
    world: World, server_app: FastAPI
) -> None:
    instance = server_app.state.uvicorn_server
    try:
        async with open_stream(world.url) as stream:
            AppStatus.should_exit = True  # what uvicorn's SIGTERM/SIGINT handler sets
            instance.should_exit = True
            # sse-starlette just stops writing on shutdown (no final chunk): the client sees either
            # a clean end or an incomplete body, and EventSource reconnects in both cases.
            with contextlib.suppress(httpx.RemoteProtocolError):
                await ends(stream, seconds=10)
    finally:
        AppStatus.should_exit = False
