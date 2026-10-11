from datetime import datetime
from typing import Annotated

from pydantic import StringConstraints

from app.schemas.common import CamelModel, FiniteFloat, RequestModel


class SeriesInput(RequestModel):
    """``minValue < maxValue`` is a semantic rule (422), checked in the service."""

    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    min_value: FiniteFloat
    max_value: FiniteFloat
    color: Annotated[str, StringConstraints(pattern=r"^#[0-9A-Fa-f]{6}$")]
    icon: Annotated[str, StringConstraints(max_length=50)] | None = None
    unit: Annotated[str, StringConstraints(max_length=20)] | None = None
    description: Annotated[str, StringConstraints(max_length=1000)] | None = None


class SeriesOut(CamelModel):
    id: int
    name: str
    min_value: float
    max_value: float
    color: str
    icon: str | None
    unit: str | None
    description: str | None
    created_at: datetime
    updated_at: datetime


class SeriesStats(CamelModel):
    count: int
    min: float | None
    max: float | None
    first_timestamp: datetime | None
    last_timestamp: datetime | None
