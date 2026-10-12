"""Turn the output of the official ``contract/zai-tests.mjs`` into an exit code.

The official script always exits 0, so CI pipes its output through this checker:

    node contract/zai-tests.mjs ... --stage E1 | tee out.txt
    python backend/scripts/check_official_output.py --min-tests 23 out.txt

The run fails on any ``✘`` line, on a missing or truncated report (no result lines, fewer than
``--min-tests``, no final summary) and when the app was unreachable. ``--allow-deployment-only``
tolerates exactly the three checks that need the public HTTPS / CloudFront deployment.
"""

import argparse
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field

# Full test names from contract/zai-tests.mjs (a test pins that they still exist there).
DEPLOYMENT_ONLY = frozenset(
    {
        "Aplikacja i API działają przez HTTPS",
        "Wejście przez http:// przekierowuje na https:// (lub port 80 jest zamknięty)",
        "Strona główna wysyła nagłówek Content-Security-Policy",
    }
)

_RESULT = re.compile(r"^\s*(?P<mark>[✔✘]) (?P<name>.+?)\s*$")
_REASON = re.compile(r"^\s*→ (?P<reason>.+?)\s*$")
_SUMMARY = re.compile(r"^=== Etap \w+: ")


@dataclass
class Report:
    passed: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)  # (name, reason)
    has_summary: bool = False

    @property
    def total(self) -> int:
        return len(self.passed) + len(self.failed)


def parse(lines: Iterable[str]) -> Report:
    report = Report()
    for line in lines:
        if match := _RESULT.match(line):
            if match["mark"] == "✔":
                report.passed.append(match["name"])
            else:
                report.failed.append((match["name"], ""))
        elif (reason := _REASON.match(line)) and report.failed and not report.failed[-1][1]:
            report.failed[-1] = (report.failed[-1][0], reason["reason"])
        elif _SUMMARY.match(line):
            report.has_summary = True
    return report


def problems(report: Report, *, min_tests: int, allow_deployment_only: bool) -> list[str]:
    """Human-readable reasons why the run must fail (empty = the run is acceptable)."""
    found: list[str] = []
    if report.total == 0:
        return ["no test results found: the script printed nothing, or the app was unreachable"]
    allowed = DEPLOYMENT_ONLY if allow_deployment_only else frozenset()
    for name, reason in report.failed:
        if name not in allowed:
            found.append(f"FAILED: {name}" + (f"\n      -> {reason}" if reason else ""))
    if report.total < min_tests:
        found.append(f"only {report.total} test results found, expected at least {min_tests}")
    if not report.has_summary:
        found.append("the final '=== Etap ...' summary is missing: the run did not finish")
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    parser.add_argument("output", nargs="?", help="saved script output (default: stdin)")
    parser.add_argument("--min-tests", type=int, default=1, help="fail below this many results")
    parser.add_argument(
        "--allow-deployment-only",
        action="store_true",
        help="LOCAL runs only (stage E2, no HTTPS): tolerate the 3 checks that need the public "
        "deployment. Never use it against the deployed stack, where they must pass.",
    )
    args = parser.parse_args(argv)
    if args.output:
        with open(args.output, encoding="utf-8") as handle:
            report = parse(handle)
    else:
        report = parse(sys.stdin)
    found = problems(
        report, min_tests=args.min_tests, allow_deployment_only=args.allow_deployment_only
    )
    tolerated = len(report.failed) - sum(1 for f in found if f.startswith("FAILED"))
    if found:
        print(f"official suite check FAILED ({len(found)} problem(s)):", file=sys.stderr)
        for item in found:
            print(f"  - {item}", file=sys.stderr)
        return 1
    print(
        f"official suite check OK: {len(report.passed)} passed, "
        f"{tolerated} deployment-only failure(s) tolerated"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
