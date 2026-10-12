import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
import typer
from typer.testing import CliRunner

from zai_sensor import cli
from zai_sensor.sources.open_meteo import FORECAST_URL

URL = "http://localhost:8000"
MEASUREMENTS = f"{URL}/api/measurements"
KEY = "zai_test-sensor-key"
runner = CliRunner()


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ZAI_API_URL", "ZAI_API_KEY", "ZAI_ADMIN_PASSWORD"):
        monkeypatch.delenv(name, raising=False)


def run(*args: str, key: str | None = KEY, input_: str | None = None) -> Any:
    base = ["--api-url", URL]
    if key:
        base += ["--api-key", key]
    return runner.invoke(cli.app, [*base, *args], input=input_)


def created(id_: int = 1, **extra: Any) -> httpx.Response:
    return httpx.Response(
        201, json={"id": id_, "seriesId": 3, "timestamp": "2026-10-11T12:00:00Z", **extra}
    )


def mock_unit() -> None:
    respx.get(f"{URL}/api/series/3").mock(return_value=httpx.Response(200, json={"unit": "°C"}))


def bodies(route: respx.Route) -> list[dict[str, Any]]:
    return [json.loads(call.request.content) for call in route.calls]


# --- send --------------------------------------------------------------------------------------


@respx.mock
def test_send_prints_the_accepted_measurement_and_a_summary() -> None:
    respx.post(MEASUREMENTS).mock(return_value=created(1234))
    mock_unit()
    result = run("send", "--value", "23.4")
    assert result.exit_code == 0
    assert "✔ #1234 23.4 °C @ 2026-10-11T12:00:00Z" in result.stdout
    assert "sent=1 accepted=1 rejected=0 failed=0" in result.stdout


@respx.mock
def test_send_422_is_a_visible_rejection_with_exit_code_1() -> None:
    respx.post(MEASUREMENTS).mock(
        return_value=httpx.Response(
            422, json={"title": "Unprocessable", "detail": "value 999 outside [0, 500] for 'PM2.5'"}
        )
    )
    result = run("send", "--value", "999")
    assert result.exit_code == 1
    assert "✘ REJECTED (422): value 999 outside [0, 500] for 'PM2.5'" in result.stdout
    assert "sent=1 accepted=0 rejected=1 failed=0" in result.stdout


@respx.mock
def test_send_401_says_the_key_was_rejected_with_exit_code_2() -> None:
    respx.post(MEASUREMENTS).mock(return_value=httpx.Response(401, json={"title": "Unauthorized"}))
    result = run("send", "--value", "1")
    assert result.exit_code == 2
    assert "✘ Sensor key rejected – sensor unregistered or key invalid" in result.stdout
    assert KEY not in result.output


@respx.mock
def test_send_timeout_is_a_failure_and_is_not_retried() -> None:
    route = respx.post(MEASUREMENTS).mock(side_effect=httpx.ReadTimeout("slow"))
    result = run("send", "--value", "1")
    assert result.exit_code == 1
    assert "✘ FAILED" in result.stdout
    assert route.call_count == 1


@respx.mock
def test_send_retries_429_and_then_succeeds(no_sleeping: list[float]) -> None:
    respx.post(MEASUREMENTS).mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "2"}), created()]
    )
    mock_unit()
    result = run("send", "--value", "1")
    assert result.exit_code == 0
    assert no_sleeping == [2.0]


@respx.mock
def test_send_with_a_timestamp_sends_utc() -> None:
    route = respx.post(MEASUREMENTS).mock(return_value=created())
    mock_unit()
    run("send", "--value", "1", "--timestamp", "2026-10-01T10:00:00+02:00")
    assert bodies(route) == [{"value": 1.0, "timestamp": "2026-10-01T08:00:00Z"}]


@respx.mock
def test_the_key_comes_from_the_environment() -> None:
    route = respx.post(MEASUREMENTS).mock(return_value=created())
    mock_unit()
    result = runner.invoke(
        cli.app, ["send", "--value", "1"], env={"ZAI_API_URL": URL, "ZAI_API_KEY": "zai_from_env"}
    )
    assert result.exit_code == 0
    assert route.calls.last.request.headers["x-api-key"] == "zai_from_env"


