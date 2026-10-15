from datetime import datetime

from app.schemas.common import CamelModel


class RejectionOut(CamelModel):
    """A refused measurement (F4 error log). Never carries key or hash material."""

    id: int
    series_id: int
    series_name: str
    # Null only if the sensor row itself is gone; an unregistered sensor keeps its row and name.
    sensor_id: int | None = None
    sensor_name: str | None = None
    value: float
    timestamp: datetime  # the timestamp the sensor sent, not when it was refused
    reason: str
    created_at: datetime
