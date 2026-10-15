"""``GET /api/rejections``: the F4 error-log view for the admin UI (contract extension)."""

from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Query

from app.db import SessionDep
from app.schemas.rejections import RejectionOut
from app.security.deps import AdminDep
from app.services import rejections as service

router = APIRouter(prefix="/rejections", tags=["rejections"])


@router.get("", response_model_exclude_none=True)
async def list_rejections(
    _admin: AdminDep,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=1000, description="Newest first; at most 1000")] = 100,
) -> list[RejectionOut]:
    """Admin only. Order: 401 (``AdminDep``) -> 400 (a bad ``limit``)."""
    return [RejectionOut(**asdict(row)) for row in await service.list_rejections(session, limit)]