@respx.mock
def test_without_a_key_it_is_prompted_hidden_and_never_echoed() -> None:
    route = respx.post(MEASUREMENTS).mock(return_value=created())
    mock_unit()
    result = run("send", "--value", "1", key=None, input_="zai_typed_key\n")
    assert result.exit_code == 0
    assert route.calls.last.request.headers["x-api-key"] == "zai_typed_key"
    assert "zai_typed_key" not in result.output


@respx.mock
def test_dry_run_sends_nothing_and_needs_no_key() -> None:
    result = runner.invoke(cli.app, ["--api-url", URL, "--dry-run", "send", "--value", "5"])
    assert result.exit_code == 0
    assert "would send 5" in result.stdout
    assert "sent=1 accepted=0 rejected=0 failed=0" in result.stdout


@respx.mock
def test_verbose_logs_requests_but_never_the_key() -> None:
    respx.post(MEASUREMENTS).mock(return_value=created())
    mock_unit()
    result = runner.invoke(
        cli.app, ["--api-url", URL, "--api-key", KEY, "--verbose", "send", "--value", "1"]
    )
    assert "POST /api/measurements" in result.stderr
    assert KEY not in result.output


# --- usage errors ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        ["send", "--value", "nan"],
        ["send", "--value", "1", "--timestamp", "yesterday"],
        ["send", "--value", "1", "--timestamp", "2026-10-01T10:00:00"],  # naive
        ["--timeout", "soon", "send", "--value", "1"],
        ["backfill"],
        ["backfill", "--count", "3", "--start", "2026-10-01T00:00:00Z"],
        ["backfill", "--count", "3", "--end", "2999-01-01T00:00:00Z"],
        ["backfill", "--start", "2026-10-02T00:00:00Z", "--end", "2026-10-01T00:00:00Z"],
        ["backfill", "--count", "3", "--interval", "never"],
        ["backfill", "--count", "3", "--min", "5", "--max", "1"],
        ["backfill", "--count", "3", "--period", "0s"],
        ["backfill", "--source", "open-meteo", "--start", "2020-01-01T00:00:00Z"],
        ["live", "--interval", "0s"],
    ],
)
def test_invalid_input_exits_64_before_any_request(args: list[str]) -> None:
    with respx.mock:  # any request would raise
        result = run(*args)
    assert result.exit_code == 64
    assert "error:" in result.stderr


def test_the_api_url_is_required() -> None:
    result = runner.invoke(cli.app, ["--api-key", KEY, "send", "--value", "1"])
    assert result.exit_code == 64
    assert "no API URL" in result.stderr


@pytest.mark.parametrize("url", ["ftp://x", "localhost:8000", "http://"])
def test_the_api_url_must_be_http(url: str) -> None:
    result = runner.invoke(cli.app, ["--api-url", url, "--api-key", KEY, "send", "--value", "1"])
    assert result.exit_code == 64


REMOTE_HTTP = "http://api.example.org"


@pytest.mark.parametrize(
    "args",
    [
        ["send", "--value", "1"],
        ["backfill", "--count", "2"],
        ["live", "--count", "1"],
        ["seed", "--config", str(Path(__file__).parents[1] / "seed.example.toml")],
    ],
)
def test_plain_http_to_a_remote_host_is_refused_before_anything_is_sent_or_asked(
    args: list[str],
) -> None:
    with respx.mock:  # any request would raise
        result = runner.invoke(
            cli.app, ["--api-url", REMOTE_HTTP, "--api-key", KEY, *args], input="never-read\n"
        )
    assert result.exit_code == 64
    assert "refusing plain http://" in result.stderr
    assert "Password" not in result.output  # the admin password prompt never appeared
    assert KEY not in result.output


@respx.mock
def test_allow_insecure_lets_plain_http_through_with_a_warning() -> None:
    route = respx.post(f"{REMOTE_HTTP}/api/measurements").mock(return_value=created())
    respx.get(f"{REMOTE_HTTP}/api/series/3").mock(return_value=httpx.Response(200, json={}))
    result = runner.invoke(
        cli.app,
        ["--api-url", REMOTE_HTTP, "--api-key", KEY, "--allow-insecure", "send", "--value", "1"],
    )
    assert result.exit_code == 0
    assert route.call_count == 1
    assert "travel unencrypted" in result.stderr


