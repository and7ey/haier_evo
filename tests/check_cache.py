"""Saved device descriptions: start without the cloud, fresh state in the background, firmware flag."""
import sys, json, os, copy, tempfile
from pathlib import Path
_HERE = Path(__file__).resolve().parent
root = sys.argv[1] if len(sys.argv) > 1 else str(_HERE.parent)
dump_path = sys.argv[2] if len(sys.argv) > 2 else str(_HERE / "fixtures" / "evo_dump.json")
sys.path.insert(0, root)
from custom_components.haier_evo import api
from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.const import EntityCategory
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
class H:
    config = C(); loop = L()

def kind(dev): return "WH" if dev["device_id"].startswith("5c:01") else "AC"
def new_haier(): return api.Haier(H(), "e@x", None, "ru")

def walk(o):
    if isinstance(o, dict):
        for k, v in o.items():
            yield k, v
            yield from walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from walk(v)

# 1. what is saved
src = dump["devices"][1]["backend_data"]
saved = api.cacheable_description(copy.deepcopy(src))
strings = [v for _, v in walk(saved) if isinstance(v, str)]
check("no links in a saved description", [s for s in strings if s.startswith(("http://", "https://"))], [])
check("app analytics, pairing and scenarios dropped",
      sorted({k for k, _ in walk(saved)} & {"analyticsEvents", "analyticsEvent", "trackingData", "pairingInfo", "scenarios"}), [])
check("what the integration reads is kept",
      [k in saved for k in ("attributes", "modifiers", "constraint", "errors", "modes", "settings", "info", "alarms")], [True] * 8)
check("louver screens survive saving", api.CFG.louver_layout(saved["modes"]) == api.CFG.louver_layout(src["modes"]))
check("garbage in, empty description out", [api.cacheable_description(x) for x in (None, [], "x")], [{}, {}, {}])

# 2. a cloud start, saved, then a start from what was saved
h1 = new_haier()
for dev in dump["devices"]:
    d = api.HaierDevice.create(haier=h1, device_type=kind(dev), device_mac=dev["device_id"],
                               device_serial=dev["device_serial"], device_title=dev["device_name"],
                               backend_data=copy.deepcopy(dev["backend_data"]))
    h1.devices.append(d)
cache = json.loads(json.dumps(h1.cache_dict()))  # what Store writes and reads back
check("cache: version and one entry per device", (cache["version"], len(cache["devices"])), (1, 4))
check("cache: type, MAC, serial, title kept", [(c["type"], c["mac"], c["serial"], c["title"]) for c in cache["devices"]],
      [(d.device_type, d.device_id, d.device_serial, d.device_name) for d in h1.devices])
size = len(json.dumps(cache, ensure_ascii=False))
check(f"cache is small ({size // 1024} KB for 4 devices)", size < 400_000)

h2 = new_haier()
h2.load_cached(cache["devices"])
def shape(d):
    out = [type(d).__name__, d.device_model, d.device_name, d.sw_version]
    if isinstance(d, api.HaierAC):
        out += [d.get_hvac_modes(), d.get_fan_modes(), d.get_swing_modes(), d.get_swing_horizontal_modes(), d.get_preset_modes(),
                d.min_temperature, d.max_temperature]
    if isinstance(d, api.HaierWH):
        out += [d.get_heating_modes(), d.min_temperature, d.max_temperature]
    ents = []
    for f in ("climate", "water_heater", "switch", "select", "sensor", "binary_sensor"):
        ents += [type(e).__name__ for e in getattr(d, f"create_entities_{f}")()]
    return out + [ents]
check("from the cache: the same devices, options and entities", [shape(d) for d in h2.devices], [shape(d) for d in h1.devices])
check("from the cache: unavailable until the real state is read", [(d.synced, d.available) for d in h2.devices], [(False, False)] * 4)
check("cloud start: available at once", [d.available for d in h1.devices], [True] * 4)

# 3. the real state arrives
for dev, d in zip(dump["devices"], h2.devices):
    fresh = copy.deepcopy(dev["backend_data"])
    if isinstance(d, api.HaierAC):
        for a in fresh["attributes"]:
            if a["name"] == "0":
                a["currentValue"] = "27"
    check(f"{d.device_name}: same description -> no reload", d.apply_fresh(fresh), False)
    check(f"{d.device_name}: available after the fresh read", (d.synced, d.available), (True, True))
    if isinstance(d, api.HaierAC):
        check(f"{d.device_name}: fresh values applied", d.target_temperature, 27.0)

dev0 = dump["devices"][2]
d = h2.devices[2]
check("signature ignores what the integration does to its copy",
      api.HaierDevice.description_signature(copy.deepcopy(dev0["backend_data"])), api.HaierDevice.description_signature(d.status_data))
