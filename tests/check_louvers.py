"""Louver positions named from the app's screens (modes.itemsV2), 1.3.0."""
import sys, json, os, copy, tempfile
from pathlib import Path
_HERE = Path(__file__).resolve().parent
root = sys.argv[1] if len(sys.argv) > 1 else str(_HERE.parent)
dump_path = sys.argv[2] if len(sys.argv) > 2 else str(_HERE / "fixtures" / "evo_dump.json")
sys.path.insert(0, root)
from custom_components.haier_evo import api, config as CFG, climate
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
    def send_message(s, p): s.sent.append(json.loads(p))
    def send_command(s, m): s.sent.append(dict(m))

acs = [d for d in dump["devices"] if not d["device_id"].startswith("5c:01")]
def make(dev, mutate=None):
    bd = copy.deepcopy(dev["backend_data"])
    if mutate: mutate(bd)
    h = Hr()
    return api.HaierAC(haier=h, device_mac=dev["device_id"], device_serial="x", device_title="t", backend_data=bd), h

V = ["auto", "angle_0", "angle_30", "angle_45", "angle_60", "angle_90", "comfort_up", "comfort_down"]
HZ = ["auto", "far_left", "left", "center", "right", "far_right"]

# 1. layout parsed from the app's screens
lay = CFG.louver_layout(acs[0]["backend_data"]["modes"])
check("layout vertical (attr 1)", lay.get("1"), [("8", "auto"), ("2", "angle_0"), ("4", "angle_30"), ("5", "angle_45"),
                                                ("6", "angle_60"), ("7", "angle_90"), ("1", "comfort_up"), ("3", "comfort_down")])
check("layout horizontal (attr 24)", lay.get("24"), [("7", "auto"), ("3", "far_left"), ("4", "left"), ("0", "center"),
                                                     ("5", "right"), ("6", "far_right")])
check("layout covers only the louver screens", sorted(lay), ["1", "24"])

# 2. every AC in the dump: options as in the app, hidden codes still decode
for dev in acs:
    ac, h = make(dev)
    tag = dev["device_id"][:8]
    check(f"{tag}: vertical options", ac.get_swing_modes(), V)
    check(f"{tag}: horizontal options", ac.get_swing_horizontal_modes(), HZ)
    dec_v = {c: ac.config.get_value("swing_mode", c) for c in map(str, range(10))}
    dec_h = {c: ac.config.get_value("swing_horizontal_mode", c) for c in map(str, range(8))}
    check(f"{tag}: vertical decode", dec_v, {"0": "off", "1": "comfort_up", "2": "angle_0", "3": "comfort_down", "4": "angle_30",
                                              "5": "angle_45", "6": "angle_60", "7": "angle_90", "8": "auto", "9": "special"})
    check(f"{tag}: horizontal decode", dec_h, {"0": "center", "1": "position_2", "2": "position_3", "3": "far_left",
                                                "4": "left", "5": "right", "6": "far_right", "7": "auto"})
    ac._set_attribute_value("1", "9")
    check(f"{tag}: unit in a hidden position shows it", ac.swing_mode, "special")
    check(f"{tag}: hidden items flagged in diagnostics",
          sorted(i["value"] for i in ac.config.get_attr_by_name("swing_mode").to_dict()["list"] if i["hidden"]), ["0", "9"])

# 3. no screens in the config: names exactly as before
ac, _ = make(acs[0], lambda bd: bd.pop("modes", None))
check("no itemsV2: old vertical names", ac.get_swing_modes(),
      ["off", "upper", "position_1", "bottom", "position_2", "position_3", "position_4", "position_5", "auto", "special"])
check("no itemsV2: old horizontal names", ac.get_swing_horizontal_modes(),
      ["position_1", "position_2", "position_3", "position_4", "position_5", "position_6", "position_7", "auto"])
ac, _ = make(acs[0], lambda bd: bd["modes"].pop("itemsV2"))
check("only modes.items (v1): same layout", ac.get_swing_modes(), V)

# 4. robustness of the parser
check("garbage modes", [CFG.louver_layout(x) for x in (None, [], "x", {"itemsV2": "x"}, {"itemsV2": [None, 1, {"value": {}}]})], [{}] * 5)
synthetic = {"itemsV2": [{"value": {"name": "1", "description": "windDirectionVertical"},
    "description": {"windDirectionVertical": {"types": [
        {"swing": {"value": "9"}},
        {"direction": {"modes": [{"name": "Чуть выше", "value": "2"}, {"name": "15", "value": "4"}, {"name": "Вверх", "value": "5"},
                                 {"name": "Вверх", "value": "6"}, {"name": "15", "value": "4"}, {"name": "x", "value": ""}]}},
    ]}}}]}