@respx.mock
@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "http://127.0.0.2:8000",
        "http://[::1]:8000",
        "http://api.localhost:8000",
    ],
)
def test_plain_http_to_loopback_is_fine(url: str) -> None:
    route = respx.post(f"{url}/api/measurements").mock(return_value=created())
    respx.get(f"{url}/api/series/3").mock(return_value=httpx.Response(200, json={}))
    result = runner.invoke(cli.app, ["--api-url", url, "--api-key", KEY, "send", "--value", "1"])
    assert result.exit_code == 0
    assert route.call_count == 1
    assert "unencrypted" not in result.stderr


@respx.mock
def test_https_to_a_remote_host_is_fine() -> None:
    respx.post("https://api.example.org/api/measurements").mock(return_value=created())
    respx.get("https://api.example.org/api/series/3").mock(
        return_value=httpx.Response(200, json={})
    )
    result = runner.invoke(
        cli.app, ["--api-url", "https://api.example.org", "--api-key", KEY, "send", "--value", "1"]
    )
    assert result.exit_code == 0
    assert "unencrypted" not in result.stderr


def test_dry_run_sends_nothing_so_it_may_name_any_host() -> None:
    result = runner.invoke(cli.app, ["--api-url", REMOTE_HTTP, "--dry-run", "send", "--value", "1"])
    assert result.exit_code == 0


def test_main_maps_click_usage_errors_to_64(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["zai-sensor", "send", "--no-such-option"])
    with pytest.raises(SystemExit) as raised:
        cli.main()
    assert raised.value.code == 64


@respx.mock
def test_main_passes_the_command_exit_code_through(monkeypatch: pytest.MonkeyPatch) -> None:
    respx.post(MEASUREMENTS).mock(return_value=httpx.Response(401, json={}))
    monkeypatch.setattr(
        sys, "argv", ["zai-sensor", "--api-url", URL, "--api-key", KEY, "send", "--value", "1"]
    )
    with pytest.raises(SystemExit) as raised:
        cli.main()
    assert raised.value.code == 2


def test_help_documents_the_exit_codes() -> None:
    result = runner.invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    assert "Exit codes" in result.stdout and "64" in result.stdout


# --- backfill ----------------------------------------------------------------------------------


@respx.mock
def test_backfill_sends_past_hourly_points_oldest_first() -> None:
    route = respx.post(MEASUREMENTS).mock(return_value=created())
    mock_unit()
    end = "2026-10-11T12:00:00Z"
    result = run("backfill", "--count", "3", "--end", end, "--pattern", "constant", "--offset", "5")
    assert result.exit_code == 0
    assert [b["timestamp"] for b in bodies(route)] == [
        "2026-10-11T10:00:00Z",
        "2026-10-11T11:00:00Z",
        "2026-10-11T12:00:00Z",
    ]
    assert {b["value"] for b in bodies(route)} == {5.0}
    assert "sent=3 accepted=3 rejected=0 failed=0" in result.stdout


@respx.mock
def test_backfill_start_and_end_define_the_grid() -> None:
    route = respx.post(MEASUREMENTS).mock(return_value=created())
    mock_unit()
    run(
        "backfill", "--start", "2026-10-01T00:00:00Z", "--end", "2026-10-01T02:00:00Z",
        "--interval", "30m",
    )  # fmt: skip
    assert len(bodies(route)) == 5


@respx.mock
def test_backfill_is_repeatable_with_a_seed() -> None:
    args = (
        "backfill",
        "--count",
        "5",
        "--end",
        "2026-10-11T12:00:00Z",
        "--pattern",
        "uniform",
        "--seed",
        "9",
    )
    first = respx.post(MEASUREMENTS).mock(return_value=created())
    mock_unit()
    run(*args)
    first_values = [b["value"] for b in bodies(first)]
    run(*args)
    assert [b["value"] for b in bodies(first)] == first_values * 2


