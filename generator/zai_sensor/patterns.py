"""Synthetic value patterns: ``uniform | random-walk | sine | constant``.

Everything is driven by one seeded ``random.Random`` and by the point's timestamp, so the same
``--seed`` and the same timestamps always give the same values (backfills are repeatable and
live data continues the same curve).
"""

import math
import random
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from zai_sensor.models import Point


class PatternName(StrEnum):
    UNIFORM = "uniform"
    RANDOM_WALK = "random-walk"
    SINE = "sine"
    CONSTANT = "constant"


@dataclass(frozen=True)
class PatternSpec:
    name: PatternName = PatternName.SINE
    minimum: float = 0.0
    maximum: float = 100.0
    offset: float | None = None  # centre of a sine / level of a constant; default: mid-range
    amplitude: float | None = None  # sine; default: a quarter of the range
    period: timedelta = timedelta(hours=24)
    peak_hour: float = 15.0  # UTC hour of day at which the sine is at its maximum
    noise: float = 0.0  # standard deviation of Gaussian noise added to sine / constant
    step: float | None = None  # random-walk step (standard deviation); default: 2 % of the range
    seed: int | None = None
    out_of_range_every: int | None = None  # every Nth value is deliberately invalid (F4 demo)

    def validate(self) -> None:
        if not self.minimum < self.maximum:
            raise ValueError("--min must be smaller than --max")
        if self.period <= timedelta():
            raise ValueError("--period must be positive")
        if self.noise < 0 or (self.step is not None and self.step < 0):
            raise ValueError("--noise and --step must not be negative")
        if self.out_of_range_every is not None and self.out_of_range_every < 1:
            raise ValueError("--out-of-range-every must be at least 1")


class Pattern:
    """Stateful generator: call :meth:`value` once per point, in sending order."""

    def __init__(self, spec: PatternSpec) -> None:
        spec.validate()
        self.spec = spec
        self._rng = random.Random(spec.seed)  # noqa: S311 - reproducible test data, not security
        self._count = 0
        self._walk = (spec.minimum + spec.maximum) / 2
        self._span = spec.maximum - spec.minimum

    def _clamp(self, value: float) -> float:
        return min(max(value, self.spec.minimum), self.spec.maximum)

    def _base(self, when: datetime) -> float:
        spec = self.spec
        match spec.name:
            case PatternName.UNIFORM:
                return self._rng.uniform(spec.minimum, spec.maximum)
            case PatternName.RANDOM_WALK:
                step = spec.step if spec.step is not None else self._span * 0.02
                self._walk = self._clamp(self._walk + self._rng.gauss(0, step))
                return self._walk
            case PatternName.SINE:
                centre = spec.offset if spec.offset is not None else spec.minimum + self._span / 2
                amplitude = spec.amplitude if spec.amplitude is not None else self._span / 4
                seconds = when.astimezone(UTC).timestamp() - spec.peak_hour * 3600
                wave = math.cos(2 * math.pi * seconds / spec.period.total_seconds())
                return centre + amplitude * wave + self._rng.gauss(0, spec.noise)
            case PatternName.CONSTANT:
                level = spec.offset if spec.offset is not None else spec.minimum + self._span / 2
                return level + self._rng.gauss(0, spec.noise)

    def value(self, when: datetime | None = None) -> float:
        """The next value; ``when`` (aware) drives the sine phase, ``None`` means now."""
        self._count += 1
        every = self.spec.out_of_range_every
        if every is not None and self._count % every == 0:
            self._base(when or datetime.now(UTC))  # keep the random sequence aligned
            return round(self.spec.maximum + self._span * 0.5 + 1, 2)  # clearly above the range
        return round(self._clamp(self._base(when or datetime.now(UTC))), 2)

    def points(self, timestamps: Iterable[datetime]) -> Iterator[Point]:
        for when in timestamps:
            yield Point(self.value(when), when)


def time_grid(*, start: datetime, end: datetime, interval: timedelta) -> Iterator[datetime]:
    """``start, start + interval, ...`` while not after ``end`` (both aware, oldest first)."""
    current = start
    while current <= end:
        yield current
        current += interval


def grid_ending_at(end: datetime, count: int, interval: timedelta) -> list[datetime]:
    """The last ``count`` grid points up to and including ``end``, oldest first."""
    return [end - interval * i for i in range(count - 1, -1, -1)]
