"""``GET /api/measurements/stream``: realtime updates over Server-Sent Events (extension).

Public, like the REST reads. The endpoint opens no database session: the broadcaster owns the one
LISTEN connection, so a viewer never pins a pool connection, however long the stream lives.
"""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Query, Request
from sse_starlette import EventSourceResponse, ServerSentEvent
from starlette.background import BackgroundTask

from app.api.params import parse_series
from app.core.paths import STREAM_PATH
from app.core.problems import ProblemError
from app.realtime.broadcaster import Broadcaster, Subscription, TooManyStreams

router = APIRouter(tags=["realtime"])

STREAM_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


async def _events(broadcaster: Broadcaster, sub: Subscription) -> AsyncIterator[ServerSentEvent]:
    try:
        while (event := await sub.queue.get()) is not None:
            yield ServerSentEvent(data=event.data, event=event.event, id=event.id)
    finally:
        broadcaster.unsubscribe(sub)


@router.get(
    STREAM_PATH.removeprefix("/api"),
    response_class=EventSourceResponse,
    responses={
        200: {"description": "Server-Sent Events", "content": {"text/event-stream": {}}},
        429: {"description": "Too many open streams for this client"},
    },
)
async def stream(
    request: Request,
    series: Annotated[
        str | None, Query(description="Comma-separated series ids; empty = all series")
    ] = None,
) -> EventSourceResponse:
    """``event: measurement`` (``data`` = the REST ``Measurement``), ``event: series-changed``
    (``data`` = ``{id, action}``), and a ``: ping`` comment as heartbeat. Events are not replayed:
    after a reconnect, refetch the range you display."""
    ids = parse_series(series)  # 400 for a bad filter, before anything is reserved
    broadcaster: Broadcaster = request.app.state.broadcaster
    client = request.client.host if request.client else "unknown"
    try:
        sub = broadcaster.subscribe(frozenset(ids) if ids else None, client)
    except TooManyStreams:
        raise ProblemError(
            429,
            "Too many open event streams for this client.",
            headers={"Retry-After": "5"},
        ) from None
    return EventSourceResponse(
        _events(broadcaster, sub),
        headers=STREAM_HEADERS,
        ping=request.app.state.settings.sse_heartbeat_seconds,
        ping_message_factory=lambda: ServerSentEvent(comment="ping"),
        # Frees the slot even if the stream never started (the generator's finally needs a start).
        background=BackgroundTask(broadcaster.unsubscribe, sub),
    )