check("unknown groups/names keep a slot without a key, duplicate keys dropped",
      CFG.louver_layout(synthetic).get("1"), [("9", None), ("4", "angle_15"), ("2", None), ("5", "up"), ("6", None)])
ac, _ = make(acs[0], lambda bd: bd.__setitem__("modes", synthetic))
check("unnamed app positions stay visible with the firmware name, the rest hidden",
      ac.get_swing_modes(), ["special", "angle_15", "position_1", "up", "position_4"])

# 5. a model profile with its own louver mappings is left alone
cfg = CFG.HaierACConfig("HSU07HRM203", os.path.join(tmp, "p"))
cfg.attrs = [CFG.Attribute(copy.deepcopy(a)) for a in acs[0]["backend_data"]["attributes"]]
cfg.merge_attributes()
profile_modes = {"itemsV2": [{"value": {"name": "8", "description": "windDirectionVertical"},
                              "description": {"windDirectionVertical": {"types": [{"auto": {"value": "8"}}]}}}]}
cfg.apply_louver_layout(profile_modes)
check("profile mappings win", cfg.get_values("swing_mode"), ["off", "on"])

# 6. commands: same payload for the same code as 1.2.0 sent under the old name
ac, h = make(acs[0])
ac.switch_on("cool")
sent = {}
for name in V:
    h.sent.clear()
    try:
        ac.set_swing_mode(name)
        sent[name] = [(c.get("commandName"), c.get("value")) for m in h.sent for c in (m.get("commands") or [m.get("command") or m])]
    except Exception as e:
        sent[name] = type(e).__name__
for name in HZ:
    h.sent.clear()
    try:
        ac.set_swing_horizontal_mode(name)
        sent["h_" + name] = [(c.get("commandName"), c.get("value")) for m in h.sent for c in (m.get("commands") or [m.get("command") or m])]
    except Exception as e:
        sent["h_" + name] = type(e).__name__
print("SENT", json.dumps(sent, ensure_ascii=False, sort_keys=True))

# 7. HA entity: options include a hidden current value, validation uses them
ac, _ = make(acs[0])
ent = climate.HaierACEntity(ac)
check("entity options (vertical)", ent.swing_modes, V)
ac._set_attribute_value("1", "0")
check("entity: hidden current appended", ent.swing_modes, V + ["off"])
ac._set_attribute_value("24", "2")
check("entity: hidden horizontal current appended", ent.swing_horizontal_modes, HZ + ["position_3"])
check("helper: nothing to add", [climate._with_current(None, "x"), climate._with_current(["a"], None), climate._with_current(["a"], "a")],
      [None, ["a"], ["a"]])

# 8. every key the AC can show is translated, icons only for translated keys
for lang in ("ru", "en"):
    t = json.load(open(os.path.join(root, "custom_components/haier_evo/translations", f"{lang}.json")))
    st = t["entity"]["climate"]["conditioner"]["state_attributes"]
    check(f"{lang}: vertical keys translated", sorted(set(V + ["off", "special", "up", "down", "angle_15", "angle_75"]) - set(st["swing_mode"]["state"])), [])
    check(f"{lang}: horizontal keys translated", sorted(set(HZ + ["position_2", "position_3"]) - set(st["swing_horizontal_mode"]["state"])), [])
icons = json.load(open(os.path.join(root, "custom_components/haier_evo/icons.json")))["entity"]["climate"]["conditioner"]["state_attributes"]
ru = json.load(open(os.path.join(root, "custom_components/haier_evo/translations/ru.json")))["entity"]["climate"]["conditioner"]["state_attributes"]
for attr in ("swing_mode", "swing_horizontal_mode"):
    check(f"icons for {attr} are translated keys", sorted(set(icons[attr]["state"]) - set(ru[attr]["state"])), [])
# horizontal louvers seen from above: the air goes from the unit (top) down into the room (1.6.0)
hz = icons["swing_horizontal_mode"]["state"]
check("horizontal arrows point down: left down-left, center down, right down-right",
      (hz["left"], hz["center"], hz["right"]), ("mdi:arrow-bottom-left", "mdi:arrow-down", "mdi:arrow-bottom-right"))

print("FAILS:", fails)
sys.exit(1 if fails else 0)
