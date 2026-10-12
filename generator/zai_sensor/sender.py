"""Send a stream of points with one sensor key and report each outcome."""

from collections.abc import Callable, Iterable

from zai_sensor.client import ApiClient
from zai_sensor.models import Kind, Point, Summary
from zai_sensor.report import Reporter


class UnitLookup:
    """Looks up a series' unit once, after the first accepted measurement (display only)."""

    def __init__(self, client: ApiClient) -> None:
        self._client = client
        self._unit: str | None = None

    def get(self, series_id: int | None) -> str:
        if self._unit is None:
            self._unit = self._client.series_unit(series_id) if series_id else ""
        return self._unit


def send_points(
    client: ApiClient,
    api_key: str,
    points: Iterable[Point],
    reporter: Reporter,
    *,
    dry_run: bool = False,
    show_accepted: bool = True,
    on_point: Callable[[], None] | None = None,
    summary: Summary | None = None,
) -> Summary:
    """Post the points in order. A rejected value (422) is reported and sending continues; a
    refused key (401) stops at once, because every further request would fail the same way."""
    summary = summary if summary is not None else Summary()
    units = UnitLookup(client)
    for point in points:
        if dry_run:
            reporter.dry_run(point)
            summary.sent += 1
            if on_point:
                on_point()
            continue
        outcome = client.post_measurement(api_key, point)
        summary.add(outcome)
        if on_point:
            on_point()
        if outcome.kind is Kind.ACCEPTED:
            if show_accepted:
                reporter.accepted(outcome, point, units.get(outcome.series_id))
        elif outcome.kind is Kind.REJECTED:
            reporter.rejected(outcome)
        elif outcome.kind is Kind.KEY_REJECTED:
            reporter.key_rejected()
            break
        else:
            reporter.failed(outcome)
    return summary
