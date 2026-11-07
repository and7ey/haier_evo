"""pytest entry point: every check_*.py script runs in its own interpreter.

The scripts print one line per check (OK / FAIL) and exit non-zero on a
failure; they also run by hand: python tests/check_louvers.py

With CHECKS_REPORT set, JSON lines are appended to that file: first the
versions the checks ran on, then one line per script with how many of its
checks passed and failed, how long it took and what failed.
scripts/ci_results.py turns the reports of a CI run into its summaries,
annotations and the README badges.
"""
import json
import os
import platform
import subprocess
import sys
import time
from importlib import metadata
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SCRIPTS = sorted(p.name for p in HERE.glob("check_*.py"))
REPORT = os.environ.get("CHECKS_REPORT")
TIMEOUT = 600
MAX_FAILURES = 50     # FAIL lines kept per script; all of them are counted
MAX_TEXT = 1500       # characters kept per failure


def _report(**record) -> None:
    if REPORT:
        with open(REPORT, "a", encoding="utf-8") as file:
            file.write(json.dumps(record) + "\n")


def _version(package: str) -> str | None:
    try:
        return metadata.version(package)
    except metadata.PackageNotFoundError:
        return None


@pytest.fixture(scope="session", autouse=True)
def _report_versions():
    try:
        from homeassistant.const import __version__ as homeassistant
    except ImportError:
        homeassistant = None
    _report(homeassistant=homeassistant, python=platform.python_version(),
            pytest=pytest.__version__, websocket_client=_version("websocket-client"))


def _failures(lines: list[str]) -> list[str]:
    """Each FAIL line with the indented lines under it (got / want)."""
    failures, inside = [], False
    for line in lines:
        if line.startswith("FAIL "):
            failures.append(line)
            inside = True
        elif inside and line.startswith(" "):
            failures[-1] += "\n" + line
        else:
            inside = False
    return failures


@pytest.mark.parametrize("script", SCRIPTS)
def test_check_script(script: str) -> None:
    started = time.monotonic()
    try:
        proc = subprocess.run(
            [sys.executable, str(HERE / script)],
            cwd=HERE.parent, capture_output=True, text=True, timeout=TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        _report(script=script, passed=0, failed=1, ok=False, seconds=round(time.monotonic() - started, 2),
                failures=[f"no result after {TIMEOUT} s"])
        raise
    output = proc.stdout + proc.stderr
    lines = proc.stdout.splitlines()
    passed = sum(line.startswith("OK ") for line in lines)
    failures = _failures(lines)
    ok = proc.returncode == 0 and "FAILS: 0" in output
    if not ok and not failures:
        # the script broke before printing a FAIL line: one failed check, with the end of its output
        failures = [f"exit code {proc.returncode}:\n" + "\n".join(output.splitlines()[-30:])]
    _report(script=script, passed=passed, failed=len(failures), ok=ok,
            seconds=round(time.monotonic() - started, 2),
            failures=[text[:MAX_TEXT] for text in failures[:MAX_FAILURES]])
    assert ok, "\n".join(failures) or output[-4000:]
