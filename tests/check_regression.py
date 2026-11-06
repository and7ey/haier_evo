"""Snapshot of what the ACs send and show for the fixture: commands for a typical
sequence, the payload for every louver position, the resulting state.

UPDATE_SNAPSHOT=1 rewrites fixtures/regression.json (only after checking the
difference is intended).
"""
import sys, json, os, copy, tempfile
from pathlib import Path
_HERE = Path(__file__).resolve().parent
root = sys.argv[1] if len(sys.argv) > 1 else str(_HERE.parent)
dump_path = sys.argv[2] if len(sys.argv) > 2 else str(_HERE / "fixtures" / "evo_dump.json")
snapshot_path = _HERE / "fixtures" / "regression.json"
sys.path.insert(0, root)
from custom_components.haier_evo import api
dump = json.load(open(dump_path))

tmp = tempfile.mkdtemp()
class C:
    def path(self, x): return os.path.join(tmp, x)
class L:
    def call_soon_threadsafe(self, cb): pass
class H: config = C(); loop = L()
class Hr:
    def __init__(s): s.hass = H(); s.sent = []
    def send_message(s, p): s.sent.append(json.loads(p))
    def send_command(s, m): s.sent.append(dict(m))

def make(dev):
    h = Hr()
    return api.HaierAC(haier=h, device_mac=dev["device_id"], device_serial="x", device_title="t",
                       backend_data=copy.deepcopy(dev["backend_data"])), h

def state(ac):
    d = ac.to_dict(); d.pop("backend_data")
    for k in ("alarms", "connected", "deferred_target", "locked"):
        d.pop(k, None)
    return d

out = {}
for dev in dump["devices"]:
    if dev["device_id"].startswith("5c:01"):
        continue
    # a typical sequence
    ac, h = make(dev)
    ac.switch_on("cool"); ac.set_temperature(23); ac.set_fan_mode("low"); ac.set_preset_mode("boost"); ac.switch_off()
    for m in h.sent:
        m.pop("trace", None)
    seq = {"state": state(ac), "sent": h.sent, "modes": [ac.get_hvac_modes(), ac.get_fan_modes(), ac.get_preset_modes()]}
    # every louver position, by code
    ac, h = make(dev)
    ac.switch_on("cool")
    by_code = {}
    for attr, setter in (("swing_mode", ac.set_swing_mode), ("swing_horizontal_mode", ac.set_swing_horizontal_mode)):
        for item in ac.config.get_attr_by_name(attr).list:
            h.sent.clear()
            try:
                setter(item.name)
                for m in h.sent:
                    m.pop("trace", None)
                by_code[f"{attr}:{item.value}"] = [item.name, h.sent[:]]
            except Exception as e:
                by_code[f"{attr}:{item.value}"] = [item.name, type(e).__name__]
    out[dev["device_id"]] = {"sequence": seq, "louvers": by_code}

got = json.loads(json.dumps(out, ensure_ascii=False, sort_keys=True, default=str))
if os.environ.get("UPDATE_SNAPSHOT") == "1":
    snapshot_path.write_text(json.dumps(got, ensure_ascii=False, sort_keys=True, indent=1) + "\n", encoding="utf-8")
    print("snapshot written")
want = json.loads(snapshot_path.read_text(encoding="utf-8"))

fails = 0
def diff(a, b, path=""):
    global fails
    if type(a) != type(b) or (not isinstance(a, (dict, list)) and a != b):
        fails += 1
        print(f"FAIL {path}: got {str(a)[:200]!r}, want {str(b)[:200]!r}")
        return
    if isinstance(a, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a or k not in b:
                fails += 1
                print(f"FAIL {path}/{k}: {'missing' if k not in a else 'unexpected'}")
            else:
                diff(a[k], b[k], f"{path}/{k}")
    elif isinstance(a, list):
        if len(a) != len(b):
            fails += 1
            print(f"FAIL {path}: {len(a)} items, want {len(b)}")
        for i, (x, y) in enumerate(zip(a, b)):
            diff(x, y, f"{path}[{i}]")

diff(got, want)
if not fails:
    print(f"OK   snapshot matches ({len(got)} ACs)")
print("FAILS:", fails)
sys.exit(1 if fails else 0)