@respx.mock
def test_out_of_range_every_produces_visible_rejections_and_the_run_continues() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        value = json.loads(request.content)["value"]
        if value > 100:
            return httpx.Response(422, json={"detail": f"value {value:g} outside [0, 100]"})
        return created()

    route = respx.post(MEASUREMENTS).mock(side_effect=respond)
    mock_unit()
    result = run(
        "backfill", "--count", "6", "--end", "2026-10-11T12:00:00Z", "--pattern", "uniform",
        "--seed", "1", "--out-of-range-every", "3",
    )  # fmt: skip
    assert route.call_count == 6  # nothing stopped the run
    assert result.stdout.count("✘ REJECTED (422)") == 2
    assert "sent=6 accepted=4 rejected=2 failed=0" in result.stdout
    assert result.exit_code == 1


@respx.mock
def test_backfill_stops_at_the_first_401() -> None:
    route = respx.post(MEASUREMENTS).mock(return_value=httpx.Response(401, json={}))
    result = run("backfill", "--count", "10", "--end", "2026-10-11T12:00:00Z")
    assert route.call_count == 1
    assert result.exit_code == 2


@respx.mock
def test_backfill_is_quiet_for_long_runs_unless_asked() -> None:
    respx.post(MEASUREMENTS).mock(return_value=created())
    mock_unit()
    quiet = run("backfill", "--count", "30", "--end", "2026-10-11T12:00:00Z")
    loud = run("backfill", "--count", "30", "--end", "2026-10-11T12:00:00Z", "--show-each")
    assert "✔" not in quiet.stdout and "accepted=30" in quiet.stdout
    assert loud.stdout.count("✔") == 30


def hourly_body(variable: str) -> dict[str, Any]:
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    times = [(now.timestamp() - h * 3600) for h in range(6, -1, -1)]
    return {
        "hourly": {
            "time": [datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M") for t in times],
            variable: [1.0, 2.0, None, 4.0, 5.0, 6.0, 7.0],
        }
    }


@respx.mock
def test_backfill_from_open_meteo_sends_aware_utc_times_and_skips_nulls() -> None:
    forecast = respx.get(FORECAST_URL).mock(
        return_value=httpx.Response(200, json=hourly_body("temperature_2m"))
    )
    route = respx.post(MEASUREMENTS).mock(return_value=created())
    mock_unit()
    result = run("backfill", "--source", "open-meteo", "--count", "4")
    assert result.exit_code == 0
    sent = bodies(route)
    assert [b["value"] for b in sent] == [4.0, 5.0, 6.0, 7.0]  # the last 4 non-null points
    assert all(b["timestamp"].endswith("Z") for b in sent)
    assert forecast.calls.last.request.url.params["past_days"] == "2"


@respx.mock
def test_open_meteo_failure_is_fatal_exit_3() -> None:
    respx.get(FORECAST_URL).mock(return_value=httpx.Response(500, text="down"))
    result = run("backfill", "--source", "open-meteo", "--count", "4")
    assert result.exit_code == 3
    assert "Open-Meteo answered HTTP 500" in result.stderr


@respx.mock
def test_open_meteo_without_data_in_range_is_fatal() -> None:
    respx.get(FORECAST_URL).mock(
        return_value=httpx.Response(200, json={"hourly": {"time": [], "temperature_2m": []}})
    )
    assert run("backfill", "--source", "open-meteo", "--count", "4").exit_code == 3


def test_backfill_dry_run_lists_without_sending() -> None:
    result = runner.invoke(
        cli.app,
        [
            "--api-url",
            URL,
            "--dry-run",
            "backfill",
            "--count",
            "3",
            "--end",
            "2026-10-11T12:00:00Z",
        ],
    )
    assert result.exit_code == 0
    assert result.stdout.count("would send") == 3


# --- live --------------------------------------------------------------------------------------


@respx.mock
def test_live_sends_without_timestamps_at_the_given_interval(no_sleeping: list[float]) -> None:
    route = respx.post(MEASUREMENTS).mock(return_value=created())
    mock_unit()
    result = run(
        "live", "--interval", "2s", "--count", "3", "--pattern", "constant", "--offset", "7"
    )
    assert result.exit_code == 0
    assert bodies(route) == [{"value": 7.0}] * 3  # the server stamps them
    assert no_sleeping == [2.0, 2.0]
    assert "sent=3 accepted=3" in result.stdout


@respx.mock
def test_live_ctrl_c_prints_the_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    respx.post(MEASUREMENTS).mock(return_value=created())
    mock_unit()
    pauses: list[float] = []

    def interrupt(seconds: float) -> None:
        pauses.append(seconds)
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "sleep", interrupt)
    result = run("live", "--interval", "1s")
    assert result.exit_code == 0
    assert "sent=1 accepted=1 rejected=0 failed=0" in result.stdout


