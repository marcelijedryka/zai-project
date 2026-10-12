"""Helpers for running the ``zai-sensor`` CLI against a real server in tests."""

import asyncio
from pathlib import Path
from typing import Any

from typer.testing import CliRunner
from zai_sensor import cli

from tests.conftest import Admin

SEED_FILE = Path(__file__).parents[2] / "generator" / "seed.example.toml"
runner = CliRunner()


async def generator(url: str, *args: str, env: dict[str, str] | None = None) -> Any:
    """Run the CLI in a worker thread so the test's event loop stays free."""
    return await asyncio.to_thread(runner.invoke, cli.app, ["--api-url", url, *args], env=env)


async def seed(url: str, admin: Admin, *extra: str, days: int = 2) -> Any:
    """``zai-sensor seed`` with the example file, offline (synthetic): no Open-Meteo in tests."""
    return await generator(
        url,
        "seed", "--config", str(SEED_FILE), "--admin-user", admin.username, "--source", "synthetic",
        "--days", str(days), *extra,
        env={"ZAI_ADMIN_PASSWORD": admin.password},
    )  # fmt: skip
