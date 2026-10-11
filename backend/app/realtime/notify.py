"""``pg_notify`` helper. The notification is queued in the caller's transaction and is only
delivered on commit, so listeners never see changes that were rolled back."""

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

SERIES_CHANNEL = "zai_series"
MEASUREMENTS_CHANNEL = "zai_measurements"


async def notify(session: AsyncSession, channel: str, payload: dict[str, Any]) -> None:
    await session.execute(
        text("SELECT pg_notify(:channel, :payload)"),
        {"channel": channel, "payload": json.dumps(payload, separators=(",", ":"))},
    )
