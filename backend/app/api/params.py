"""Query-parameter parsing shared by the measurement routes (list, stream, aggregate).

Every failure is a 400 problem naming the field, and never echoes the offending value.
"""

import re
from datetime import UTC, datetime

from pydantic import AwareDatetime, TypeAdapter, ValidationError

from app.core.problems import ProblemError
from app.schemas.common import MAX_ID

MAX_SERIES_FILTER = 200
_AWARE = TypeAdapter(AwareDatetime)
# A "+" in a query string decodes to a space, so "...10:00:00+01:00" arrives as "...10:00:00 01:00".
_LOST_PLUS = re.compile(r"(?<=\d) (?=\d\d:\d\d$)")
_EPOCH_NUMBER = re.compile(r"-?\d+(\.\d+)?")  # pydantic would read these as Unix timestamps


def bad_request(field: str, message: str) -> ProblemError:
    return ProblemError(
        400, "The query parameters are invalid.", errors=[{"field": field, "message": message}]
    )


def parse_series(raw: str | None) -> list[int] | None:
    if raw is None or raw.strip() == "":
        return None  # no filter: all series
    ids: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part.isascii() or not part.isdigit() or not 1 <= int(part) <= MAX_ID:
            raise bad_request("series", "must be a comma-separated list of series ids")
        ids.append(int(part))
    if len(ids) > MAX_SERIES_FILTER:
        raise bad_request("series", f"at most {MAX_SERIES_FILTER} series ids are allowed")
    return ids


def parse_time(field: str, raw: str | None) -> datetime | None:
    if raw is None:
        return None
    text = raw.strip()
    try:
        if _EPOCH_NUMBER.fullmatch(text):
            raise ValueError("epoch numbers are ambiguous (seconds or milliseconds)")
        parsed = _AWARE.validate_python(_LOST_PLUS.sub("+", text))
        return parsed.astimezone(UTC)
    except (ValidationError, ValueError, OverflowError):
        raise bad_request(field, "must be an ISO 8601 timestamp with a UTC offset") from None
