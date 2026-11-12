"""The CI scripts: job summaries, annotations and badges (scripts/ci_results.py),
the versions CI runs against (scripts/ha_versions.py) and the type check
against its baseline (scripts/check_types.py).

Ported from the check scripts of 1.7.2 (tests/check_ci_results.py,
check_ha_versions.py, check_type_baseline.py): every check() of a script is
kept, the test fails with the list of the checks that did not hold.
"""
# ruff: noqa: E501, E731, PLR2004, SIM115
from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
root = str(ROOT)


def _script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cr = _script("ci_results")
hv = _script("ha_versions")
ct = _script("check_types")


class Checks(list):
    """check(name, got, want) of the scripts: the failed ones are collected."""

    def __call__(self, name, got, want=True):
        if got != want:
            self.append(f"{name}\n      got:  {got!r}\n      want: {want!r}")

    def verify(self):
        assert not self, "\n".join(self)


def test_ci_results():
    check = Checks()
    VERSIONS = {"minimum": "", "prev2": "2026.9.4", "prev1": "2026.10.4", "current": "2026.11.0", "next": "dev"}
    HA = {"minimum": "2026.9.4", "prev2": "2026.9.4", "prev1": "2026.10.4", "current": "2026.11.0",
          "next": "2026.12.0.dev202611070240"}
    DIGEST = "sha256:" + "ab12" * 16

    def lines(channel, passed=(300, 166), failed=(0, 0), outcome="success"):
        """A report as run_checks.sh, tests/ci_report.py and the job step write it."""
        tag = VERSIONS.get(channel) or HA[channel]
        out = [{"image": f"ghcr.io/home-assistant/home-assistant:{tag}", "digest": DIGEST},
               {"homeassistant": HA[channel], "python": "3.14.2", "pytest": "8.4.2", "plugin": "0.13.375"}]
        for i, (p, f) in enumerate(zip(passed, failed, strict=True)):
            out.append({"script": f"check_{i}.py", "passed": p, "failed": f, "ok": not f, "seconds": 10.0 + i,
                        "failures": [f"FAIL case {n}\n      got:  1\n      want: 2" for n in range(f)]})
        if outcome is not None:
            out.append({"outcome": outcome if not any(failed) else "failure"})
        return out

    def write(folder, reports):
        for channel, records in reports.items():
            (folder / f"{channel}.jsonl").write_text("".join(
                (r if isinstance(r, str) else json.dumps(r)) + "\n" for r in records))

    def run(reports, versions=VERSIONS, minimum="2026.9.0"):
        tmp = Path(tempfile.mkdtemp())
        write(tmp, reports)
        entries = cr.results(versions, tmp)
        return entries, cr.badges(entries, minimum)

    def shown(files):
        """{file: (message, color)} of the Home Assistant badges."""
        return {name: (b["message"], b["color"]) for name, b in files.items() if name.startswith("ha-")}

    ALL = {c: lines(c) for c in ("prev2", "prev1", "current", "next")}
    GREEN = {"ha-minimum.json": ("≥ 2026.9.0", "brightgreen"), "ha-prev2.json": ("2026.9.4", "brightgreen"),
             "ha-prev1.json": ("2026.10.4", "brightgreen"), "ha-current.json": ("2026.11.0", "brightgreen"),
             "ha-next.json": ("2026.12-dev", "brightgreen")}

    # ---- badges
    entries, files = run(ALL)
    tests = files["tests.json"]
    check("every version ran: one entry per version, in the order of the jobs",
          [(e["channel"], e["status"]) for e in entries],
          [("prev2", "passed"), ("prev1", "passed"), ("current", "passed"), ("next", "passed")])
    check("the files of the badges branch: tests, the minimum, one per version, in the order of the README",
          list(files), ["tests.json", "ha-minimum.json", "ha-prev2.json", "ha-prev1.json", "ha-current.json", "ha-next.json"])
    check("tests badge: checks passed on the current version", (tests["label"], tests["message"], tests["color"]),
          ("tests", "466 passed", "brightgreen"))
    check("a badge per version, green; the nightly build named by its release; the minimum of hacs.json",
          shown(files), GREEN)
    check("Home Assistant badges: label and logo",
          {(b["label"], b["namedLogo"]) for n, b in files.items() if n.startswith("ha-")}, {("Home Assistant", "homeassistant")})
    check("shields.io endpoint format", all(b["schemaVersion"] == 1 and isinstance(b["message"], str) and b["message"]
                                            for b in files.values()), True)
    check("the counts differ between versions: the current one is shown",
          run(dict(ALL, prev2=lines("prev2", passed=(1, 166))))[1]["tests.json"]["message"], "466 passed")

    _, files = run(dict(ALL, next=lines("next", passed=(298, 166), failed=(2, 0))))
    check("only next failed: tests stay green, next gets a cross and is yellow, the others green",
          (files["tests.json"]["message"], files["tests.json"]["color"], shown(files)),
          ("466 passed", "brightgreen", dict(GREEN, **{"ha-next.json": ("2026.12-dev ✗", "yellow")})))
    _, files = run(dict(ALL, prev1=lines("prev1", passed=(297, 166), failed=(3, 0)),
                        current=lines("current", passed=(299, 166), failed=(1, 0))))
    check("blocking versions failed: the worst one on tests, each failed version red with a cross",
          (files["tests.json"]["message"], files["tests.json"]["color"], shown(files)),
          ("463 passed, 3 failed", "red", dict(GREEN, **{"ha-prev1.json": ("2026.10.4 ✗", "red"),
                                                          "ha-current.json": ("2026.11.0 ✗", "red")})))

    # ---- the minimum of hacs.json
    _, files = run(dict(ALL, prev2=lines("prev2", failed=(1, 0))))
    check("minimum inside the window: coloured by the job of its release",
          (shown(files)["ha-minimum.json"], shown(files)["ha-prev2.json"]), (("≥ 2026.9.0 ✗", "red"), ("2026.9.4 ✗", "red")))
    check("minimum inside the window on a later release of it",
          shown(run(dict(ALL, prev1=lines("prev1", failed=(1, 0))), minimum="2026.10.0")[1])["ha-minimum.json"],
          ("≥ 2026.10.0 ✗", "red"))
    outside = dict(VERSIONS, minimum="2026.9.4", prev2="2026.10.4", prev1="2026.11.0", current="2026.12.0", next="2027.1.0b1")
    entries, files = run({c: lines(c) for c in ("prev2", "prev1", "current", "next")}
                         | {"minimum": lines("minimum", failed=(1, 0))}, outside)
    check("the window left the minimum behind: its own job colours it, no extra version badge, a beta by its tag",
          (entries[0]["channel"], list(files), shown(files)["ha-minimum.json"], shown(files)["ha-next.json"][0]),
          ("minimum", ["tests.json", "ha-minimum.json", "ha-prev2.json", "ha-prev1.json", "ha-current.json", "ha-next.json"],
           ("≥ 2026.9.0 ✗", "red"), "2027.1.0b1"))
    check("a minimum no job checks: blue, just the requirement",
          shown(run(ALL, minimum="2025.1.0")[1])["ha-minimum.json"], ("≥ 2025.1.0", "blue"))
    check("no minimum in hacs.json: no minimum badge", "ha-minimum.json" in run(ALL, minimum=None)[1], False)
    hacs_dir = Path(tempfile.mkdtemp())
    for name, text in (("ok", '{"homeassistant": " 2026.9.0 "}'), ("none", '{"name": "x"}'), ("beta", '{"homeassistant": "2026.9.0b1"}'),
                       ("text", '{"homeassistant": "latest"}'), ("list", '[]'), ("broken", '{')):
        (hacs_dir / name).write_text(text)
    check("hacs.json: the minimum as written, nothing for no key, a beta, text, garbage or no file",
          [cr.hacs_minimum(str(hacs_dir / n)) for n in ("ok", "none", "beta", "text", "list", "broken", "missing")],
          ["2026.9.0", None, None, None, None, None, None])

    # ---- statuses
    entries, files = run({k: v for k, v in ALL.items() if k != "prev2"})
    check("no report (the job broke before the checks): an error, not a pass",
          (entries[0]["status"], files["tests.json"]["message"], files["tests.json"]["color"], shown(files)["ha-prev2.json"]),
          ("error", "error", "red", ("2026.9.4 ✗", "red")))
    check("only the outcome line (docker pull or pip failed): an error",
          run(dict(ALL, current=[{"outcome": "failure"}]))[0][2]["status"], "error")
    check("a report without scripts (pytest broke before the checks) is an error",
          run(dict(ALL, current=lines("current", passed=(), failed=())))[0][2]["status"], "error")
    check("a script that broke without a FAIL line counts as failed",
          run(dict(ALL, current=lines("current") + [{"script": "check_x.py", "passed": 0, "failed": 1, "ok": False}]))[1]["tests.json"]["message"],
          "466 passed, 1 failed")
    check("a broken line in a report is a failure, not ignored",
          run(dict(ALL, current=lines("current") + ['{"script": "check_x.py", "pass']))[0][2]["status"], "failed")
    check("a failed checks step with a clean report is an error",
          run(dict(ALL, prev1=lines("prev1", outcome="failure")))[0][1]["status"], "error")
    check("a report from a run by hand (no outcome) is judged by its checks",
          run(dict(ALL, prev1=lines("prev1", outcome=None)))[0][1]["status"], "passed")
    check("the nightly build without a version in its report stays dev",
          cr.version_name({"tag": "dev", "homeassistant": None}), "dev")
    _, files = run({}, versions={}, minimum=None)
    check("no versions at all: a grey tests badge, not a crash", (list(files), files["tests.json"]["color"]), (["tests.json"], "lightgrey"))

    # ---- the job of one version: summary and annotations
    def job(channel, records, env=None):
        tmp = Path(tempfile.mkdtemp())
        report = tmp / "checks" / f"{channel}.jsonl"
        if records is not None:
            report.parent.mkdir()
            report.write_text("".join(json.dumps(r) + "\n" for r in records))
        summary_file = tmp / "summary.md"
        out = io.StringIO()
        saved = {k: os.environ.get(k) for k in ("GITHUB_SHA", "GITHUB_EVENT_NAME", "GITHUB_HEAD_REF", "GITHUB_REF_NAME")}
        os.environ.update(env or {})
        try:
            with redirect_stdout(out):
                code = cr.main(["job", "--report", str(report), "--channel", channel, "--tag", VERSIONS[channel] or "2026.9.4",
                                "--outcome", "failure" if any(r.get("failed") for r in records or ()) or records is None else "success",
                                "--summary-file", str(summary_file)])
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        return code, summary_file.read_text(), out.getvalue().splitlines(), report.read_text()

    env = {"GITHUB_SHA": "3f13894aaaa", "GITHUB_EVENT_NAME": "pull_request", "GITHUB_HEAD_REF": "ci-badges"}
    code, summary, notes, report = job("prev2", lines("prev2", outcome=None), env)
    check("job: the outcome of the checks step goes into the report", json.loads(report.splitlines()[-1]), {"outcome": "success"})
    check("job summary: the verdict with the counts",
          summary.splitlines()[0], "### ✅ пройдено: Home Assistant 2026.9.4 (previous but one) — 466 из 466 проверок")
    check("job summary: the image, its digest, the versions inside it and the commit",
          all(t in summary for t in ("`ghcr.io/home-assistant/home-assistant:2026.9.4`", DIGEST, "| Python | `3.14.2` |",
                                     "| pytest | `8.4.2` |", "| pytest-homeassistant-custom-component | `0.13.375` |", "`3f13894` · pull_request · `ci-badges`")), True)
    check("job summary: a row per script with its counts and time", "| ✅ `check_1.py` | 166 | 0 | 11.0 |" in summary, True)
    check("a passed version: one notice with the count, Python and time",
          notes, ["::notice title=Home Assistant 2026.9.4::466 проверок пройдено, Python 3.14.2, 21 с"])

    code, summary, notes, _ = job("current", lines("current", passed=(290, 166), failed=(12, 0)))
    check("job summary: what failed, with got / want", ("#### Что не прошло" in summary, "      want: 2" in summary), (True, True))
    check("failures: errors on the script file, at most 10 per step, the rest counted",
          (len(notes), notes[0].split("::")[1], notes[-1]),
          (10, "error file=tests/check_0.py,title=Home Assistant 2026.11.0%3A check_0.py",
           "::error title=Home Assistant 2026.11.0::ещё 3 не пройдено, все — в сводке задачи"))
    check("annotation text keeps its lines, escaped", "FAIL case 0%0A      got:  1%0A      want: 2" in notes[0], True)
    code, summary, notes, _ = job("next", lines("next", passed=(299, 166), failed=(1, 0)))
    check("a version that does not block: warnings, not errors, and it says so",
          (notes[0].startswith("::warning "), "⚠️ не пройдено (не блокирует)" in summary), (True, True))
    code, summary, notes, report = job("prev1", None)
    check("no report at all (docker pull failed): an error that points to the log",
          (notes, "не дошли до конца" in summary, json.loads(report)), (["::error title=Home Assistant 2026.10.4::Проверки не дошли до конца: причина в логе шага Checks"], True, {"outcome": "failure"}))
    check("property values escape : and ,", cr.command("error", "a", "x: y, z", "f"), "::error file=f,title=x%3A y%2C z::a")

    # ---- the results job
    tmp = Path(tempfile.mkdtemp())
    write(tmp, ALL)
    def cli(*argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = cr.main(["run", "--reports", str(tmp), "--versions", json.dumps(VERSIONS),
                            "--hacs", os.path.join(root, "hacs.json"), *argv])
        return code, out.getvalue()
    code, out = cli()
    got = dict(line.split("=", 1) for line in out.splitlines())
    files = json.loads(got["badges"])
    check("$GITHUB_OUTPUT: one line, the files of the badges branch; the minimum from hacs.json",
          (code, list(got), list(files), files["tests.json"]["message"], shown(files)),
          (0, ["badges"], ["tests.json", *GREEN], "466 passed", GREEN))
    check("badge file names are plain", all(cr.re.fullmatch(r"[a-z0-9-]+\.json", n) for n in files), True)
    code, table = cli("--summary")
    check("run summary: a row per version with Python, the image digest, counts and time",
          ("| текущая | `2026.11.0` | 3.14.2 | `sha256:ab12ab12ab12` | 466 | 0 | 21 | ✅ пройдено |" in table,
           "(2026.12.0.dev202611070240)" in table), (True, True))
    check("verdict: all blocking versions passed", cli("--verdict")[0], 0)
    write(tmp, {"next": lines("next", failed=(1, 0))})
    check("verdict: next alone may fail", cli("--verdict")[0], 0)
    write(tmp, {"prev1": lines("prev1", failed=(1, 0))})
    check("verdict: a blocking version failed", cli("--verdict")[0], 1)

    # ---- tests/ci_report.py writes what this script reads: a real pytest run over three files
    work = Path(tempfile.mkdtemp()) / "tests"
    work.mkdir()
    (work / "ci_report.py").write_text((ROOT / "tests" / "ci_report.py").read_text(encoding="utf-8"), encoding="utf-8")
    (work / "conftest.py").write_text(
        "from ci_report import pytest_runtest_logreport, pytest_sessionfinish, pytest_sessionstart  # noqa: F401\n"
    )
    (work / "test_a.py").write_text("def test_one():\n    pass\n\n\ndef test_two():\n    pass\n")
    (work / "test_b.py").write_text("def test_one():\n    pass\n\n\ndef test_two():\n    assert 1 == 2, 'got 1'\n")
    (work / "test_c.py").write_text(
        "import pytest\n\n\n@pytest.fixture\ndef broken():\n    yield\n    raise RuntimeError('broken')\n\n\n"
        "def test_call_and_teardown(broken):\n    raise RuntimeError('call')\n"
    )
    report_file = work.parent / "report.jsonl"
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--rootdir", str(work),
                           "--import-mode=importlib", str(work)],
                          cwd=work, capture_output=True, text=True, timeout=120,
                          # plain pytest: the Home Assistant harness wants its own fixtures
                          env=dict(os.environ, CHECKS_REPORT=str(report_file), PYTHONPATH=str(work),
                                   PYTEST_DISABLE_PLUGIN_AUTOLOAD="1"))
    got = cr.read_report(report_file)
    by_script = {s["script"]: s for s in got["scripts"]} if got else {}
    check("ci_report.py: passed and failed tests counted per file, a test failing twice counts once",
          (proc.returncode, got and {k: got[k] for k in ("passed", "failed", "broken")}),
          (1, {"passed": 3, "failed": 2, "broken": 2}))
    check("ci_report.py: a failure keeps its node and assertion",
          any("test_b.py::test_two (call)" in f and "got 1" in f for f in by_script.get("test_b.py", {}).get("failures", [])), True)
    check("ci_report.py: a crash in teardown keeps its message",
          "RuntimeError: broken" in "".join(by_script.get("test_c.py", {}).get("failures", [])), True)
    check("ci_report.py: the time of every file and the versions of the run",
          # trivial tests round to 0.0 s: the time is there, not above zero
          (all(isinstance(s["seconds"], float) for s in got["scripts"]) if got else False,
           sorted(got["meta"]) if got else None),
          (True, ["homeassistant", "plugin", "pytest", "python"]))
    check.verify()


