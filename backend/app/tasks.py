"""Background maintenance started from the application lifespan."""

import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.services import auth

logger = logging.getLogger("zai.tasks")

PURGE_INTERVAL_SECONDS = 60 * 60


async def purge_expired_sessions_forever(
    factory: async_sessionmaker[AsyncSession], interval: float = PURGE_INTERVAL_SECONDS
) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            async with factory() as session:
                purged = await auth.purge_expired_sessions(session)
            if purged:
                logger.info("expired sessions purged", extra={"fields": {"count": purged}})
        except Exception:
            logger.exception("session purge failed")
