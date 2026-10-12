import json
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.fake_api import FakeApi
from zai_sensor.client import ApiClient, ApiError
from zai_sensor.report import Reporter
from zai_sensor.seed import (
    ConfigError,
    SeedConfig,
    load_config,
    parse_config,
    run_seed,
    write_keys,
)
from zai_sensor.sources.open_meteo import OpenMeteo

EXAMPLE = Path(__file__).parents[1] / "seed.example.toml"
NOW = datetime(2026, 10, 11, 12, 30, tzinfo=UTC)


@pytest.fixture
def config() -> SeedConfig:
    return load_config(EXAMPLE)


def seed(
    api: FakeApi,
    config: SeedConfig,
    *,
    password: str = "pw",
    open_meteo: OpenMeteo | None = None,
    **kwargs: Any,
) -> Any:
    client = ApiClient("http://api.test", transport=api.transport())
    return run_seed(
        client, config, "admin", password, Reporter(), now=NOW, open_meteo=open_meteo, **kwargs
    )


def meteo(handler: Any) -> OpenMeteo:
    return OpenMeteo(client=httpx.Client(transport=httpx.MockTransport(handler)))


# --- the seed file -----------------------------------------------------------------------------


def test_the_example_file_describes_the_five_series_of_the_plan(config: SeedConfig) -> None:
    assert config.days == 14
    assert [(s.name, s.unit, s.minimum, s.maximum) for s in config.series] == [
        ("Warsaw – Air temperature", "°C", -40, 50),
        ("Warsaw – PM2.5", "µg/m³", 0, 500),
        ("Warsaw – PM10", "µg/m³", 0, 600),
        ("Warsaw – NO₂", "µg/m³", 0, 400),
        ("Lab – Indoor temperature (synthetic)", "°C", 10, 35),
    ]
    assert [s.variable for s in config.series] == [
        "temperature_2m",
        "pm2_5",
        "pm10",
        "nitrogen_dioxide",
        None,
    ]
    assert not any(s.name.upper().startswith("ZAI-TEST-") for s in config.series)
    assert (config.latitude, config.longitude) == (52.2297, 21.0122)


