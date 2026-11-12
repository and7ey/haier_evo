"""The report of a CI job: JSON lines for scripts/ci_results.py.

With CHECKS_REPORT=<file> (scripts/run_checks.sh sets it) the run appends to
that file: first the versions it ran on, then one line per test file with how
many of its tests passed and failed, how long they took and what failed.
scripts/ci_results.py turns the reports of all versions into the summaries,
annotations and README badges. Without CHECKS_REPORT nothing is written.
"""
from __future__ import annotations

import json
import os
import platform
from importlib import metadata
from pathlib import Path

import pytest

REPORT = os.environ.get("CHECKS_REPORT")
MAX_FAILURES = 50     # failures kept per file; all of them are counted
MAX_TEXT = 1500       # characters kept per failure

_files: dict[str, dict] = {}


def _write(**record) -> None:
    if REPORT:
        with open(REPORT, "a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")


def _version(package: str) -> str | None:
    try:
        return metadata.version(package)
    except metadata.PackageNotFoundError:
        return None


def pytest_sessionstart(session: pytest.Session) -> None:
    from homeassistant.const import __version__ as homeassistant

    _write(homeassistant=homeassistant, python=platform.python_version(), pytest=pytest.__version__,
           plugin=_version("pytest-homeassistant-custom-component"))


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if not REPORT:
        return
    entry = _files.setdefault(
        Path(report.fspath).name, {"passed": set(), "failed": set(), "seconds": 0.0, "failures": []}
    )
    entry["seconds"] += report.duration
    # a test counts once: failed when any of its phases failed (a call and its
    # teardown can both fail), passed when its call passed and nothing failed
    if report.failed:
        entry["failed"].add(report.nodeid)
        if len(entry["failures"]) < MAX_FAILURES:
            text = f"{report.nodeid} ({report.when})\n{report.longreprtext}"
            entry["failures"].append(text[:MAX_TEXT])
    elif report.passed and report.when == "call":
        entry["passed"].add(report.nodeid)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    for name, entry in sorted(_files.items()):
        failed = len(entry["failed"])
        _write(script=name, passed=len(entry["passed"] - entry["failed"]), failed=failed, ok=not failed,
               seconds=round(entry["seconds"], 2), failures=entry["failures"])
