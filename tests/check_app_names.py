"""AC features named as in the Haier app; switches the app does not offer are not created."""
import sys, json, os, copy, tempfile
from types import SimpleNamespace
from pathlib import Path
_HERE = Path(__file__).resolve().parent
root = sys.argv[1] if len(sys.argv) > 1 else str(_HERE.parent)
dump_path = sys.argv[2] if len(sys.argv) > 2 else str(_HERE / "fixtures" / "evo_dump.json")
sys.path.insert(0, root)
from custom_components.haier_evo import api, config as CFG, switch
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

src = dump["devices"][2]   # an AC
def make(mutate=None, title="Кондиционер 3"):
    bd = copy.deepcopy(src["backend_data"])
    if mutate:
        mutate(bd)
    h = Hr()
    return api.HaierAC(haier=h, device_mac=src["device_id"], device_serial="x", device_title=title, backend_data=bd), h

# 1. the app's screens
screens = CFG.app_screens(src["backend_data"])
check("screens: app titles by attribute",
      {k: screens.get(k) for k in ("12", "16", "13", "31", "6", "17", "18", "20", "23", "1", "24")},
      {"12": "Подсветка блока", "16": "Комфортный сон", "13": "10 градусов", "31": "Самоочистка", "6": "Стерильная очистка",
       "17": "Тихий", "18": "Турбо", "20": "Здоровье", "23": "Эко датчик", "1": "Вертикальные жалюзи", "24": "Горизонтальные жалюзи"})
check("screens: the beep (14) is not in the app", "14" in screens, False)
check("no itemsV2 -> no screens (nothing hidden)", CFG.app_screens({k: v for k, v in src["backend_data"].items() if k != "modes"}), {})
check("garbage -> no screens", [CFG.app_screens(x) for x in (None, [], {"modes": "x"}, {"modes": {"itemsV2": [1, {"value": {}}]}})],
      [{}, {}, {}, {}])
wh = next(d for d in dump["devices"] if d["device_id"].startswith("5c:01"))
check("water heater: no screens described", CFG.app_screens(wh["backend_data"]), {})

# 2. AC switches
ac, h = make()
ents = ac.create_entities_switch()
check("switches: names from the app, no beep switch", [e.name for e in ents],
      [f"Кондиционер 3 {t}" for t in ("Подсветка блока", "Тихий", "Турбо", "Здоровье", "Комфортный сон",
                                         "Стерильная очистка", "10 градусов", "Самоочистка")])
check("switches: unique ids as in 1.4.0 (entity ids and history stay)", [e.unique_id for e in ents],
      [f"{ac.device_id}_{ac.device_model}_{s}" for s in ("light", "quiet", "turbo", "health", "comfort", "cleaning", "antifreeze", "autohumidity")])
check("switches: classes kept", [type(e).__name__ for e in ents],
      ["HaierACLightSwitch", "HaierACQuietSwitch", "HaierACTurboSwitch", "HaierACHealthSwitch", "HaierACComfortSwitch",
       "HaierACCleaningSwitch", "HaierACAntiFreezeSwitch", "HaierACAutoHumiditySwitch"])
check("the beep switch is listed for removal", ac.hidden_entities, [("switch", f"{ac.device_id}_{ac.device_model}_sound")])
check("a second call does not pile up removals", (ac.create_entities_switch() and ac.hidden_entities), [("switch", f"{ac.device_id}_{ac.device_model}_sound")])
selfclean = next(e for e in ents if type(e).__name__ == "HaierACAutoHumiditySwitch")
check("self-cleaning switch: state and command of attribute 31",
      (selfclean._device_attr_name, ac.config["autohumidity"]), ("autohumidity_on", "31"))

# 3. no screens: every switch, old fallback-free names from the app titles not available
ac2, _ = make(lambda bd: bd.pop("modes"))
names2 = [e.name for e in ac2.create_entities_switch()]
check("no screens: all switches, the beep included", len(names2), 9)
check("no screens: cloud list names (e.g. 31 = 'Авто влажность')",
      next(n for n in names2 if n.endswith("влажность")), "Кондиционер 3 Авто влажность")
check("no screens: nothing to remove", ac2.hidden_entities, [])

# 4. lock messages use the app's names
ac3, h3 = make()
ac3.switch_on("auto")
try:
    ac3.set_autohumidity_on(True); err = None
except api.HaierCommandLocked as e:
    err = e
check("locked in auto: attribute named as in the app",
      (getattr(err, "translation_key", None), (getattr(err, "translation_placeholders", None) or {}).get("attribute")),
      ("locked_mode", "Самоочистка"))

# 5. registry cleanup at platform setup
removed = []
class Reg:
    def async_get_entity_id(self, platform, domain, unique_id):
        return f"{platform}.x_{unique_id.rsplit('_', 1)[1]}" if unique_id.endswith("_sound") else None
    def async_remove(self, entity_id): removed.append(entity_id)
orig = switch.er.async_get
switch.er.async_get = lambda hass: Reg()
try:
    switch._remove_hidden(None, [ac, ac2, SimpleNamespace()])
finally:
    switch.er.async_get = orig
check("old beep switch removed from the registry", removed, ["switch.x_sound"])

# 6. translations and icons
for lang in ("ru", "en"):
    t = json.load(open(os.path.join(root, "custom_components/haier_evo/translations", f"{lang}.json")))
    st = t["entity"]["select"]["conditioner_eco_sensor"]["state"]
    check(f"{lang}: eco sensor options translated", sorted(st), sorted(ac.get_eco_sensor_options()))
ru = json.load(open(os.path.join(root, "custom_components/haier_evo/translations/ru.json")))["entity"]["select"]["conditioner_eco_sensor"]["state"]
check("ru: eco sensor flows say who gets the air", (ru["related"], ru["outlining"]), ("Поток на человека", "Поток мимо человека"))

print("FAILS:", fails)
sys.exit(1 if fails else 0)
