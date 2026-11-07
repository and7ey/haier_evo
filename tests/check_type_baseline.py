"""The type check of CI fails only on a new mypy error (scripts/check_types.py)."""
import sys, os, tempfile, importlib.util
from pathlib import Path
_HERE = Path(__file__).resolve().parent
root = sys.argv[1] if len(sys.argv) > 1 else str(_HERE.parent)
spec = importlib.util.spec_from_file_location("check_types", os.path.join(root, "scripts", "check_types.py"))
ct = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ct)

fails = 0
def check(name, got, want=True):
    global fails
    ok = got == want
    fails += not ok
    print(("OK  " if ok else "FAIL"), name, "" if ok else f"\n      got:  {got!r}\n      want: {want!r}")

OUTPUT = """custom_components/haier_evo/api.py:517: error: Missing return statement  [return]
custom_components/haier_evo/api.py:875: error: Item "None" of "WebSocketApp | None" has no attribute "run_forever"  [union-attr]
custom_components/haier_evo/sensor.py:56: error: No overload variant of "getattr" matches  [call-overload]
custom_components/haier_evo/sensor.py:98: error: No overload variant of "getattr" matches  [call-overload]
custom_components/haier_evo/sensor.py:98: note: Possible overload variants:
custom_components/haier_evo/climate.py:3: error: Something without a code
Success: no issues found in 1 source file
"""
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

print("FAILS:", fails)
sys.exit(1 if fails else 0)
