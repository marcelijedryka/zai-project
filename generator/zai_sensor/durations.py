"""Parsing of short durations such as ``10s``, ``15m``, ``1h`` or ``2d``."""

import re
from datetime import timedelta

_DURATION = re.compile(r"(?P<amount>\d+(?:\.\d+)?)\s*(?P<unit>[smhd])")
_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}


def parse_duration(text: str) -> timedelta:
    """``"90s"``, ``"1h"``, ``"1h30m"`` -> ``timedelta``; a bare number means seconds."""
    raw = text.strip().lower()
    if re.fullmatch(r"\d+(?:\.\d+)?", raw):
        raw += "s"
    position, total = 0, timedelta()
    for match in _DURATION.finditer(raw):
        if match.start() != position:
            break
        total += timedelta(**{_UNITS[match["unit"]]: float(match["amount"])})
        position = match.end()
    if position == 0 or position != len(raw) or total <= timedelta():
        raise ValueError("expected a positive duration such as 10s, 15m, 1h or 2d")
    return total