def test_ha_versions():
    check = Checks()
    def pypi(*versions, yanked=()):
        return {"releases": {v: [{"yanked": v in yanked}] for v in versions}}

    def raises(fn):
        try:
            fn()
        except SystemExit:
            return True
        return False

    TODAY = ["2026.8.0", "2026.8.3", "2026.9.0b0", "2026.9.0", "2026.9.4", "2026.10.0b7", "2026.10.0", "2026.10.4",
             "2026.11.0b6", "2026.11.0"]
    w = hv.window(pypi(*TODAY))
    check("November: two previous, the current one, next as the nightly build",
          [w[k] for k in ("prev2", "prev1", "current", "next")], ["2026.9.4", "2026.10.4", "2026.11.0", "dev"])
    w = hv.window(pypi(*TODAY, "2026.12.0b0", "2026.12.0b1"))
    check("beta week: next is the newest beta of the next release", w["next"], "2026.12.0b1")
    w = hv.window(pypi(*TODAY, "2026.11.1", "2026.12.0b3", "2026.12.0"))
    check("release day: the window moves by a month",
          [w[k] for k in ("prev2", "prev1", "current", "next")], ["2026.10.4", "2026.11.1", "2026.12.0", "dev"])
    w = hv.window(pypi(*TODAY, "2026.12.0", "2026.12.2", "2027.1.0b0", "2027.1.0b2"))
    check("December -> January", (w["current"], w["next"]), ("2026.12.2", "2027.1.0b2"))
    w = hv.window(pypi(*TODAY, "2026.11.1", "2027.1.0b0"))
    check("a beta two releases ahead is not the next one", w["next"], "dev")
    w = hv.window(pypi(*TODAY, "2026.10.5", yanked=("2026.10.5",)))
    check("a yanked patch is skipped", w["prev1"], "2026.10.4")
    w = hv.window(pypi(*TODAY, "2026.12.0.dev0", "2026.11.1rc1", "2026.11.x"))
    check("dev builds, release candidates and odd names are ignored", (w["current"], w["next"]), ("2026.11.0", "dev"))
    check("fewer than three releases: an error, not a short window", raises(lambda: hv.window(pypi("2026.10.0", "2026.11.0"))), True)

    # the minimum of hacs.json
    check("minimum inside the window: no extra step", hv.window(pypi(*TODAY), "2026.9.0")["minimum"], "")
    w = hv.window(pypi(*TODAY, "2026.12.0"), "2026.9.0")
    check("the window left the minimum behind: it is checked on its last patch", (w["prev2"], w["minimum"]), ("2026.10.4", "2026.9.4"))
    check("a minimum PyPI does not list is used as written", hv.window(pypi(*TODAY), "2025.1.0")["minimum"], "2025.1.0")
    check("a beta or garbage as the minimum is an error", (raises(lambda: hv.window(pypi(*TODAY), "2026.9.0b1")),
                                                         raises(lambda: hv.window(pypi(*TODAY), "latest"))), (True, True))

    # what the workflow reads
    tmp = tempfile.mkdtemp()
    pypi_file, hacs_file = os.path.join(tmp, "pypi.json"), os.path.join(tmp, "hacs.json")
    json.dump(pypi(*TODAY, "2026.12.0"), open(pypi_file, "w"))
    json.dump({"name": "Haier Evo", "homeassistant": "2026.9.0"}, open(hacs_file, "w"))
    out = io.StringIO()
    with redirect_stdout(out):
        hv.main(["--pypi", pypi_file, "--hacs", hacs_file])
    got = out.getvalue().splitlines()
    check("$GITHUB_OUTPUT lines, every key present",
          got[:5], ["minimum=2026.9.4", "prev2=2026.10.4", "prev1=2026.11.0", "current=2026.12.0", "next=dev"])
    check("$GITHUB_OUTPUT: the matrix, one job per version, next does not block",
          [(j["channel"], j["tag"], j["blocking"]) for j in json.loads(got[5].split("=", 1)[1])] if got[5:] and got[5].startswith("matrix=") else got[5:],
          [("minimum", "2026.9.4", True), ("prev2", "2026.10.4", True), ("prev1", "2026.11.0", True),
           ("current", "2026.12.0", True), ("next", "dev", False)])
    check("the matrix has no job for a minimum inside the window",
          [j["channel"] for j in hv.matrix(hv.window(pypi(*TODAY), "2026.9.0"))], ["prev2", "prev1", "current", "next"])
    check("job names as the steps were named", hv.matrix(hv.window(pypi(*TODAY)))[0]["name"], "previous but one")
    out = io.StringIO()
    with redirect_stdout(out):
        hv.main(["--pypi", pypi_file, "--hacs", hacs_file, "--summary"])
    check("summary table names every step", all(v in out.getvalue() for v in ("2026.9.4", "2026.10.4", "2026.11.0", "2026.12.0", "dev")), True)
    check("every value is a safe container tag", all(hv.re.fullmatch(r"[A-Za-z0-9._-]*", v) for v in hv.window(pypi(*TODAY, "2026.12.0b1"), "2026.9.0").values()), True)

    hacs = json.load(open(os.path.join(root, "hacs.json"), encoding="utf-8"))
    check("hacs.json: the minimum Home Assistant is 2026.9.0, the oldest version CI checks", hacs.get("homeassistant"), "2026.9.0")
    check.verify()


