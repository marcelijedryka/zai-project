"""``zai-sensor``: send, backfill or stream measurements, and seed the sample data.

Exit codes: 0 success; 1 some values were rejected (422) or failed after the retries;
2 the sensor key was refused (401); 3 a fatal error (API or data source unreachable, bad
admin login, invalid seed file); 64 invalid command line.
"""

import ipaddress
import math
import os
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Annotated, NoReturn
from urllib.parse import urlparse

import typer
from rich.console import Console
from rich.progress import BarColumn, Progress, TextColumn, TimeRemainingColumn

from zai_sensor.client import ApiClient, ApiError
from zai_sensor.durations import parse_duration
from zai_sensor.models import Point, Summary
from zai_sensor.patterns import (
    Pattern,
    PatternName,
    PatternSpec,
    grid_ending_at,
    time_grid,
)
from zai_sensor.report import Reporter
from zai_sensor.seed import ConfigError, load_config, run_seed
from zai_sensor.sender import send_points
from zai_sensor.sources.open_meteo import MAX_PAST_DAYS, WARSAW, OpenMeteo, SourceError

EXIT_OK, EXIT_PARTIAL, EXIT_KEY, EXIT_FATAL, EXIT_USAGE = 0, 1, 2, 3, 64

EXIT_CODES_HELP = (
    "Exit codes: 0 all accepted; 1 some values rejected (422) or failed; "
    "2 sensor key rejected (401); 3 fatal error (API/source unreachable, bad admin login, "
    "invalid seed file); 64 invalid command line."
)

app = typer.Typer(
    help="Sensor emulator for the ZAI measurement API.\n\n" + EXIT_CODES_HELP,
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)

sleep = time.sleep  # replaced in tests


class Source(StrEnum):
    SYNTHETIC = "synthetic"
    OPEN_METEO = "open-meteo"


@dataclass
class Globals:
    api_url: str | None
    api_key: str | None
    timeout: float
    retries: int
    reporter: Reporter
    dry_run: bool
    allow_insecure: bool = False


def fail(message: str, code: int = EXIT_USAGE) -> NoReturn:
    typer.secho(f"error: {message}", err=True, fg=typer.colors.RED)
    raise typer.Exit(code)


def _duration(text: str, option: str) -> timedelta:
    try:
        return parse_duration(text)
    except ValueError as exc:
        fail(f"{option}: {exc}")