@respx.mock
def test_live_from_open_meteo_uses_the_current_value() -> None:
    respx.get(FORECAST_URL).mock(
        return_value=httpx.Response(
            200, json={"current": {"time": "2026-10-11T12:15", "temperature_2m": 9.5}}
        )
    )
    route = respx.post(MEASUREMENTS).mock(return_value=created())
    mock_unit()
    result = run("live", "--source", "open-meteo", "--count", "2")
    assert result.exit_code == 0
    assert bodies(route) == [{"value": 9.5}, {"value": 9.5}]


@respx.mock
def test_live_skips_ticks_without_a_current_value() -> None:
    respx.get(FORECAST_URL).mock(
        return_value=httpx.Response(200, json={"current": {"temperature_2m": None}})
    )
    route = respx.post(MEASUREMENTS).mock(return_value=created())
    result = run("live", "--source", "open-meteo", "--count", "2")
    assert route.call_count == 0
    assert "no current value" in result.stderr
    assert "sent=0" in result.stdout


@respx.mock
def test_live_stops_when_the_key_is_rejected() -> None:
    route = respx.post(MEASUREMENTS).mock(return_value=httpx.Response(401, json={}))
    result = run("live", "--count", "5")
    assert route.call_count == 1
    assert result.exit_code == 2


# --- seed --------------------------------------------------------------------------------------

SEED_FILE = Path(__file__).parents[1] / "seed.example.toml"


def test_seed_dry_run_lists_the_plan_without_logging_in() -> None:
    result = runner.invoke(
        cli.app, ["--api-url", URL, "--dry-run", "seed", "--config", str(SEED_FILE)]
    )
    assert result.exit_code == 0
    assert "Warsaw – PM2.5: 336 hourly points (pm2_5)" in result.stdout
    assert "Lab – Indoor temperature (synthetic): 336 hourly points (synthetic)" in result.stdout


def test_seed_with_a_broken_file_exits_3(tmp_path: Path) -> None:
    broken = tmp_path / "seed.toml"
    broken.write_text("series = []", encoding="utf-8")
    result = run("seed", "--config", str(broken), key=None)
    assert result.exit_code == 3
    assert "at least one" in result.stderr


@respx.mock
def test_seed_wrong_admin_password_exits_3_and_never_prints_it() -> None:
    respx.post(f"{URL}/api/auth/login").mock(
        return_value=httpx.Response(401, json={"detail": "Invalid credentials."})
    )
    result = runner.invoke(
        cli.app,
        ["--api-url", URL, "seed", "--config", str(SEED_FILE)],
        env={"ZAI_ADMIN_PASSWORD": "s3cret-admin-pw"},
    )
    assert result.exit_code == 3
    assert "Invalid credentials" in result.stderr
    assert "s3cret-admin-pw" not in result.output


@respx.mock
def test_seed_prompts_for_the_admin_password_hidden() -> None:
    login = respx.post(f"{URL}/api/auth/login").mock(
        return_value=httpx.Response(401, json={"detail": "Invalid credentials."})
    )
    result = runner.invoke(
        cli.app, ["--api-url", URL, "seed", "--config", str(SEED_FILE)], input="typed-pw\n"
    )
    assert json.loads(login.calls.last.request.content)["password"] == "typed-pw"
    assert "typed-pw" not in result.output


def test_abort_at_a_prompt_is_a_quiet_exit_130(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(*_: object, **__: object) -> str:
        raise typer.Abort

    monkeypatch.setattr(typer, "prompt", refuse)
    monkeypatch.setattr(sys, "argv", ["zai-sensor", "--api-url", URL, "send", "--value", "1"])
    with pytest.raises(SystemExit) as raised:
        cli.main()
    assert raised.value.code == 130
    captured = capsys.readouterr()
    assert "Aborted." in captured.err
    assert "Traceback" not in captured.err
