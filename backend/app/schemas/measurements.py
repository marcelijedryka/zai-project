from datetime import datetime

from pydantic import AwareDatetime, field_validator

from app.schemas.common import CamelModel, FiniteFloat, RequestModel


class MeasurementInput(RequestModel):
    """The series comes from the sensor's API key, never from the body."""

    value: FiniteFloat
    # ISO 8601 *with* a UTC offset; omitted or null means "now" on the server.
    timestamp: AwareDatetime | None = None

    @field_validator("timestamp", mode="before")
    @classmethod
    def _iso_strings_only(cls, value: object) -> object:
        # Pydantic would also accept epoch numbers (seconds or milliseconds: ambiguous).
        if value is None or isinstance(value, str):
            return value
        raise ValueError("timestamp must be an ISO 8601 string with a UTC offset")


class MeasurementOut(CamelModel):
    id: int
    series_id: int
    sensor_id: int | None = None
    value: float
    timestamp: datetime
