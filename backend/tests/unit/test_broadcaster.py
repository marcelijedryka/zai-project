"""The broadcaster without a database: filtering, back-pressure, caps, reconnecting."""

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest

from app.realtime.broadcaster import (
    Broadcaster,
    StreamEvent,
    TooManyStreams,
    event_from_notification,
)
from app.realtime.notify import MEASUREMENTS_CHANNEL, SERIES_CHANNEL
from app.schemas.measurements import MeasurementOut

NOTIFICATION = {
    "event": "measurement",
    "id": 41,
    "seriesId": 7,
    "sensorId": 3,
    "value": 21.5,
    "timestamp": "2026-10-15T10:30:00+02:00",
}


def measurement_event(series_id: int, id_: int = 1) -> StreamEvent:
    return StreamEvent("measurement", "{}", series_id, str(id_))


async def never_connects() -> Any:
    raise OSError("no database in this test")


# --- translating notifications -------------------------------------------------------------------


def test_a_measurement_notification_becomes_exactly_the_rest_measurement_json() -> None:
    event = event_from_notification(MEASUREMENTS_CHANNEL, json.dumps(NOTIFICATION))
    assert event is not None
    assert (event.event, event.id, event.series_id) == ("measurement", "41", 7)
    rest = MeasurementOut(
        id=41,
        series_id=7,
        sensor_id=3,
        value=21.5,
        timestamp=datetime(2026, 10, 15, 8, 30, tzinfo=UTC),
    ).model_dump_json(by_alias=True)
    assert json.loads(event.data) == json.loads(rest)
    assert json.loads(event.data) == {
        "id": 41,
        "seriesId": 7,
        "sensorId": 3,
        "value": 21.5,
        "timestamp": "2026-10-15T08:30:00Z",  # UTC with Z, like the REST API (not the +02:00 input)
    }


def test_the_notify_event_key_is_not_forwarded() -> None:
    event = event_from_notification(MEASUREMENTS_CHANNEL, json.dumps(NOTIFICATION))
    assert event is not None
    assert "event" not in json.loads(event.data)


def test_a_missing_sensor_is_omitted_like_in_the_rest_api() -> None:
    body = {**NOTIFICATION, "sensorId": None}
    event = event_from_notification(MEASUREMENTS_CHANNEL, json.dumps(body))
    assert event is not None
    assert "sensorId" not in json.loads(event.data)


def test_a_series_notification_carries_only_id_and_action() -> None:
    payload = json.dumps({"event": "series-changed", "id": 5, "action": "deleted"})
    event = event_from_notification(SERIES_CHANNEL, payload)
    assert event == StreamEvent("series-changed", '{"id":5,"action":"deleted"}', 5)


@pytest.mark.parametrize(
    ("channel", "payload"),
    [
        (MEASUREMENTS_CHANNEL, "not json"),
        (MEASUREMENTS_CHANNEL, "{}"),
        (MEASUREMENTS_CHANNEL, json.dumps({**NOTIFICATION, "value": "x"})),
        (MEASUREMENTS_CHANNEL, json.dumps({**NOTIFICATION, "timestamp": "nope"})),
        (SERIES_CHANNEL, json.dumps({"id": 1})),
        ("some_other_channel", "{}"),
    ],
)
def test_unusable_notifications_are_ignored(channel: str, payload: str) -> None:
    assert event_from_notification(channel, payload) is None


# --- subscribers ---------------------------------------------------------------------------------


async def test_events_reach_matching_subscribers_only() -> None:
    hub = Broadcaster(never_connects)
    everyone = hub.subscribe(None, "a")
    only_seven = hub.subscribe(frozenset({7}), "b")
    only_eight = hub.subscribe(frozenset({8}), "c")
    hub.publish(measurement_event(7))
    assert everyone.queue.qsize() == 1
    assert only_seven.queue.qsize() == 1
    assert only_eight.queue.qsize() == 0


async def test_a_full_queue_drops_only_that_subscriber() -> None:
    hub = Broadcaster(never_connects, queue_size=3)
    slow, fast = hub.subscribe(None, "slow"), hub.subscribe(None, "fast")
    for n in range(3):
        hub.publish(measurement_event(1, n))
        fast.queue.get_nowait()  # the fast one keeps up
    assert hub.subscriber_count == 2
    hub.publish(measurement_event(1, 99))  # the slow queue is full
    assert hub.subscriber_count == 1
    assert slow.closed
    assert slow.queue.get_nowait() is None  # its stream is told to end (queued events dropped)
    assert (await fast.queue.get()) == measurement_event(1, 99)