OUTPUT = """custom_components/haier_evo/api.py:517: error: Missing return statement  [return]
custom_components/haier_evo/api.py:875: error: Item "None" of "WebSocketApp | None" has no attribute "run_forever"  [union-attr]
custom_components/haier_evo/sensor.py:56: error: No overload variant of "getattr" matches  [call-overload]
custom_components/haier_evo/sensor.py:98: error: No overload variant of "getattr" matches  [call-overload]
custom_components/haier_evo/sensor.py:98: note: Possible overload variants:
custom_components/haier_evo/climate.py:3: error: Something without a code
Success: no issues found in 1 source file
"""


def test_type_baseline():
    check = Checks()
    errors = ct.parse(OUTPUT)
    check("errors are read, notes and the summary are not", len(errors), 5)
    check("an error is keyed by file, code and message, not by line",
          errors[0]["key"], "custom_components/haier_evo/api.py: [return] Missing return statement")
    check("an error without a code counts as misc", errors[-1]["code"], "misc")
    baseline = [e["key"] for e in errors]

    moved = ct.parse(OUTPUT.replace(":517:", ":530:").replace(":875:", ":12:"))
    check("moved code: nothing new", ct.compare(moved, baseline), ([], []))
    new, gone = ct.compare(ct.parse(OUTPUT + "custom_components/haier_evo/api.py:900: error: Name \"x\" is not defined  [name-defined]\n"), baseline)
    check("a new error is found, with its line", [(e["code"], e["line"]) for e in new], [("name-defined", "900")])
    new, _ = ct.compare(ct.parse(OUTPUT + "custom_components/haier_evo/sensor.py:120: error: No overload variant of \"getattr\" matches  [call-overload]\n"), baseline)
    check("the same error once more is new: they are counted", [e["line"] for e in new], ["120"])
    new, gone = ct.compare(ct.parse(OUTPUT.replace("[return]", "[return-value]").split("\n", 1)[1]), baseline)
    check("a fixed error leaves the baseline, nothing new", (new, gone), ([], ["custom_components/haier_evo/api.py: [return] Missing return statement"]))

    lines = ct.report(ct.parse("custom_components/haier_evo/api.py:7: error: a: b, c%  [misc]\n"), ["x"], 3, github=True)
    check("on GitHub: an error annotation on the line, escaped",
          lines[0], "::error file=custom_components/haier_evo/api.py,line=7,title=mypy [misc]::a: b, c%25")
    check("on GitHub: a notice when the baseline can shrink", lines[1].startswith("::notice title=mypy::1 type errors of the baseline are gone"), True)
    check("the counts", lines[-1], "mypy: 3 errors, 2 of them known, 1 new")
    check("by hand: plain text with file and line",
          ct.report(ct.parse("custom_components/haier_evo/a.py:7: error: m  [misc]\n"), [], 1, github=False)[0],
          "custom_components/haier_evo/a.py:7: new type error: m  [misc]")

    tmp = Path(tempfile.mkdtemp()) / "baseline.txt"
    tmp.write_text("# comment\n\nline one\nline one\n", encoding="utf-8")
    check("baseline: comments and empty lines skipped, repeats kept", ct.read_baseline(tmp), ["line one", "line one"])
    check("no baseline file: empty", ct.read_baseline(tmp.parent / "missing.txt"), [])
    real = ct.read_baseline()
    check("the baseline of the repository is sorted keys of the integration",
          (real == sorted(real), all(line.startswith("custom_components/haier_evo/") and ": [" in line for line in real)), (True, True))
    check.verify()


def test_harness_is_pinned_for_the_releases_ci_blocks_on(tmp_path):
    """R15 of #106: the harness of a listed release comes from scripts/harness_pins.txt, not PyPI."""
    it = _script("install_test_env")
    assert it.pinned_harness("2026.9.4") == ("0.13.367", "2026.9.4")
    assert it.pinned_harness("2026.11.1") == ("0.13.375", "2026.10.4")
    assert it.pinned_harness("2099.1.0") is None
    pins = tmp_path / "pins.txt"
    pins.write_text("# comment\n2026.9.4  0.1  2026.9.4  # why\nbroken line\n", encoding="utf-8")
    assert it.pinned_harness("2026.9.4", pins) == ("0.1", "2026.9.4")
    # every release a pin names is a version the script understands
    for line in it.PINS.read_text(encoding="utf-8").splitlines():
        fields = line.split("#", 1)[0].split()
        if fields:
            assert len(fields) == 3 and it.version_key(fields[0]) and it.version_key(fields[2]), line
