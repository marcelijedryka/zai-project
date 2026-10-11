"""Liveness/readiness probe."""

import asyncio

from fastapi import APIRouter, HTTPException
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.db import SessionDep

router = APIRouter(tags=["system"])

DB_TIMEOUT_SECONDS = 1.0


@router.get("/health")
async def health(session: SessionDep) -> dict[str, str]:
    try:
        async with asyncio.timeout(DB_TIMEOUT_SECONDS):
            await session.execute(text("SELECT 1"))
    except (SQLAlchemyError, TimeoutError, OSError) as exc:
        raise HTTPException(status_code=503, detail="Database unavailable.") from exc
    return {"status": "ok"}
