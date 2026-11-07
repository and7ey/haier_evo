"""The Home Assistant versions CI runs against move by themselves (scripts/ha_versions.py)."""
import sys, json, os, io, tempfile, importlib.util
from contextlib import redirect_stdout
from pathlib import Path
_HERE = Path(__file__).resolve().parent
root = sys.argv[1] if len(sys.argv) > 1 else str(_HERE.parent)
spec = importlib.util.spec_from_file_location("ha_versions", os.path.join(root, "scripts", "ha_versions.py"))
hv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hv)

fails = 0
def check(name, got, want=True):
    global fails
    ok = got == want
    fails += not ok
    print(("OK  " if ok else "FAIL"), name, "" if ok else f"\n      got:  {got!r}\n      want: {want!r}")

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
check("$GITHUB_OUTPUT lines, every key present",
      out.getvalue().splitlines(), ["minimum=2026.9.4", "prev2=2026.10.4", "prev1=2026.11.0", "current=2026.12.0", "next=dev"])
out = io.StringIO()
with redirect_stdout(out):
    hv.main(["--pypi", pypi_file, "--hacs", hacs_file, "--summary"])
check("summary table names every step", all(v in out.getvalue() for v in ("2026.9.4", "2026.10.4", "2026.11.0", "2026.12.0", "dev")), True)
check("every value is a safe container tag", all(hv.re.fullmatch(r"[A-Za-z0-9._-]*", v) for v in hv.window(pypi(*TODAY, "2026.12.0b1"), "2026.9.0").values()), True)

hacs = json.load(open(os.path.join(root, "hacs.json"), encoding="utf-8"))
check("hacs.json: the minimum Home Assistant is 2026.9.0, the oldest version CI checks", hacs.get("homeassistant"), "2026.9.0")

print("FAILS:", fails)
sys.exit(1 if fails else 0)
