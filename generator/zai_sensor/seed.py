"""Sample data (F11): create the series and one sensor per series through the API, then backfill
about 14 days of hourly values with each sensor's own key. Nothing touches the database directly.

Idempotent: a series that already holds data is left alone, so running it twice duplicates nothing.
"""

import json
import os
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from zai_sensor.client import ApiClient, ApiError
from zai_sensor.durations import parse_duration
from zai_sensor.models import Point, Summary
from zai_sensor.patterns import Pattern, PatternName, PatternSpec, grid_ending_at
from zai_sensor.report import Reporter
from zai_sensor.sender import send_points
from zai_sensor.sources.open_meteo import WARSAW, OpenMeteo, SourceError

MAX_DAYS = 90


class ConfigError(Exception):
    """The seed file is invalid (the message never contains secrets)."""


@dataclass(frozen=True)
class SeedSeries:
    name: str
    minimum: float
    maximum: float
    color: str
    icon: str | None
    unit: str | None
    variable: str | None  # Open-Meteo variable; ``None`` = synthetic only
    synthetic: PatternSpec  # the series' own pattern, also the offline stand-in for Open-Meteo


@dataclass(frozen=True)
class SeedConfig:
    days: int
    latitude: float
    longitude: float
    series: list[SeedSeries]


@dataclass
class SeedResult:
    created: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    summaries: dict[str, Summary] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def exit_code(self) -> int:
        if any(s.key_rejected for s in self.summaries.values()):
            return 2
        bad = any(s.rejected or s.failed for s in self.summaries.values())
        return 1 if bad or self.errors else 0


def _table(raw: object, where: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} must be a table")
    return raw


def _number(table: dict[str, Any], key: str, where: str, default: float | None = None) -> float:
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"{where}.{key} must be a number")
    return float(value)


def _text(table: dict[str, Any], key: str, where: str, *, required: bool = True) -> str | None:
    value = table.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where}.{key} must be a non-empty string")
    return value


def _synthetic(
    table: dict[str, Any], where: str, low: float, high: float, seed: int
) -> PatternSpec:
    name = _text(table, "pattern", where) or ""
    try:
        pattern = PatternName(name)
        period = parse_duration(str(table.get("period", "24h")))
    except ValueError as exc:
        raise ConfigError(f"{where}: invalid pattern or period ({exc})") from None
    spec = PatternSpec(
        name=pattern,
        minimum=low,
        maximum=high,
        offset=_number(table, "offset", where) if "offset" in table else None,
        amplitude=_number(table, "amplitude", where) if "amplitude" in table else None,
        period=period,
        peak_hour=_number(table, "peak_hour", where, 15.0),
        noise=_number(table, "noise", where, 0.0),
        seed=seed,
    )
    try:
        spec.validate()
    except ValueError as exc:
        raise ConfigError(f"{where}: {exc}") from None
    return spec


def parse_config(data: dict[str, Any]) -> SeedConfig:
    days = int(_number(data, "days", "seed", 14))
    if not 1 <= days <= MAX_DAYS:
        raise ConfigError(f"seed.days must be between 1 and {MAX_DAYS}")
    base_seed = int(_number(data, "seed", "seed", 42))
    location = _table(data.get("location", {}), "location")
    latitude = _number(location, "latitude", "location", WARSAW[0])
    longitude = _number(location, "longitude", "location", WARSAW[1])
    raw_series = data.get("series")
    if not isinstance(raw_series, list) or not raw_series:
        raise ConfigError("the seed file needs at least one [[series]] table")
    series: list[SeedSeries] = []
    for index, raw in enumerate(raw_series):
        where = f"series[{index}]"
        item = _table(raw, where)
        name = _text(item, "name", where) or ""
        if name.upper().startswith("ZAI-TEST-"):
            raise ConfigError(f"{where}.name must not use the reserved ZAI-TEST- prefix")
        low, high = _number(item, "min", where), _number(item, "max", where)
        series.append(
            SeedSeries(
                name=name,
                minimum=low,
                maximum=high,
                color=_text(item, "color", where) or "",
                icon=_text(item, "icon", where, required=False),
                unit=_text(item, "unit", where, required=False),
                variable=_text(item, "variable", where, required=False),
                synthetic=_synthetic(
                    _table(item.get("synthetic"), f"{where}.synthetic"),
                    f"{where}.synthetic",
                    low,
                    high,
                    base_seed + index,
                ),
            )
        )
    return SeedConfig(days, latitude, longitude, series)


