"""Terminal output of the generator (colours are stripped automatically when not a terminal)."""

import typer

from zai_sensor.models import Outcome, Point, Summary

OK = "✔"
BAD = "✘"


class Reporter:
    def __init__(self, *, verbose: bool = False) -> None:
        self.verbose = verbose

    def info(self, line: str) -> None:
        typer.echo(line)

    def debug(self, line: str) -> None:
        if self.verbose:
            typer.secho(f"  {line}", err=True, dim=True)

    def error(self, line: str) -> None:
        typer.secho(f"error: {line}", err=True, fg=typer.colors.RED)

    def warning(self, line: str) -> None:
        typer.secho(f"warning: {line}", err=True, fg=typer.colors.YELLOW)

    def accepted(self, outcome: Outcome, point: Point, unit: str) -> None:
        unit_part = f" {unit}" if unit else ""
        typer.secho(
            f"{OK} #{outcome.measurement_id} {point.value:g}{unit_part} @ {outcome.timestamp}",
            fg=typer.colors.GREEN,
        )

    def dry_run(self, point: Point) -> None:
        when = point.timestamp.isoformat().replace("+00:00", "Z") if point.timestamp else "now"
        typer.echo(f"- would send {point.value:g} @ {when}")

    def rejected(self, outcome: Outcome) -> None:
        typer.secho(f"{BAD} REJECTED ({outcome.status}): {outcome.message}", fg=typer.colors.RED)

    def key_rejected(self) -> None:
        typer.secho(
            f"{BAD} Sensor key rejected – sensor unregistered or key invalid",  # noqa: RUF001
            fg=typer.colors.RED,
        )

    def failed(self, outcome: Outcome) -> None:
        typer.secho(f"{BAD} FAILED: {outcome.message}", fg=typer.colors.RED)

    def summary(self, summary: Summary) -> None:
        typer.echo(summary.line())
