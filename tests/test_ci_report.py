"""The CI report counts every test once."""
from __future__ import annotations

import json
from types import SimpleNamespace

from . import ci_report


def report(nodeid: str, when: str, outcome: str):
    return SimpleNamespace(
        fspath="tests/test_x.py", nodeid=nodeid, when=when, duration=0.5, longreprtext="boom",
        failed=outcome == "failed", passed=outcome == "passed",
    )


def test_a_test_failing_twice_counts_once(tmp_path):
    out = tmp_path / "report.jsonl"
    # the module's own state, put back before the test returns: the report of
    # this very test goes there after it (monkeypatch undoes only at teardown)
    saved = ci_report.REPORT, ci_report._files
    ci_report.REPORT, ci_report._files = str(out), {}
    try:
        for nodeid, phases in {
            "t::ok": ["passed", "passed", "passed"],
            "t::call_and_teardown": ["passed", "failed", "failed"],
            "t::teardown_only": ["passed", "passed", "failed"],
        }.items():
            for when, outcome in zip(("setup", "call", "teardown"), phases, strict=True):
                ci_report.pytest_runtest_logreport(report(nodeid, when, outcome))
        ci_report.pytest_sessionfinish(None, 1)
    finally:
        ci_report.REPORT, ci_report._files = saved
    line = json.loads(out.read_text(encoding="utf-8"))
    assert (line["passed"], line["failed"], line["ok"]) == (1, 2, False)
    assert len(line["failures"]) == 3
