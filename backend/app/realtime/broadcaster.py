"""Fan-out of Postgres ``NOTIFY`` messages to the SSE subscribers of this worker process.

One dedicated asyncpg connection per worker (not from the SQLAlchemy pool: it must live for as
long as the app) runs ``LISTEN zai_measurements`` and ``LISTEN zai_series``. asyncpg calls the
listener callbacks synchronously on the event loop, so they only parse the payload and push it
with ``put_nowait`` into each subscriber's bounded queue. A subscriber whose queue is full is
dropped (its stream ends; the client reconnects and refetches) instead of slowing everyone down.

Notifications are sent by the writers in their own transaction, hence delivered on commit only:
a rollback or a rejected measurement produces nothing here, and this module never invents events.
If the listener connection is lost it reconnects with exponential backoff and, because events
may have been missed meanwhile, ends all open streams so that clients resynchronise.
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime

import asyncpg
from pydantic import ValidationError

from app.realtime.notify import MEASUREMENTS_CHANNEL, SERIES_CHANNEL
from app.schemas.measurements import MeasurementOut

logger = logging.getLogger("zai.realtime")

QUEUE_SIZE = 200
MAX_BACKOFF_SECONDS = 30.0
CHANNELS = (MEASUREMENTS_CHANNEL, SERIES_CHANNEL)


@dataclass(frozen=True)
class StreamEvent:
    """One SSE event; ``data`` is the JSON text, ``series_id`` drives the subscriber filter."""

    event: str
    data: str
    series_id: int
    id: str | None = None


class TooManyStreams(Exception):
    """The client already holds the maximum number of streams."""


@dataclass(eq=False)
class Subscription:
    queue: asyncio.Queue[StreamEvent | None]
    series_ids: frozenset[int] | None
    client: str
    closed: bool = field(default=False)

    def accepts(self, event: StreamEvent) -> bool:
        return self.series_ids is None or event.series_id in self.series_ids

    def close(self) -> None:
        """End the stream: drop what is queued and leave the end marker (``None``)."""
        if self.closed:
            return
        self.closed = True
        while True:
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self.queue.put_nowait(None)


def event_from_notification(channel: str, payload: str) -> StreamEvent | None:
    """Turn a NOTIFY payload into the event clients see, or ``None`` if it is not usable.

    Measurement events are re-serialised through ``MeasurementOut`` so that ``data`` is exactly
    the ``Measurement`` JSON of the REST API (same fields, same UTC timestamp format).
    """
    try:
        body = json.loads(payload)
        if channel == MEASUREMENTS_CHANNEL:
            measurement = MeasurementOut(
                id=body["id"],
                series_id=body["seriesId"],
                sensor_id=body.get("sensorId"),
                value=body["value"],
                # The producer keeps the client's UTC offset: normalise, as the REST API does.
                timestamp=datetime.fromisoformat(body["timestamp"]).astimezone(UTC),
            )
            return StreamEvent(
                "measurement",
                measurement.model_dump_json(by_alias=True, exclude_none=True),
                measurement.series_id,
                str(measurement.id),
            )
        if channel == SERIES_CHANNEL:
            data = json.dumps({"id": body["id"], "action": body["action"]}, separators=(",", ":"))
            return StreamEvent("series-changed", data, int(body["id"]))
    except (ValueError, KeyError, TypeError, ValidationError):
        pass
    logger.warning("ignoring an unusable notification", extra={"fields": {"channel": channel}})
    return None


Connect = Callable[[], Awaitable[asyncpg.Connection]]


def _signal_loss(lost: asyncio.Future[None]) -> Callable[[object], None]:
    """An asyncpg termination listener that resolves ``lost`` (once)."""

    def on_termination(_connection: object) -> None:
        if not lost.done():
            lost.set_result(None)

    return on_termination


class Broadcaster:
    def __init__(
        self,
        connect: Connect,
        *,
        max_streams_per_client: int = 5,
        queue_size: int = QUEUE_SIZE,
        initial_backoff: float = 1.0,
    ) -> None:
        self._connect = connect
        self._max_per_client = max_streams_per_client
        self._queue_size = queue_size
        self._initial_backoff = initial_backoff
        self._subscribers: set[Subscription] = set()
        self._per_client: dict[str, int] = {}
        self._task: asyncio.Task[None] | None = None
        self._connected = asyncio.Event()
        self._stopping = False

    # --- subscribers -------------------------------------------------------------------------

    def subscribe(self, series_ids: frozenset[int] | None, client: str) -> Subscription:
        if self._stopping:
            raise TooManyStreams  # shutting down: refuse like a full house
        if self._per_client.get(client, 0) >= self._max_per_client:
            raise TooManyStreams
        sub = Subscription(asyncio.Queue(self._queue_size), series_ids, client)
        self._subscribers.add(sub)
        self._per_client[client] = self._per_client.get(client, 0) + 1
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        """Idempotent."""
        if sub not in self._subscribers:
            return
        self._subscribers.discard(sub)
        remaining = self._per_client.get(sub.client, 1) - 1
        if remaining > 0:
            self._per_client[sub.client] = remaining
        else:
            self._per_client.pop(sub.client, None)

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def publish(self, event: StreamEvent) -> None:
        for sub in tuple(self._subscribers):
            if sub.closed or not sub.accepts(event):
                continue
            try:
                sub.queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning("dropping a slow stream subscriber")
                sub.close()
                self.unsubscribe(sub)

    def _on_notification(
        self, _conn: object, _pid: int, channel: str, payload: str
    ) -> None:  # asyncpg listener: synchronous, runs on the loop
        event = event_from_notification(channel, payload)
        if event is not None:
            self.publish(event)

    def _end_all_streams(self) -> None:
        for sub in tuple(self._subscribers):
            sub.close()
            self.unsubscribe(sub)

    # --- lifecycle ---------------------------------------------------------------------------

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="realtime-listener")

    async def wait_connected(self, seconds: float = 10.0) -> None:
        async with asyncio.timeout(seconds):
            await self._connected.wait()

    async def stop(self) -> None:
        self._stopping = True
        self._end_all_streams()
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        backoff = self._initial_backoff
        while True:
            connection: asyncpg.Connection | None = None
            try:
                connection = await self._connect()
                lost: asyncio.Future[None] = asyncio.get_running_loop().create_future()
                connection.add_termination_listener(_signal_loss(lost))
                for channel in CHANNELS:
                    await connection.add_listener(channel, self._on_notification)
                self._connected.set()
                backoff = self._initial_backoff
                logger.info("realtime listener connected")
                await lost
                logger.warning("realtime listener connection lost")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "realtime listener failed", extra={"fields": {"error": type(exc).__name__}}
                )
            finally:
                self._connected.clear()
                if connection is not None and not connection.is_closed():
                    with suppress(Exception):
                        await asyncio.shield(connection.close(timeout=2))
            # Events may have been missed: end the streams so clients reconnect and refetch.
            self._end_all_streams()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)


def listener_connector(dsn: str, *, timeout: float = 10.0) -> Connect:
    async def connect() -> asyncpg.Connection:
        return await asyncpg.connect(dsn, timeout=timeout)

    return connect