def load_config(path: Path) -> SeedConfig:
    try:
        with path.open("rb") as handle:
            return parse_config(tomllib.load(handle))
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc.strerror}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from None


def floor_hour(moment: datetime) -> datetime:
    return moment.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def build_points(
    spec: SeedSeries,
    config: SeedConfig,
    now: datetime,
    *,
    synthetic_only: bool,
    open_meteo: OpenMeteo | None,
) -> list[Point]:
    """About ``days * 24`` hourly points, oldest first, all in the past."""
    count = config.days * 24
    if synthetic_only or spec.variable is None:
        grid = grid_ending_at(floor_hour(now), count, timedelta(hours=1))
        return list(Pattern(spec.synthetic).points(grid))
    source = open_meteo or OpenMeteo(latitude=config.latitude, longitude=config.longitude)
    points = source.hourly(spec.variable, past_days=config.days + 1, now=now)
    return points[-count:]


def run_seed(
    client: ApiClient,
    config: SeedConfig,
    username: str,
    password: str,
    reporter: Reporter,
    *,
    now: datetime | None = None,
    synthetic_only: bool = False,
    open_meteo: OpenMeteo | None = None,
    keys_path: Path | None = None,
) -> SeedResult:
    """Seed through the public API. Raises :class:`ApiError` if the admin login or a setup call
    fails; problems with one series' data are collected in ``errors`` and the rest continues."""
    now = now or datetime.now(UTC)
    result = SeedResult()
    keys: dict[str, str] = {}  # in memory only, unless ``keys_path`` is given
    source = open_meteo or OpenMeteo(latitude=config.latitude, longitude=config.longitude)
    token = client.login(username, password)
    try:
        existing = {item["name"]: item for item in client.list_series()}
        for spec in config.series:
            item = existing.get(spec.name)
            if item is None:
                item = client.create_series(token, _series_body(spec))
                result.created.append(spec.name)
            if client.series_stats(item["id"])["count"] > 0:
                reporter.info(f"  {spec.name}: already has data, skipped")
                result.skipped.append(spec.name)
                continue
            try:
                points = build_points(
                    spec, config, now, synthetic_only=synthetic_only, open_meteo=source
                )
            except SourceError as exc:
                result.errors[spec.name] = str(exc)
                reporter.error(f"{spec.name}: {exc}")
                continue
            if not points:
                result.errors[spec.name] = "the source returned no usable points"
                reporter.error(f"{spec.name}: the source returned no usable points")
                continue
            sensor = client.create_sensor(token, f"seed - {spec.name}", item["id"])
            keys[spec.name] = sensor["apiKey"]
            summary = send_points(client, sensor["apiKey"], points, reporter, show_accepted=False)
            result.summaries[spec.name] = summary
            reporter.info(f"  {spec.name}: {summary.line()}")
            if summary.key_rejected:
                break
    finally:
        if keys_path is not None and keys:
            write_keys(keys_path, keys)
        try:
            client.logout(token)
        except ApiError:
            reporter.warning("could not log out; the session expires on its own after 2 hours")
    return result


def _series_body(spec: SeedSeries) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": spec.name,
        "minValue": spec.minimum,
        "maxValue": spec.maximum,
        "color": spec.color,
    }
    if spec.icon:
        body["icon"] = spec.icon
    if spec.unit:
        body["unit"] = spec.unit
    return body


def write_keys(path: Path, keys: dict[str, str]) -> None:
    """Write ``{series name: sensor key}`` readable by the owner only (created as 0600)."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(descriptor, 0o600)  # also tightens a file that already existed
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(keys, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
