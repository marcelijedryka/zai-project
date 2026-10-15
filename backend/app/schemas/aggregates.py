from datetime import datetime

from app.schemas.common import CamelModel


class AggregateOut(CamelModel):
    """One time bucket of one series. ``bucket`` is the bucket's start (UTC)."""

    series_id: int
    bucket: datetime
    avg: float
    min: float
    max: float
    count: int
