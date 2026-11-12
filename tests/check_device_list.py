"""The table of supported models in README.md agrees with the code: every model
profile (devices/*.yaml) and every model of the test data (fixtures) has a row,
and the level of each row is what the repository can back it with."""
import sys, json, re
from pathlib import Path
_HERE = Path(__file__).resolve().parent
root = Path(sys.argv[1] if len(sys.argv) > 1 else _HERE.parent)

fails = 0
def check(name, got, want=True):
    global fails
    ok = got == want
    fails += not ok
    print(("OK  " if ok else "FAIL"), name, "" if ok else f"\n      got:  {got!r}\n      want: {want!r}")

LEVELS = {"устройство": "device", "данные облака": "data", "профиль": "profile", "сообщение": "report"}
KINDS = {"AC": "Кондиционер", "REF": "Холодильник", "WH": "Водонагреватель", "WM": "Стиральная машина", "DRY": "Сушильная машина"}
FIXTURES = ("evo_dump.json", "ref_dump.json", "laundry_dump.json", "wh_dump.json")

def key(model: str) -> str:
    """What the integration looks a profile up by: no maker's note in brackets, no - / or spaces."""
    model = re.sub(r"\s+\([^)]*\)$", "", model.strip())
    return re.sub(r"[-/\s]", "", model).upper()

def matches(model_key: str, name: str) -> bool:
    return model_key == name or model_key[:11] == name

# ---- the table
readme = (root / "README.md").read_text(encoding="utf-8")
block = re.search(r"<!-- devices:start -->\n(.*?)<!-- devices:end -->", readme, re.S)
check("README has the table of models between its markers", block is not None)
rows = []
for line in (block.group(1).splitlines() if block else []):
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    if len(cells) < 3 or cells[0] in ("Тип", "") or set(cells[0]) <= set("-:"):
        continue
    level = next((v for k, v in LEVELS.items() if k in cells[2]), None)
    rows.append({"kind": cells[0], "model": cells[1], "key": key(cells[1]), "level": level, "line": line.strip()})
check("the table has rows", len(rows) > 10, True)
check("every row has a known type and level", [r["line"] for r in rows if r["kind"] not in KINDS.values() or r["level"] is None], [])
keys = [r["key"] for r in rows]
check("no model twice", sorted({k for k in keys if keys.count(k) > 1}), [])

# ---- what backs it: profiles and test data
profiles = {}
for path in sorted((root / "custom_components" / "haier_evo" / "devices").glob("*.yaml")):
    if path.stem == "default":
        continue
    text = path.read_text(encoding="utf-8")
    names = set(re.findall(r"^\s*-\s*name:\s*(\w+)", text, re.M))
    kind = "WH" if "heating_mode" in names else "WM" if "program" in names else "AC"
    profiles[path.stem] = kind

data = {}
for name in FIXTURES:
    dump = json.loads((_HERE / "fixtures" / name).read_text(encoding="utf-8"))
    page_types = {}
    for container in ((dump.get("backend_data") or {}).get("presentation") or {}).get("layout", {}).get("scrollContainer") or []:
        for match in re.finditer(r"type=(\w+)&deviceId=([0-9a-fA-F%3A:]+)", json.dumps(container)):
            page_types[match.group(2).replace("%3A", ":").lower()] = match.group(1)
    for dev in dump["devices"]:
        bd = dev["backend_data"]
        inner = bd.get("smartDeviceControl") if isinstance(bd.get("smartDeviceControl"), dict) else bd
        model = (inner.get("info") or {}).get("model")
        data[key(model)] = page_types.get(dev["device_id"].lower())

def row_for(name):
    return next((r for r in rows if matches(r["key"], name)), None)

check("every model profile has a row", sorted(p for p in profiles if row_for(p) is None), [])
check("every model of the test data has a row", sorted(m for m in data if row_for(m) is None), [])
check("a model of the test data is at least 'данные облака'",
      sorted(m for m in data if (row_for(m) or {}).get("level") not in ("device", "data")), [])

def backed(row):
    return ({p for p in profiles if matches(row["key"], p)}, {m for m in data if m == row["key"]})
check("'устройство' and 'данные облака' rows have test data",
      [r["model"] for r in rows if r["level"] in ("device", "data") and not backed(r)[1]], [])
check("'профиль' rows have a profile and no test data (else they are 'данные облака')",
      [r["model"] for r in rows if r["level"] == "profile" and (not backed(r)[0] or backed(r)[1])], [])
check("'сообщение' rows have neither (else the level is too low)",
      [r["model"] for r in rows if r["level"] == "report" and any(backed(r))], [])
check("the type of a row is the type of its profile",
      sorted(f"{p}: {row_for(p)['kind']} != {KINDS[k]}" for p, k in profiles.items()
             if row_for(p) and row_for(p)["kind"] != KINDS[k]), [])
check("the type of a row is the type the app's device list gives",
      sorted(f"{m}: {row_for(m)['kind']} != {KINDS.get(t)}" for m, t in data.items()
             if t and row_for(m) and row_for(m)["kind"] != KINDS.get(t)), [])

# ---- how a model gets into the table
form = root / ".github" / "ISSUE_TEMPLATE" / "new_device.yml"
check("the form for a new device exists and README links to it",
      (form.exists(), "issues/new?template=new_device.yml" in readme), (True, True))

print("FAILS:", fails)
sys.exit(1 if fails else 0)
