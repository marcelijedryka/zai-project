import pytest

import zai_sensor.cli
import zai_sensor.client


@pytest.fixture(autouse=True)
def no_sleeping(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Backoff and live-mode pauses are recorded instead of slept."""
    slept: list[float] = []
    monkeypatch.setattr(zai_sensor.client, "_sleep", slept.append)
    monkeypatch.setattr(zai_sensor.cli, "sleep", slept.append)
    return slept