def minimal(**series: Any) -> dict[str, Any]:
    item = {
        "name": "S",
        "min": 0,
        "max": 10,
        "color": "#112233",
        "synthetic": {"pattern": "constant"},
    }
    return {"series": [{**item, **series}]}


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({}, "at least one"),
        ({"series": []}, "at least one"),
        ({"days": 0, **minimal()}, "seed.days"),
        ({"days": 500, **minimal()}, "seed.days"),
        (minimal(name="ZAI-TEST-x"), "ZAI-TEST-"),
        (minimal(name="zai-test-x"), "ZAI-TEST-"),
        (minimal(name=""), "name"),
        (minimal(min="a"), "min"),
        (minimal(min=5, max=5), "--min must be smaller"),
        (minimal(synthetic={"pattern": "wobble"}), "invalid pattern"),
        (minimal(synthetic={"pattern": "sine", "period": "soon"}), "period"),
        (minimal(synthetic=None), "must be a table"),
        ({"series": ["x"]}, "must be a table"),
        ({"location": 1, **minimal()}, "must be a table"),
    ],
)
def test_invalid_seed_files_are_refused(data: dict[str, Any], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        parse_config(data)


def test_unreadable_or_broken_files_are_config_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot read"):
        load_config(tmp_path / "missing.toml")
    broken = tmp_path / "broken.toml"
    broken.write_text("days = [", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid TOML"):
        load_config(broken)


# --- seeding -----------------------------------------------------------------------------------


def test_synthetic_seed_creates_series_sensors_and_hourly_history(config: SeedConfig) -> None:
    api = FakeApi()
    result = seed(api, config, synthetic_only=True)
    assert result.exit_code == 0
    assert len(api.series) == 5
    assert len(api.sensors) == 5  # one sensor per series
    for item in api.series:
        stamps = [
            datetime.fromisoformat(m["timestamp"])
            for m in api.measurements
            if m["seriesId"] == item["id"]
        ]
        assert len(stamps) == 14 * 24
        assert stamps == sorted(stamps)
        assert all(s <= NOW and s.minute == 0 for s in stamps)
        assert stamps[-1] == NOW.replace(minute=0)
        assert stamps[0] == stamps[-1] - timedelta(hours=14 * 24 - 1)
    assert api.logged_out


def test_series_bodies_carry_the_configured_style(config: SeedConfig) -> None:
    api = FakeApi()
    seed(api, config, synthetic_only=True)
    first = api.series[0]
    assert (first["color"], first["icon"], first["unit"]) == ("#1f77b4", "circle", "°C")


def test_synthetic_values_stay_inside_each_series_range(config: SeedConfig) -> None:
    api = FakeApi()
    result = seed(api, config, synthetic_only=True)
    assert all(s.rejected == 0 for s in result.summaries.values())


def test_seeding_is_idempotent(config: SeedConfig) -> None:
    api = FakeApi()
    seed(api, config, synthetic_only=True)
    stored, sensors = len(api.measurements), len(api.sensors)
    second = seed(api, config, synthetic_only=True)
    assert second.skipped == [s.name for s in config.series]
    assert (len(api.measurements), len(api.sensors), len(api.series)) == (stored, sensors, 5)
    assert api.logged_out


def test_an_existing_empty_series_gets_a_sensor_and_data(config: SeedConfig) -> None:
    api = FakeApi()
    first = config.series[0]
    api.series.append(
        {"id": 1, "name": first.name, "minValue": first.minimum, "maxValue": first.maximum}
    )
    result = seed(api, config, synthetic_only=True)
    assert result.created == [s.name for s in config.series[1:]]
    assert api.count(1) == 14 * 24


def test_seeding_is_deterministic(config: SeedConfig) -> None:
    a, b = FakeApi(), FakeApi()
    seed(a, config, synthetic_only=True)
    seed(b, config, synthetic_only=True)
    assert [m["value"] for m in a.measurements] == [m["value"] for m in b.measurements]


def test_a_wrong_admin_password_stops_before_anything_is_created(config: SeedConfig) -> None:
    api = FakeApi()
    with pytest.raises(ApiError, match="Invalid credentials"):
        seed(api, config, password="nope", synthetic_only=True)
    assert api.series == [] and api.sensors == {}


def test_open_meteo_values_are_used_for_the_real_series(config: SeedConfig) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        variable = request.url.params["hourly"]
        times = [f"2026-10-11T{h:02d}:00" for h in range(0, 13)]
        values: list[Any] = [round(10 + h / 10, 1) for h in range(13)]
        values[3] = None  # a gap must simply be skipped
        return httpx.Response(200, json={"hourly": {"time": times, variable: values}})

    api = FakeApi()
    result = seed(api, config, open_meteo=meteo(handler))
    assert result.exit_code == 0
    temperature = [m for m in api.measurements if m["seriesId"] == 1]
    assert len(temperature) == 12  # 13 hours minus the null
    assert temperature[0]["timestamp"] == "2026-10-11T00:00:00Z"
    assert temperature[0]["value"] == 10.0
    lab = [m for m in api.measurements if m["seriesId"] == 5]
    assert len(lab) == 14 * 24  # the lab series is synthetic whatever the source


def test_a_failing_source_skips_only_that_series(config: SeedConfig) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        variable = request.url.params["hourly"]
        if variable == "pm10":
            return httpx.Response(500, text="down")
        return httpx.Response(200, json={"hourly": {"time": ["2026-10-11T10:00"], variable: [5.0]}})

    api = FakeApi()
    result = seed(api, config, open_meteo=meteo(handler))
    assert list(result.errors) == ["Warsaw – PM10"]
    assert result.exit_code == 1
    assert api.count(3) == 0
    assert api.count(1) == 1 and api.count(5) == 14 * 24


def test_a_source_without_usable_points_is_an_error(config: SeedConfig) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        variable = request.url.params["hourly"]
        return httpx.Response(
            200, json={"hourly": {"time": ["2026-10-11T10:00"], variable: [None]}}
        )

    result = seed(FakeApi(), config, open_meteo=meteo(handler))
    assert len(result.errors) == 4


def test_a_revoked_key_stops_the_seed(config: SeedConfig) -> None:
    api = FakeApi()
    original = api.handle

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/measurements":
            return httpx.Response(401, json={"title": "Unauthorized"})
        return original(request)

    api.handle = handle  # type: ignore[method-assign]
    result = seed(api, config, synthetic_only=True)
    assert result.exit_code == 2
    assert len(result.summaries) == 1  # it did not carry on with the other series
    assert api.logged_out


# --- keys --------------------------------------------------------------------------------------


def test_keys_are_only_written_when_asked_and_with_owner_only_permissions(
    config: SeedConfig, tmp_path: Path
) -> None:
    path = tmp_path / "keys.json"
    seed(FakeApi(), config, synthetic_only=True)
    assert not path.exists()

    api = FakeApi()
    seed(api, config, synthetic_only=True, keys_path=path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert set(saved) == {s.name for s in config.series}
    assert set(saved.values()) == set(api.sensors)


def test_write_keys_tightens_an_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "keys.json"
    path.write_text("old", encoding="utf-8")
    path.chmod(0o644)
    write_keys(path, {"a": "zai_x"})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text(encoding="utf-8")) == {"a": "zai_x"}


def test_keys_are_not_printed(config: SeedConfig, capsys: pytest.CaptureFixture[str]) -> None:
    api = FakeApi()
    seed(api, config, synthetic_only=True)
    out = capsys.readouterr()
    assert not any(key in out.out + out.err for key in api.sensors)