async def test_the_per_client_cap_is_enforced_and_released() -> None:
    hub = Broadcaster(never_connects, max_streams_per_client=2)
    first = hub.subscribe(None, "10.0.0.1")
    hub.subscribe(None, "10.0.0.1")
    with pytest.raises(TooManyStreams):
        hub.subscribe(None, "10.0.0.1")
    hub.subscribe(None, "10.0.0.2")  # another client is not affected
    hub.unsubscribe(first)
    hub.unsubscribe(first)  # idempotent: must not release a second slot
    hub.subscribe(None, "10.0.0.1")
    with pytest.raises(TooManyStreams):
        hub.subscribe(None, "10.0.0.1")


async def test_stop_ends_every_stream_and_refuses_new_ones() -> None:
    hub = Broadcaster(never_connects)
    subs = [hub.subscribe(None, str(n)) for n in range(3)]
    await hub.stop()
    assert all(sub.queue.get_nowait() is None for sub in subs)
    assert hub.subscriber_count == 0
    with pytest.raises(TooManyStreams):
        hub.subscribe(None, "late")


# --- the listener connection ---------------------------------------------------------------------


class FakeConnection:
    """Just enough of an asyncpg connection: listeners, termination, close."""

    def __init__(self) -> None:
        self.listeners: dict[str, Callable[..., None]] = {}
        self.on_terminate: list[Callable[[Any], None]] = []
        self.closed = False

    def add_termination_listener(self, callback: Callable[[Any], None]) -> None:
        self.on_terminate.append(callback)

    async def add_listener(self, channel: str, callback: Callable[..., None]) -> None:
        self.listeners[channel] = callback

    def is_closed(self) -> bool:
        return self.closed

    async def close(self, timeout: float | None = None) -> None:  # noqa: ASYNC109
        self.closed = True

    def notify(self, channel: str, payload: dict[str, Any]) -> None:
        self.listeners[channel](self, 1, channel, json.dumps(payload))

    def drop(self) -> None:
        self.closed = True
        for callback in self.on_terminate:
            callback(self)


async def test_it_listens_on_both_channels_and_fans_out() -> None:
    connection = FakeConnection()

    async def connect() -> Any:
        return connection

    hub = Broadcaster(connect)
    await hub.start()
    await hub.wait_connected(2)
    sub = hub.subscribe(None, "a")
    connection.notify(MEASUREMENTS_CHANNEL, NOTIFICATION)
    connection.notify(SERIES_CHANNEL, {"event": "series-changed", "id": 7, "action": "updated"})
    assert set(connection.listeners) == {MEASUREMENTS_CHANNEL, SERIES_CHANNEL}
    assert [(await sub.queue.get()).event for _ in range(2)] == [  # type: ignore[union-attr]
        "measurement",
        "series-changed",
    ]
    await hub.stop()
    assert connection.closed  # the dedicated connection is closed on shutdown


async def test_it_reconnects_after_a_loss_and_ends_the_open_streams() -> None:
    connections: list[FakeConnection] = []
    failures = [True]  # the second attempt fails once: the backoff loop must retry

    async def connect() -> Any:
        if len(connections) == 1 and failures:
            failures.clear()
            raise OSError("still down")
        connections.append(FakeConnection())
        return connections[-1]

    hub = Broadcaster(connect, initial_backoff=0.01)
    await hub.start()
    await hub.wait_connected(2)
    sub = hub.subscribe(None, "a")
    connections[0].drop()
    assert await asyncio.wait_for(sub.queue.get(), 2) is None  # stream ended: clients refetch
    for _ in range(200):
        if len(connections) == 2 and hub.is_connected:
            break
        await asyncio.sleep(0.01)
    assert len(connections) == 2
    assert not hub.is_connected or connections[1].listeners
    fresh = hub.subscribe(None, "a")
    connections[1].notify(MEASUREMENTS_CHANNEL, NOTIFICATION)
    assert (await asyncio.wait_for(fresh.queue.get(), 2)).series_id == 7  # type: ignore[union-attr]
    await hub.stop()


async def test_nothing_is_emitted_while_disconnected() -> None:
    hub = Broadcaster(never_connects, initial_backoff=0.01)
    await hub.start()
    sub = hub.subscribe(None, "a")
    await asyncio.sleep(0.1)
    assert not hub.is_connected
    assert sub.queue.empty() or sub.queue.get_nowait() is None  # never a fabricated event
    await hub.stop()
