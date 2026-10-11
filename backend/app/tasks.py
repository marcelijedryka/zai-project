"""Background maintenance started from the application lifespan."""

import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.services import auth, measurements

logger = logging.getLogger("zai.tasks")

PURGE_INTERVAL_SECONDS = 60 * 60


# Housekeeping only: expired sessions never authenticate (``find_session`` filters on
# ``expires_at``) and old rejections are only shown in the admin error log, so the first run may
# wait a full interval; ``purge-sessions`` runs the session purge on demand.
async def housekeeping_forever(
    factory: async_sessionmaker[AsyncSession],
    rejection_retention_days: int,
    interval: float = PURGE_INTERVAL_SECONDS,
) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            async with factory() as session:
                sessions = await auth.purge_expired_sessions(session)
            async with factory() as session:
                rejections = await measurements.purge_old_rejections(
                    session, rejection_retention_days
                )
            if sessions or rejections:
                logger.info(
                    "housekeeping done",
                    extra={"fields": {"sessions": sessions, "rejections": rejections}},
                )
        except Exception:
            logger.exception("housekeeping failed")
