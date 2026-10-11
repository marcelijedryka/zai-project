from datetime import UTC, datetime, timedelta, timezone

from app.services.measurements import EARLIEST_UTC, LATEST_UTC, to_utc_clamped


def test_normal_values_are_converted_to_utc() -> None:
    moment = datetime(2026, 1, 10, 13, 0, tzinfo=timezone(timedelta(hours=2)))
    assert to_utc_clamped(moment) == datetime(2026, 1, 10, 11, 0, tzinfo=UTC)


def test_overflow_past_year_9999_clamps_to_the_late_end() -> None:
    moment = datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone(timedelta(hours=-12)))
    assert to_utc_clamped(moment) == LATEST_UTC


def test_underflow_before_year_1_clamps_to_the_early_end() -> None:
    moment = datetime(1, 1, 1, 0, 0, tzinfo=timezone(timedelta(hours=14)))
    assert to_utc_clamped(moment) == EARLIEST_UTC
    assert EARLIEST_UTC.year == 1
