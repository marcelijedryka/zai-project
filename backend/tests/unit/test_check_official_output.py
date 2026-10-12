"""The checker that turns the official script's output (always exit code 0) into a CI verdict.

The fixtures are real outputs of ``contract/zai-tests.mjs`` against the composed stack.
"""

import io
import re
from pathlib import Path

import pytest

from scripts.check_official_output import DEPLOYMENT_ONLY, main, parse, problems

FIXTURES = Path(__file__).parents[1] / "fixtures" / "official_output"
OFFICIAL_SCRIPT = Path(__file__).parents[3] / "contract" / "zai-tests.mjs"
E1_ARGS = ["--min-tests", "23"]
E2_ARGS = ["--min-tests", "45", "--allow-deployment-only"]


def fixture(name: str) -> str:
    return str(FIXTURES / name)


def mutated(tmp_path: Path, name: str, old: str, new: str) -> str:
    text = (FIXTURES / name).read_text(encoding="utf-8")
    assert old in text
    path = tmp_path / name
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    return str(path)


def test_clean_e1_run_passes(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([*E1_ARGS, fixture("e1_pass.txt")]) == 0
    assert "23 passed" in capsys.readouterr().out


def test_e2_with_only_the_three_deployment_failures_passes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main([*E2_ARGS, fixture("e2_deployment_only_failures.txt")]) == 0
    assert "3 deployment-only" in capsys.readouterr().out


def test_deployment_failures_are_not_tolerated_unless_allowed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["--min-tests", "45", fixture("e2_deployment_only_failures.txt")]) == 1
    assert "HTTPS" in capsys.readouterr().err


def test_any_other_failure_fails_even_when_deployment_failures_are_allowed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = mutated(
        tmp_path,
        "e2_deployment_only_failures.txt",
        "✔ GET /api/health zwraca 200",
        "✘ GET /api/health zwraca 200",
    )
    assert main([*E2_ARGS, path]) == 1
    assert "FAILED: GET /api/health" in capsys.readouterr().err


def test_failure_reason_is_reported(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = mutated(
        tmp_path,
        "e1_pass.txt",
        "✔ Filtrowanie from/to jest domknięte",
        "✘ Filtrowanie from/to jest domknięte\n      → Oczekiwano 200, otrzymano 500",
    )
    assert main([*E1_ARGS, path]) == 1
    assert "otrzymano 500" in capsys.readouterr().err


def test_one_deployment_only_name_does_not_excuse_a_lookalike(tmp_path: Path) -> None:
    path = mutated(
        tmp_path,
        "e2_deployment_only_failures.txt",
        "✘ Aplikacja i API działają przez HTTPS",
        "✘ Aplikacja i API działają przez HTTPS (inaczej)",
    )
    assert main([*E2_ARGS, path]) == 1


@pytest.mark.parametrize("name", ["empty.txt", "unreachable.txt"])
def test_empty_or_unreachable_output_fails(name: str) -> None:
    assert main([*E1_ARGS, fixture(name)]) == 1
    assert main([*E2_ARGS, fixture(name)]) == 1


def test_truncated_output_fails(tmp_path: Path) -> None:
    lines = (FIXTURES / "e1_pass.txt").read_text(encoding="utf-8").splitlines()
    cut = tmp_path / "cut.txt"
    cut.write_text("\n".join(lines[:10]), encoding="utf-8")
    found = problems(parse(lines[:10]), min_tests=23, allow_deployment_only=False)
    assert any("expected at least 23" in item for item in found)
    assert any("summary is missing" in item for item in found)
    assert main([*E1_ARGS, str(cut)]) == 1


def test_reads_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    text = (FIXTURES / "e1_pass.txt").read_text(encoding="utf-8")
    monkeypatch.setattr("sys.stdin", io.StringIO(text))
    assert main(E1_ARGS) == 0


def test_deployment_only_names_exist_in_the_official_script() -> None:
    """If the script renames a test, the allow-list must be revisited (it never matches by luck)."""
    source = OFFICIAL_SCRIPT.read_text(encoding="utf-8")
    names = set(re.findall(r"test\('bezpieczenstwo', '([^']+)'", source))
    assert names >= DEPLOYMENT_ONLY
