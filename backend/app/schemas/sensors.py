from datetime import datetime
from typing import Annotated

from pydantic import Field, StringConstraints

from app.schemas.common import CamelModel, RequestModel


class SensorInput(RequestModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    # Integers only; whether such a series exists (or the number is even a valid id) is a
    # semantic question answered with 422 by the service.
    series_id: Annotated[int, Field(strict=True)]


class SensorOut(CamelModel):
    id: int
    name: str
    series_id: int
    created_at: datetime
    last_measurement_at: datetime | None


class SensorCreated(SensorOut):
    """The only response that ever carries the key, and only at registration."""

    api_key: str