def _moment(text: str, option: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(text.strip())
    except ValueError:
        fail(f"{option} must be an ISO 8601 timestamp, e.g. 2026-10-01T12:00:00Z")
    if parsed.tzinfo is None:
        fail(f"{option} needs a UTC offset (e.g. ...Z or ...+02:00); the API rejects naive times")
    return parsed.astimezone(UTC)


@app.callback()
def global_options(
    ctx: typer.Context,
    api_url: Annotated[
        str | None,
        typer.Option(envvar="ZAI_API_URL", help="API base URL, e.g. http://localhost:8000"),
    ] = None,
    api_key: Annotated[
        str | None,
        typer.Option(
            envvar="ZAI_API_KEY",
            help="Sensor key. Prefer the ZAI_API_KEY variable or the hidden prompt: a value on "
            "the command line is visible in the process list.",
        ),
    ] = None,
    timeout: Annotated[str, typer.Option(help="Per-request timeout, e.g. 10s")] = "10s",
    retries: Annotated[
        int, typer.Option(min=0, max=10, help="Retries on 429/503/connect errors")
    ] = 3,
    verbose: Annotated[bool, typer.Option("--verbose", help="Log requests (never keys)")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Print what would be sent")] = False,
    allow_insecure: Annotated[
        bool,
        typer.Option(
            "--allow-insecure",
            help="Allow plain http:// to a non-local host (keys and the admin password would "
            "travel unencrypted; by default only localhost / loopback may use http)",
        ),
    ] = False,
) -> None:
    ctx.obj = Globals(
        api_url=api_url,
        api_key=api_key,
        timeout=_duration(timeout, "--timeout").total_seconds(),
        retries=retries,
        reporter=Reporter(verbose=verbose),
        dry_run=dry_run,
        allow_insecure=allow_insecure,
    )


def _is_loopback(host: str) -> bool:
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _check_url(g: Globals) -> str:
    """The API URL, or exit 64. Plain http is refused for non-loopback hosts (credentials would
    travel in the clear) unless ``--allow-insecure``; a dry run sends nothing, so it may pass."""
    if not g.api_url:
        fail("no API URL: pass --api-url or set ZAI_API_URL")
    parsed = urlparse(g.api_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        fail("--api-url must be an http(s) URL")
    if parsed.scheme == "http" and not _is_loopback(parsed.hostname) and not g.dry_run:
        if not g.allow_insecure:
            fail(
                "refusing plain http:// to a non-local host: the sensor key and the admin "
                "password would be sent unencrypted. Use https://, or pass --allow-insecure "
                "if you really mean it."
            )
        g.reporter.warning("plain http: keys and passwords travel unencrypted (--allow-insecure)")
    return g.api_url


def _client(g: Globals) -> ApiClient:
    return ApiClient(_check_url(g), timeout=g.timeout, retries=g.retries, log=g.reporter.debug)


def _key(g: Globals) -> str:
    if g.dry_run:
        return ""
    return g.api_key or typer.prompt("Sensor API key", hide_input=True)


def _finish(summary: Summary, reporter: Reporter, *, dry_run: bool) -> NoReturn:
    reporter.summary(summary)
    raise typer.Exit(EXIT_OK if dry_run else summary.exit_code)


# --- shared option types -----------------------------------------------------------------------

SourceOpt = Annotated[Source, typer.Option(help="Where the values come from")]
PatternOpt = Annotated[PatternName, typer.Option(help="Synthetic pattern")]
MinOpt = Annotated[float, typer.Option("--min", help="Series minimum (bounds the pattern)")]
MaxOpt = Annotated[float, typer.Option("--max", help="Series maximum (bounds the pattern)")]
OffsetOpt = Annotated[
    float | None, typer.Option(help="Sine centre / constant level (default: mid-range)")
]
AmplitudeOpt = Annotated[
    float | None, typer.Option(help="Sine amplitude (default: a quarter of the range)")
]
PeriodOpt = Annotated[str, typer.Option(help="Sine period, e.g. 24h")]
PeakOpt = Annotated[float, typer.Option(help="UTC hour of day of the sine maximum")]
NoiseOpt = Annotated[float, typer.Option(help="Gaussian noise (standard deviation)")]
StepOpt = Annotated[float | None, typer.Option(help="Random-walk step (default: 2 % of the range)")]
SeedOpt = Annotated[int | None, typer.Option("--seed", help="Random seed: same seed, same values")]
BadEveryOpt = Annotated[
    int | None,
    typer.Option(
        "--out-of-range-every",
        min=1,
        help="Send an invalid value every N points (F4 demo)",
    ),
]
VariableOpt = Annotated[
    str,
    typer.Option(help="Open-Meteo variable: temperature_2m, pm2_5, pm10, nitrogen_dioxide"),
]
LatOpt = Annotated[float, typer.Option("--lat", help="Latitude (default: Warsaw)")]
LonOpt = Annotated[float, typer.Option("--lon", help="Longitude (default: Warsaw)")]


def _spec(
    pattern: PatternName,
    minimum: float,
    maximum: float,
    offset: float | None,
    amplitude: float | None,
    period: str,
    peak_hour: float,
    noise: float,
    step: float | None,
    seed: int | None,
    bad_every: int | None,
) -> PatternSpec:
    spec = PatternSpec(
        name=pattern,
        minimum=minimum,
        maximum=maximum,
        offset=offset,
        amplitude=amplitude,
        period=_duration(period, "--period"),
        peak_hour=peak_hour,
        noise=noise,
        step=step,
        seed=seed,
        out_of_range_every=bad_every,
    )
    try:
        spec.validate()
    except ValueError as exc:
        fail(str(exc))
    return spec


# --- send --------------------------------------------------------------------------------------


@app.command()
def send(
    ctx: typer.Context,
    value: Annotated[float, typer.Option(help="The value to send")],
    timestamp: Annotated[
        str | None, typer.Option(help="ISO 8601 with offset; default: now")
    ] = None,
) -> None:
    """Send one value (e.g. an out-of-range one to see the 422 rejection)."""
    g: Globals = ctx.obj
    if not math.isfinite(value):
        fail("--value must be a finite number")
    when = _moment(timestamp, "--timestamp") if timestamp else None
    with _client(g) as client:
        summary = send_points(client, _key(g), [Point(value, when)], g.reporter, dry_run=g.dry_run)
    _finish(summary, g.reporter, dry_run=g.dry_run)


# --- backfill ----------------------------------------------------------------------------------


def _open_meteo_points(
    source: OpenMeteo,
    variable: str,
    *,
    start: datetime | None,
    end: datetime,
    count: int | None,
    now: datetime,
) -> list[Point]:
    days_back = (now - start).total_seconds() / 86400 if start else (count or 24) / 24
    past_days = math.ceil(days_back) + 1
    if past_days > MAX_PAST_DAYS:
        fail(f"Open-Meteo serves at most {MAX_PAST_DAYS} past days; ask for a shorter range")
    points = [
        p
        for p in source.hourly(variable, past_days=past_days, now=now)
        if p.timestamp is not None
        and p.timestamp <= end
        and (start is None or p.timestamp >= start)
    ]
    return points[-count:] if count else points


@app.command()
def backfill(
    ctx: typer.Context,
    count: Annotated[
        int | None, typer.Option(min=1, help="Number of points, ending at --end")
    ] = None,
    start: Annotated[
        str | None, typer.Option(help="First timestamp (ISO 8601 with offset)")
    ] = None,
    end: Annotated[str | None, typer.Option(help="Last timestamp; default: now")] = None,
    interval: Annotated[str, typer.Option(help="Spacing of synthetic points, e.g. 1h")] = "1h",
    source: SourceOpt = Source.SYNTHETIC,
    pattern: PatternOpt = PatternName.SINE,
    minimum: MinOpt = 0.0,
    maximum: MaxOpt = 100.0,
    offset: OffsetOpt = None,
    amplitude: AmplitudeOpt = None,
    period: PeriodOpt = "24h",
    peak_hour: PeakOpt = 15.0,
    noise: NoiseOpt = 0.0,
    step: StepOpt = None,
    seed: SeedOpt = None,
    out_of_range_every: BadEveryOpt = None,
    variable: VariableOpt = "temperature_2m",
    lat: LatOpt = WARSAW[0],
    lon: LonOpt = WARSAW[1],
    show_each: Annotated[
        bool, typer.Option("--show-each", help="Print every accepted point")
    ] = False,
) -> None:
    """Send past values, oldest first: --count N, or --start (and optionally --end)."""
    g: Globals = ctx.obj
    now = datetime.now(UTC)
    if (count is None) == (start is None):
        fail("give exactly one of --count and --start")
    last = _moment(end, "--end") if end else now
    if last > now + timedelta(minutes=5):
        fail(
            "--end must not be in the future: the API refuses timestamps more than 5 minutes ahead"
        )
    first = _moment(start, "--start") if start else None
    if first is not None and first > last:
        fail("--start must not be later than --end")

    if source is Source.OPEN_METEO:
        meteo = OpenMeteo(latitude=lat, longitude=lon, timeout=g.timeout * 2)
        try:
            points = _open_meteo_points(
                meteo, variable, start=first, end=last, count=count, now=now
            )
        except SourceError as exc:
            fail(str(exc), EXIT_FATAL)
        finally:
            meteo.close()
    else:
        step_size = _duration(interval, "--interval")
        spec = _spec(
            pattern,
            minimum,
            maximum,
            offset,
            amplitude,
            period,
            peak_hour,
            noise,
            step,
            seed,
            out_of_range_every,
        )
        if count is not None:
            grid = grid_ending_at(last, count, step_size)
        else:
            assert first is not None  # noqa: S101 - narrowed by the check above
            grid = list(time_grid(start=first, end=last, interval=step_size))
        points = list(Pattern(spec).points(grid))
    if not points:
        fail("no points to send in that range", EXIT_FATAL)

    g.reporter.info(f"Backfilling {len(points)} points ({source.value})")
    with _client(g) as client, _progress(len(points), g.reporter.verbose) as advance:
        summary = send_points(
            client,
            _key(g),
            points,
            g.reporter,
            dry_run=g.dry_run,
            show_accepted=show_each or g.reporter.verbose or len(points) <= 20,
            on_point=advance,
        )
    _finish(summary, g.reporter, dry_run=g.dry_run)


@contextmanager
def _progress(total: int, quiet: bool) -> Iterator[Callable[[], None]]:
    """A progress bar on stderr, only on a terminal; yields the 'one more done' callback."""
    console = Console(stderr=True)
    with Progress(
        TextColumn("{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeRemainingColumn(),
        console=console,
        transient=True,
        disable=quiet or not console.is_terminal,
    ) as progress:
        task = progress.add_task("sending", total=total)
        yield lambda: progress.advance(task)


# --- live --------------------------------------------------------------------------------------


@app.command()
def live(
    ctx: typer.Context,
    interval: Annotated[str, typer.Option(help="Time between values, e.g. 10s")] = "10s",
    count: Annotated[
        int | None, typer.Option(min=1, help="Stop after N values (default: Ctrl+C)")
    ] = None,
    source: SourceOpt = Source.SYNTHETIC,
    pattern: PatternOpt = PatternName.SINE,
    minimum: MinOpt = 0.0,
    maximum: MaxOpt = 100.0,
    offset: OffsetOpt = None,
    amplitude: AmplitudeOpt = None,
    period: PeriodOpt = "24h",
    peak_hour: PeakOpt = 15.0,
    noise: NoiseOpt = 0.0,
    step: StepOpt = None,
    seed: SeedOpt = None,
    out_of_range_every: BadEveryOpt = None,
    variable: VariableOpt = "temperature_2m",
    lat: LatOpt = WARSAW[0],
    lon: LonOpt = WARSAW[1],
) -> None:
    """Send a value every --interval with the server's own clock (Ctrl+C prints the summary)."""
    g: Globals = ctx.obj
    pause = _duration(interval, "--interval").total_seconds()
    spec = _spec(
        pattern,
        minimum,
        maximum,
        offset,
        amplitude,
        period,
        peak_hour,
        noise,
        step,
        seed,
        out_of_range_every,
    )
    generator = Pattern(spec)
    meteo = OpenMeteo(latitude=lat, longitude=lon, timeout=g.timeout * 2)
    summary = Summary()

    def values() -> Iterator[Point]:
        produced = 0
        while count is None or produced < count:
            if produced:
                sleep(pause)
            produced += 1
            if source is Source.OPEN_METEO:
                try:
                    point = meteo.current(variable)
                except SourceError as exc:
                    g.reporter.warning(str(exc))
                    continue
                if point is None:
                    g.reporter.warning("Open-Meteo has no current value yet; skipping this tick")
                    continue
                yield point
            else:
                yield Point(generator.value(datetime.now(UTC)))  # no timestamp: server time

    interrupted = False
    try:
        with _client(g) as client:
            send_points(
                client,
                _key(g),
                values(),
                g.reporter,
                dry_run=g.dry_run,
                summary=summary,
            )
    except KeyboardInterrupt:
        interrupted = True
    finally:
        meteo.close()
    if interrupted:
        typer.echo("")
    _finish(summary, g.reporter, dry_run=g.dry_run)


# --- seed --------------------------------------------------------------------------------------


@app.command()
def seed(
    ctx: typer.Context,
    config: Annotated[Path, typer.Option(help="Seed file, e.g. generator/seed.example.toml")],
    admin_user: Annotated[str, typer.Option(help="Admin username")] = "admin",
    source: Annotated[
        Source | None,
        typer.Option(
            help="'synthetic' replaces Open-Meteo with each series' own pattern (offline)"
        ),
    ] = None,
    days: Annotated[
        int | None, typer.Option(min=1, max=90, help="History in days (default: the seed file's)")
    ] = None,
    save_keys: Annotated[
        Path | None,
        typer.Option(
            help="Also write the sensor keys here (mode 600). Default: kept in memory only"
        ),
    ] = None,
) -> None:
    """Create the sample series and sensors through the API and backfill about 14 days."""
    g: Globals = ctx.obj
    try:
        seed_config = load_config(config)
    except ConfigError as exc:
        fail(str(exc), EXIT_FATAL)
    if days is not None:
        seed_config = replace(seed_config, days=days)
    if g.dry_run:
        for item in seed_config.series:
            origin = (
                item.variable if item.variable and source is not Source.SYNTHETIC else "synthetic"
            )
            g.reporter.info(f"- {item.name}: {seed_config.days * 24} hourly points ({origin})")
        g.reporter.info("dry run: nothing was sent")
        raise typer.Exit(EXIT_OK)
    _check_url(g)  # before the password is asked for
    password = os.environ.get("ZAI_ADMIN_PASSWORD") or typer.prompt(
        f"Password of {admin_user}", hide_input=True
    )
    g.reporter.info(f"Seeding {g.api_url} (through the API only)")
    with _client(g) as client:
        try:
            result = run_seed(
                client,
                seed_config,
                admin_user,
                password,
                g.reporter,
                synthetic_only=source is Source.SYNTHETIC,
                keys_path=save_keys,
            )
        except ApiError as exc:
            fail(str(exc), EXIT_FATAL)
    total = Summary()
    for item_summary in result.summaries.values():
        total.sent += item_summary.sent
        total.accepted += item_summary.accepted
        total.rejected += item_summary.rejected
        total.failed += item_summary.failed
    g.reporter.info(
        f"Done: {len(result.created)} series created, {len(result.skipped)} skipped, "
        f"{len(result.errors)} with source errors"
    )
    g.reporter.summary(total)
    raise typer.Exit(result.exit_code)


def main() -> None:
    """Console entry point. Usage errors exit 64: 2 is reserved for 'sensor key rejected'."""
    try:
        code = app(standalone_mode=False)
    except Exception as exc:  # click usage errors (typer bundles click): show, then map the code
        show = getattr(exc, "show", None)
        if getattr(exc, "exit_code", None) != 2 or not callable(show):
            raise
        show()
        sys.exit(EXIT_USAGE)
    sys.exit(code if isinstance(code, int) else 0)


if __name__ == "__main__":
    main()
