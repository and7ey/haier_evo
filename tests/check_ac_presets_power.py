"""AC: "Тихий"/"Турбо" follow the unit, "power on" always goes out (1.5.3)."""
import sys, json, os, copy, tempfile
from pathlib import Path
_HERE = Path(__file__).resolve().parent
root = sys.argv[1] if len(sys.argv) > 1 else str(_HERE.parent)
dump_path = sys.argv[2] if len(sys.argv) > 2 else str(_HERE / "fixtures" / "evo_dump.json")
sys.path.insert(0, root)
from custom_components.haier_evo import api, climate, switch
dump = json.load(open(dump_path))

fails = 0
def check(name, got, want=True):
    global fails
    ok = got == want
    fails += not ok
    print(("OK  " if ok else "FAIL"), name, "" if ok else f"\n      got:  {got!r}\n      want: {want!r}")

tmp = tempfile.mkdtemp()
class C:
    def path(self, x): return os.path.join(tmp, x)
class L:
    def call_soon_threadsafe(self, cb): pass
class H: config = C(); loop = L()
class Hr:
    def __init__(s, reject=None):
        s.hass = H(); s.sent = []; s.reject = reject
    def send_command(s, m):
        s.sent.append(copy.deepcopy(m))
        if s.reject and s.reject(m):
            raise api.HaierCommandRejected("rejected")

ac_src = next(d for d in dump["devices"] if not d["device_id"].startswith("5c:01"))
def make(reject=None, **values):
    data = copy.deepcopy(ac_src["backend_data"])
    for attr in data["attributes"]:
        if attr["name"] in values:
            attr["currentValue"] = values[attr["name"]]
    h = Hr(reject)
    return api.HaierAC(haier=h, device_mac=ac_src["device_id"], device_serial="x", device_title="t", backend_data=data), h

def push(ac, **props):
    ac.on_message({"event": "status", "macAddress": ac.device_id,
                   "payload": {"statuses": [{"properties": dict(props)}]}})

def sent(h):
    return [{c["commandName"]: c["value"] for c in m.get("commands") or [m["command"]]} for m in h.sent]

ac, h = make()
QUIET, TURBO, POWER = str(ac.config["quiet"]), str(ac.config["turbo"]), str(ac.config["status"])
MODE = str(ac.config["mode"])
check("fixture: one code each for quiet/sleep and turbo/boost",
      ([a.name for a in ac.config.get_attrs_by_code(QUIET)], [a.name for a in ac.config.get_attrs_by_code(TURBO)]),
      (["preset_mode_sleep", "quiet"], ["preset_mode_boost", "turbo"]))

# 1. the unit's own state reaches the switches and the preset
push(ac, **{QUIET: "1"})
check("echo: quiet on -> switch on, preset sleep", (ac.quiet_on, ac.preset_mode), (True, "sleep"))
push(ac, **{QUIET: "0", TURBO: "1"})
check("echo: turbo on -> switch on, preset boost", (ac.quiet_on, ac.turbo_on, ac.preset_mode), (False, True, "boost"))
push(ac, **{TURBO: "0"})
check("echo: both off -> preset none", (ac.quiet_on, ac.turbo_on, ac.preset_mode), (False, False, "none"))
check("entities read it", (switch.HaierACQuietSwitch(ac).is_on, climate.HaierACEntity(ac).preset_mode), (False, "none"))
push(ac, **{QUIET: "1"})
check("entities follow the echo", (switch.HaierACQuietSwitch(ac).is_on, climate.HaierACEntity(ac).preset_mode), (True, "sleep"))

ac, h = make(**{QUIET: "1"})
check("state read from the cloud at start: quiet on", (ac.quiet_on, ac.preset_mode), (True, "sleep"))
ac, h = make(**{TURBO: "1"})
check("state read from the cloud at start: turbo on", (ac.turbo_on, ac.preset_mode), (True, "boost"))

# 2. HA's own preset shows until the echo
ac, h = make(**{POWER: "1", MODE: "1"})
ac.set_preset_mode("boost")
check("set boost: sent, shown", (sent(h)[-1].get(TURBO), ac.turbo_on, ac.quiet_on, ac.preset_mode), ("1", True, False, "boost"))
ac.set_preset_mode("sleep")
check("set sleep: shown", (ac.turbo_on, ac.quiet_on, ac.preset_mode), (False, True, "sleep"))
ac.set_preset_mode("none")
check("set none: shown", (ac.turbo_on, ac.quiet_on, ac.preset_mode), (False, False, "none"))
ac.set_preset_mode("boost")
push(ac, **{TURBO: "0"})
check("the unit's answer wins over what was asked", (ac.turbo_on, ac.preset_mode), (False, "none"))

# 3. power on always goes out
ac, h = make(**{POWER: "0"})
ac.switch_on("cool")
off_cmd = sent(h)[0]
check("unit off: power on with the cloud's power-on extras", (off_cmd.get(POWER), off_cmd.get("6"), off_cmd.get("31")), ("1", "0", "0"))

ac, h = make(**{POWER: "1", MODE: "1"})
ac.switch_on("heat")
on_cmd = sent(h)[0]
check("unit believed on: power on goes out too", on_cmd.get(POWER), "1")
check("... with the new mode", on_cmd.get(MODE), ac.config.get_attr_by_name("mode").get_item_code("heat"))
check("... without the extras that stop the cleaning cycles", ("6" in on_cmd, "31" in on_cmd), (False, False))
check("... in one command", len(h.sent), 1)

ac, h = make(**{POWER: "1", MODE: "1"})
ac.switch_on()
check("turn on of a unit believed on: power on and its mode", (sent(h)[0].get(POWER), sent(h)[0].get(MODE)), ("1", "1"))

# the cloud refuses "power on" for a running unit together with the group
ac, h = make(reject=lambda m: any(str(c["commandName"]) == POWER for c in m.get("commands") or []), **{POWER: "1", MODE: "1"})
err = None
try:
    ac.switch_on("heat")
except Exception as e:
    err = e
check("refused group: no error, the mode goes out alone",
      (err, len(h.sent), POWER in sent(h)[1], sent(h)[1].get(MODE)), (None, 2, False, ac.config.get_attr_by_name("mode").get_item_code("heat")))
check("refused group: the new mode is shown", (ac.status, ac.mode), (1, "heat"))

ac, h = make(reject=lambda m: True, **{POWER: "0"})
try:
    ac.switch_on("cool"); err = None
except Exception as e:
    err = e
check("unit off, power on refused: the error reaches HA, no second command",
      (isinstance(err, api.HaierCommandRejected), len(h.sent)), (True, 1))

print("FAILS:", fails)
sys.exit(1 if fails else 0)
