from datetime import timedelta

import pytest

from zai_sensor.durations import parse_duration


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("10s", timedelta(seconds=10)),
        ("15m", timedelta(minutes=15)),
        ("1h", timedelta(hours=1)),
        ("2d", timedelta(days=2)),
        ("1h30m", timedelta(hours=1, minutes=30)),
        ("0.5s", timedelta(milliseconds=500)),
        ("5", timedelta(seconds=5)),
        (" 3S ", timedelta(seconds=3)),
    ],
)
def test_valid(text: str, expected: timedelta) -> None:
    assert parse_duration(text) == expected


@pytest.mark.parametrize("text", ["", "abc", "0s", "-1h", "1x", "h", "1h junk", "1 h 2"])
def test_invalid(text: str) -> None:
    with pytest.raises(ValueError, match="positive duration"):
        parse_duration(text)