changed = copy.deepcopy(dev0["backend_data"]); changed["settings"]["firmware"]["value"] = "1.7.0"
check("new firmware -> rebuild", d.apply_fresh(changed), True)
check("rebuild: the saved description is the new one", d.cache_dict()["config"]["settings"]["firmware"]["value"], "1.7.0")
changed = copy.deepcopy(dev0["backend_data"])
next(a for a in changed["attributes"] if a["name"] == "1")["list"]["data"].append({"data": "10", "name": "Новое положение"})
d3 = h2.devices[3]
check("new attribute value -> rebuild", d3.apply_fresh(changed), True)
check("values and alarms do not rebuild", h2.devices[1].apply_fresh(dict(copy.deepcopy(dump["devices"][1]["backend_data"]), alarms=["x"])), False)

# 4. refresh_devices
h3 = new_haier()
h3.load_cached(cache["devices"])
calls = []
def pull(mac):
    calls.append(mac)
    if mac == "30:c9:22:00:00:03":
        raise api.requests.exceptions.ConnectTimeout("t")
    return copy.deepcopy(next(x for x in dump["devices"] if x["device_id"] == mac)["backend_data"])
h3.pull_device_data = pull
res = h3.refresh_devices()
check("refresh: one device timed out, the rest are live", (res, [d.available for d in h3.devices]),
      ({"failed": ["30:c9:22:00:00:03"], "changed": False}, [True, True, False, True]))
def reauth(mac): raise api.ReauthRequired("no")
h3.pull_device_data = reauth
try:
    h3.refresh_devices(); got = None
except api.ReauthRequired as e:
    got = e
check("refresh: a rejected sign-in is not swallowed", isinstance(got, api.ReauthRequired))
h3.stop()
h3.pull_device_data = pull
check("refresh after stop does nothing", h3.refresh_devices(), {"failed": [], "changed": False})

# 5. the app's device list
h4 = new_haier()
h4.pull_data_from_api = lambda: copy.deepcopy(dump["backend_data"])
check("device list: MACs of the app", h4.list_device_ids(), {d["device_id"] for d in dump["devices"]})
h4.pull_data_from_api = lambda: {}
check("device list: empty answer = unknown", h4.list_device_ids(), None)
def boom(): raise RuntimeError("x")
h4.pull_data_from_api = boom
check("device list: error = unknown", h4.list_device_ids(), None)

# 6. bad caches
for bad in ([], [{"mac": "x", "config": {}}], [{"mac": "5c:01:3b:00:00:01"}], [{"mac": "5c:01:3b:00:00:01", "config": "x"}]):
    try:
        new_haier().load_cached(bad); got = None
    except ValueError as e:
        got = e
    check(f"bad cache {str(bad)[:40]} -> ValueError", isinstance(got, ValueError))

# 7. firmware flag
ac = h1.devices[1]
check("firmware: no update waiting, auto-update on", (ac.firmware_update, ac.firmware_auto_update, ac.sw_version), (False, True, "1.6.0"))
ac.status_data["settings"]["firmware"]["updateRequired"] = True
check("firmware: update waiting", ac.firmware_update, True)
ac.status_data["settings"].pop("firmware")
check("firmware: unknown without the block", (ac.firmware_update, ac.firmware_auto_update), (None, None))
ac.status_data["settings"]["firmware"] = {"value": "1.6.0", "updateRequired": True, "autoUpdate": {"enabled": False}}
sensor = next(e for e in ac.create_entities_binary_sensor() if type(e).__name__ == "HaierFirmwareUpdateSensor")
check("firmware sensor", (sensor.is_on, sensor.extra_state_attributes, sensor.device_class, sensor.entity_category, sensor.name),
      (True, {"installed_version": "1.6.0", "auto_update": False}, BinarySensorDeviceClass.UPDATE, EntityCategory.DIAGNOSTIC,
       f"{ac.device_name} Обновление прошивки"))
check("firmware sensor unique id", sensor.unique_id, f"{ac.device_id}_{ac.device_model}_firmware_update")
wh = h1.devices[0]
check("boiler has the firmware sensor too",
      "HaierFirmwareUpdateSensor" in [type(e).__name__ for e in wh.create_entities_binary_sensor()])

# 8. link quality event (was logged as an unknown message)
ac.on_message({"event": "deviceConnectionStatusEvent", "macAddress": ac.device_id, "payload": {"status": "GOOD"}})
conn = next(e for e in ac.create_entities_binary_sensor() if type(e).__name__ == "HaierConnectivitySensor")
check("link status kept and shown on the connectivity sensor", (ac.link_status, conn.extra_state_attributes), ("GOOD", {"link": "GOOD"}))

for h in (h1, h2, h4):
    h.stop()
print("FAILS:", fails)
sys.exit(1 if fails else 0)
