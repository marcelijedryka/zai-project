from datetime import UTC, datetime, timedelta
from itertools import pairwise

import pytest

from zai_sensor.patterns import (
    Pattern,
    PatternName,
    PatternSpec,
    grid_ending_at,
    time_grid,
)

START = datetime(2026, 10, 1, tzinfo=UTC)
GRID = grid_ending_at(START + timedelta(days=3), 72, timedelta(hours=1))


def values(spec: PatternSpec) -> list[float]:
    return [p.value for p in Pattern(spec).points(GRID)]


@pytest.mark.parametrize("name", list(PatternName))
def test_values_stay_inside_the_range(name: PatternName) -> None:
    spec = PatternSpec(name=name, minimum=-5, maximum=5, noise=4, amplitude=20, seed=1)
    assert all(-5 <= v <= 5 for v in values(spec))


@pytest.mark.parametrize("name", list(PatternName))
def test_same_seed_same_values(name: PatternName) -> None:
    spec = PatternSpec(name=name, noise=1.5, seed=7)
    assert values(spec) == values(spec)


@pytest.mark.parametrize("name", [PatternName.UNIFORM, PatternName.RANDOM_WALK, PatternName.SINE])
def test_different_seed_different_values(name: PatternName) -> None:
    a = values(PatternSpec(name=name, noise=2, seed=1))
    b = values(PatternSpec(name=name, noise=2, seed=2))
    assert a != b


def test_constant_without_noise_is_constant() -> None:
    assert set(values(PatternSpec(name=PatternName.CONSTANT, offset=21.5))) == {21.5}


def test_constant_defaults_to_mid_range() -> None:
    assert set(values(PatternSpec(name=PatternName.CONSTANT, minimum=10, maximum=30))) == {20.0}


def test_sine_peaks_at_the_peak_hour_and_is_lowest_twelve_hours_later() -> None:
    spec = PatternSpec(
        name=PatternName.SINE, minimum=0, maximum=100, offset=50, amplitude=10, peak_hour=15
    )
    pattern = Pattern(spec)
    peak = pattern.value(datetime(2026, 10, 2, 15, tzinfo=UTC))
    low = pattern.value(datetime(2026, 10, 3, 3, tzinfo=UTC))
    assert (peak, low) == (60.0, 40.0)


def test_sine_depends_on_the_timestamp_not_on_the_call_order() -> None:
    spec = PatternSpec(name=PatternName.SINE, offset=50, amplitude=10)
    when = datetime(2026, 10, 2, 9, tzinfo=UTC)
    assert Pattern(spec).value(when) == Pattern(spec).value(when)


def test_random_walk_moves_in_small_steps() -> None:
    spec = PatternSpec(name=PatternName.RANDOM_WALK, minimum=0, maximum=100, step=1, seed=3)
    series = values(spec)
    assert max(abs(a - b) for a, b in pairwise(series)) < 6


def test_out_of_range_every_n_emits_invalid_values_at_those_positions_only() -> None:
    spec = PatternSpec(
        name=PatternName.UNIFORM, minimum=0, maximum=10, out_of_range_every=4, seed=1
    )
    series = values(spec)
    invalid = [i for i, v in enumerate(series, start=1) if not 0 <= v <= 10]
    assert invalid == list(range(4, len(series) + 1, 4))


def test_out_of_range_does_not_change_the_other_values() -> None:
    plain = values(PatternSpec(name=PatternName.UNIFORM, seed=5))
    spoiled = values(PatternSpec(name=PatternName.UNIFORM, seed=5, out_of_range_every=3))
    assert [v for i, v in enumerate(spoiled, 1) if i % 3] == [
        v for i, v in enumerate(plain, 1) if i % 3
    ]


@pytest.mark.parametrize(
    "spec",
    [
        PatternSpec(minimum=5, maximum=5),
        PatternSpec(minimum=9, maximum=1),
        PatternSpec(period=timedelta(0)),
        PatternSpec(noise=-1),
        PatternSpec(step=-1),
        PatternSpec(out_of_range_every=0),
    ],
)
def test_invalid_specs_are_refused(spec: PatternSpec) -> None:
    with pytest.raises(ValueError, match="--"):
        Pattern(spec)


def test_time_grid_is_closed_and_ordered() -> None:
    grid = list(time_grid(start=START, end=START + timedelta(hours=3), interval=timedelta(hours=1)))
    assert grid == [START + timedelta(hours=h) for h in range(4)]


def test_grid_ending_at_ends_exactly_at_the_end() -> None:
    end = datetime(2026, 10, 5, 12, 30, tzinfo=UTC)
    grid = grid_ending_at(end, 3, timedelta(minutes=30))
    assert grid == [end - timedelta(minutes=60), end - timedelta(minutes=30), end]
