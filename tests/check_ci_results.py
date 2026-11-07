"""Job summaries, annotations and the README badges of CI (scripts/ci_results.py)."""
import sys, json, os, io, tempfile, importlib.util
from contextlib import redirect_stdout
from pathlib import Path
_HERE = Path(__file__).resolve().parent
root = sys.argv[1] if len(sys.argv) > 1 else str(_HERE.parent)
spec = importlib.util.spec_from_file_location("ci_results", os.path.join(root, "scripts", "ci_results.py"))
cr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cr)

fails = 0
def check(name, got, want=True):
    global fails
    ok = got == want
    fails += not ok
    print(("OK  " if ok else "FAIL"), name, "" if ok else f"\n      got:  {got!r}\n      want: {want!r}")

VERSIONS = {"minimum": "", "prev2": "2026.9.4", "prev1": "2026.10.4", "current": "2026.11.0", "next": "dev"}
HA = {"minimum": "2026.9.4", "prev2": "2026.9.4", "prev1": "2026.10.4", "current": "2026.11.0",
      "next": "2026.12.0.dev202611070240"}
DIGEST = "sha256:" + "ab12" * 16

def lines(channel, passed=(300, 166), failed=(0, 0), outcome="success"):
    """A report as run_checks.sh, tests/test_checks.py and the job step write it."""
    tag = VERSIONS.get(channel) or HA[channel]
    out = [{"image": f"ghcr.io/home-assistant/home-assistant:{tag}", "digest": DIGEST},
           {"homeassistant": HA[channel], "python": "3.14.2", "pytest": "8.4.2", "websocket_client": "1.9.0"}]
    for i, (p, f) in enumerate(zip(passed, failed)):
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
                                 "| pytest | `8.4.2` |", "| websocket-client | `1.9.0` |", "`3f13894` · pull_request · `ci-badges`")), True)
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

# ---- tests/test_checks.py writes what this script reads: a real pytest run over three scripts
if importlib.util.find_spec("pytest") is None:
    print("SKIP the report of tests/test_checks.py: pytest is not installed")
else:
    import shutil, subprocess
    work = Path(tempfile.mkdtemp()) / "tests"
    work.mkdir()
    shutil.copy(os.path.join(root, "tests", "test_checks.py"), work)
    (work / "check_a.py").write_text('print("OK   one")\nprint("OK   two")\nprint("FAILS: 0")\n')
    (work / "check_b.py").write_text('import sys\nprint("OK   one")\nprint("FAIL two")\nprint("      got:  1")\n'
                                     'print("OK   three")\nprint("FAILS: 1")\nsys.exit(1)\n')
    (work / "check_c.py").write_text('print("OK   one")\nraise RuntimeError("broken")\n')
    report_file = work.parent / "report.jsonl"
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(work)],
                          cwd=work.parent, capture_output=True, text=True, timeout=120,
                          env=dict(os.environ, CHECKS_REPORT=str(report_file)))
    got = cr.read_report(report_file)
    by_script = {s["script"]: s for s in got["scripts"]} if got else {}
    check("test_checks.py: OK and FAIL lines counted per script (not FAILS:), a crash is one failed check",
          (proc.returncode, got and {k: got[k] for k in ("passed", "failed", "broken")}),
          (1, {"passed": 5, "failed": 2, "broken": 2}))
    check("test_checks.py: a failure keeps its got / want lines", by_script.get("check_b.py", {}).get("failures"),
          ["FAIL two\n      got:  1"])
    check("test_checks.py: a crash keeps the end of its output",
          "RuntimeError: broken" in "".join(by_script.get("check_c.py", {}).get("failures", [])), True)
    check("test_checks.py: the time of every script and the versions of the run",
          (all(s["seconds"] > 0 for s in got["scripts"]) if got else False, sorted(got["meta"]) if got else None),
          (True, ["homeassistant", "pytest", "python", "websocket_client"]))

print("FAILS:", fails)
sys.exit(1 if fails else 0)
