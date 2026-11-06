"""A value the description does not list is unknown (None), never the string "None" (1.5.1)."""
import sys, json, os, copy, tempfile
from pathlib import Path
_HERE = Path(__file__).resolve().parent
root = sys.argv[1] if len(sys.argv) > 1 else str(_HERE.parent)
dump_path = sys.argv[2] if len(sys.argv) > 2 else str(_HERE / "fixtures" / "evo_dump.json")
sys.path.insert(0, root)
from custom_components.haier_evo import api, climate, select
from homeassistant.components.climate.const import HVACMode
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
    def __init__(s): s.hass = H(); s.sent = []
    def send_command(s, m): s.sent.append(dict(m))

ac_src = dump["devices"][2]
wh_src = next(d for d in dump["devices"] if d["device_id"].startswith("5c:01"))
def make(cls, src):
    return cls(haier=Hr(), device_mac=src["device_id"], device_serial="x", device_title="t", backend_data=copy.deepcopy(src["backend_data"]))

# 1. the lookup itself
ac = make(api.HaierAC, ac_src)
mode_attr = ac.config.get_attr_by_name("mode")
check("known code -> its name", mode_attr.get_item_name("1"), "cool")
check("unknown code -> None", mode_attr.get_item_name("42"), None)
check("explicit default kept", mode_attr.get_item_name("42", "x"), "x")

# 2. an AC reporting codes the description does not list
ac._set_attribute_value(ac.config["status"], "1")
for name, code in (("mode", "42"), ("fan_mode", "42"), ("swing_mode", "42"), ("swing_horizontal_mode", "42"), ("eco_sensor", "42")):
    ac._set_attribute_value(ac.config[name], code)
check("unknown mode / fan / louvers / eco sensor are None",
      (ac.mode, ac.fan_mode, ac.swing_mode, ac.swing_horizontal_mode, ac.eco_sensor), (None,) * 5)
ent = climate.HaierACEntity(ac)
check("climate: a running unit in an unknown mode is unknown, not 'None' or 'off'", ent.hvac_mode, None)
check("climate: options do not get a None appended", (ent.swing_modes[-1] is not None, ent.swing_horizontal_modes[-1] is not None), (True, True))
check("select: unknown eco sensor option is None", select.HaierACEcoSensorSelect(ac).current_option, None)
ac._set_attribute_value(ac.config["status"], "0")
check("climate: an unknown mode of a unit that is off is still off", ent.hvac_mode, HVACMode.OFF)
ac._set_attribute_value(ac.config["status"], "1")
ac._set_attribute_value(ac.config["mode"], "1")
check("climate: a known mode again", ent.hvac_mode, "cool")
ac._set_attribute_value(ac.config["light"], "7")
check("switch with an unknown code is off", ac.light_on, False)

# 3. the water heater keeps the last known heating mode
wh = make(api.HaierWH, wh_src)
code = wh.config["heating_mode"]
wh._set_attribute_value(code, wh.config.get_attr_by_name("heating_mode").get_item_code("performance"))
check("heating mode set", wh.heating_mode, "performance")
wh._set_attribute_value(code, "42")
check("unknown heating mode keeps the last known one", wh.heating_mode, "performance")
wh._set_attribute_value(wh.config["heating_status"], "42")
check("unknown heating status is not heating", wh.heating, False)

print("FAILS:", fails)
sys.exit(1 if fails else 0)
