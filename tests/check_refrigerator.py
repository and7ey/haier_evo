"""A refrigerator (type REF) on the real description of a C4F640CWU1 (upstream
issue #52, anonymized in fixtures/ref_dump.json): what the integration reads
from it, the entities it creates, the commands it sends and how pushes apply."""
import sys, json, os, copy, tempfile, logging
from pathlib import Path
_HERE = Path(__file__).resolve().parent
root = sys.argv[1] if len(sys.argv) > 1 else str(_HERE.parent)
sys.path.insert(0, root)
logging.getLogger("custom_components.haier_evo").setLevel(logging.ERROR)
from custom_components.haier_evo import api

dump = json.loads((_HERE / "fixtures" / "ref_dump.json").read_text(encoding="utf-8"))
SRC = dump["devices"][0]
MAC = SRC["device_id"]

tmp = tempfile.mkdtemp()
class Conf:
    def path(self, *x): return os.path.join(tmp, *x)
class Loop:
    def call_soon_threadsafe(self, cb, *a): pass
class Hass: config = Conf(); loop = Loop()
class FakeHaier:
    def __init__(self): self.hass = Hass(); self.sent = []
    def send_message(self, payload): self.sent.append(json.loads(payload))
    def send_command(self, message): self.sent.append(dict(message))
    def get_device_by_id(self, mac): return None

fails = 0
def check(name, got, want=True):
    global fails
    ok = got == want
    fails += not ok
    print(("OK  " if ok else "FAIL"), name, "" if ok else f"\n      got:  {got!r}\n      want: {want!r}")

clients = []  # a device holds its client by a weak reference
def make(data=None):
    clients.append(FakeHaier())
    return api.HaierDevice.create(haier=clients[-1], device_type="REF", device_mac=MAC, device_serial=SRC["device_serial"],
                                  device_title=SRC["device_name"], backend_data=copy.deepcopy(data or SRC["backend_data"]))

def sent(device):
    out = [(m.get("action"), (m.get("command") or {}).get("commandName"), (m.get("command") or {}).get("value"))
           for m in device._haier.sent]
    device._haier.sent.clear()
    return out

def push(device, **values):
    device.on_message({"event": "status", "macAddress": device.device_id,
                       "payload": {"statuses": [{"properties": {k.lstrip("_"): v for k, v in values.items()}}]}})

STATE = ("current_fridge_temperature", "current_freezer_temperature", "current_temperature", "fridge_mode", "freezer_mode",
         "my_zone", "super_cooling", "super_freeze", "vacation_mode", "door_open")
def state(device):
    return {k: getattr(device, k) for k in STATE}

# ---- what is read
check("the app's device list names it a refrigerator",
      [(d["device_type"], d["device_mac"]) for d in api.Haier._parse_device_list(dump["backend_data"])], [("REF", MAC)])
ref = make()
check("built as a refrigerator, the model and firmware from the description",
      (type(ref).__name__, ref.device_model, ref.sw_version, ref.device_info["identifiers"]),
      ("HaierREF", "C4F640CWU1", "1.5.5", {("haier_evo", MAC)}))
check("every attribute of the description is recognized",
      sorted(a.name for a in ref.config.attrs),
      sorted(["current_fridge_temperature", "current_freezer_temperature", "current_temperature", "fridge_mode", "freezer_mode",
              "my_zone", "super_cooling", "super_freeze", "vacation_mode", "door_open"]))
check("the values of the description",
      state(ref),
      {"current_fridge_temperature": 5.0, "current_freezer_temperature": -18.0, "current_temperature": 25.0, "fridge_mode": "+5",
       "freezer_mode": "-18", "my_zone": "5", "super_cooling": False, "super_freeze": False, "vacation_mode": False, "door_open": False})
check("the options, as the app names them without ℃",
      (ref.get_fridge_mode_options(), ref.get_freezer_mode_options(), ref.get_my_zone_options()),
      (["+2", "+3", "+4", "+5", "+6", "+7", "+8"], [str(t) for t in range(-24, -15)], ["-5", "0", "5"]))
check("the state is JSON", bool(json.dumps(ref.to_dict(), ensure_ascii=False, allow_nan=False)), True)

# ---- entities
entities = {p: getattr(ref, f"create_entities_{p}")() for p in ("climate", "water_heater", "switch", "select", "sensor", "binary_sensor")}
ids = {p: [e._attr_unique_id.removeprefix(f"{MAC}_C4F640CWU1_") for e in es] for p, es in entities.items()}
check("entities: 3 switches, 3 selects, 5 sensors, 4 binary sensors, no climate or water heater",
      ids, {"climate": [], "water_heater": [],
            "switch": ["super_cooling_switch", "super_freeze_switch", "vacation_mode_switch"],
            "select": ["fridge_mode_select", "freezer_mode_select", "my_zone"],
            "sensor": ["temperature", "fridge_temperature", "freezer_temperature", "fridge_mode", "freezer_mode"],
            "binary_sensor": ["super_cooling", "super_freeze", "vacation", "door_open"]})
every = [e for es in entities.values() for e in es]
check("every unique_id once, every one under the device", (len({e._attr_unique_id for e in every}), all(
    e._attr_unique_id.startswith(f"{MAC}_C4F640CWU1") for e in every)), (len(every), True))
check("selects show the current option out of their options",
      [(e.current_option, e.current_option in e.options) for e in entities["select"]], [("+5", True), ("-18", True), ("5", True)])
check("sensors: temperatures in °C",
      [(e.native_value, e._attr_native_unit_of_measurement, str(e._attr_device_class)) for e in entities["sensor"]],
      [(25.0, "°C", "temperature"), (5.0, "°C", "temperature"), (-18.0, "°C", "temperature"),
       (5.0, "°C", "temperature"), (-18.0, "°C", "temperature")])
check("switches and binary sensors are off", [e.is_on for e in entities["switch"] + entities["binary_sensor"]], [False] * 7)

# ---- commands: one command each, the codes of the description (no command_name on a fridge)
calls = {
    "fridge +3": (lambda: ref.set_fridge_mode("+3"), [("command", "3", "4")]),
    "freezer -20": (lambda: ref.set_freezer_mode("-20"), [("command", "4", "10")]),
    "My Zone 0": (lambda: ref.set_my_zone("0"), [("command", "5", "31")]),
    "super cooling on": (lambda: ref.set_super_cooling(True), [("command", "6", "1")]),
    "super freeze off": (lambda: ref.set_super_freeze(False), [("command", "7", "0")]),
    "vacation on": (lambda: ref.set_vacation_mode(True), [("command", "8", "1")]),
}
got = {}
for name, (call, _) in calls.items():
    ref = make()
    call()
    got[name] = sent(ref)
check("commands: the code of the attribute and of the value", got, {name: want for name, (_, want) in calls.items()})
ref = make()
ref.set_fridge_mode("+3")
check("the value is shown at once, before the fridge echoes it", ref.fridge_mode, "+3")
ref = make()
ref.set_fridge_mode("+99")
ref.set_my_zone(None)
check("a value the fridge does not list: nothing sent, the state kept", (sent(ref), ref.fridge_mode, ref.my_zone), ([], "+5", "5"))
ref = make()
for entity, option in zip(ref.create_entities_select(), ("+8", "-24", "-5")):
    entity.set_option(option)  # what async_select_option runs in the executor
check("the selects send through the device", sent(ref), [("command", "3", "9"), ("command", "4", "6"), ("command", "5", "26")])
ref = make()
for entity in ref.create_entities_switch():
    entity.turn_on()
check("the switches send through the device", sent(ref), [("command", "6", "1"), ("command", "7", "1"), ("command", "8", "1")])

# ---- pushes
ref = make()
# switches and the door come as 1 / 0: the description's own values are numbers too
push(ref, _0="3", _1="-21", _2="24", _3="8", _4="9", _5="26", _6="1", _9="1")
check("a push applies every value: temperatures, modes, switches, the door",
      state(ref), {"current_fridge_temperature": 3.0, "current_freezer_temperature": -21.0, "current_temperature": 24.0,
                   "fridge_mode": "+7", "freezer_mode": "-21", "my_zone": "-5", "super_cooling": True, "super_freeze": False,
                   "vacation_mode": False, "door_open": True})
push(ref, _9=0, _6="0")
check("1 / 0 as text or as a number", (ref.door_open, ref.super_cooling), (False, False))
push(ref, _3="42", _5="1")
check("a mode code the description does not list: unknown (None), never 'None'", (ref.fridge_mode, ref.my_zone), (None, None))
push(ref, _0="abc", _1="-17")
check("an unreadable value is skipped, the others of the message apply",
      (ref.current_fridge_temperature, ref.current_freezer_temperature), (3.0, -17.0))
client = api.Haier(None, "e", "p", "ru")
ref = make()
client.devices = [ref]
client._on_message(None, json.dumps({"event": "status", "macAddress": MAC.upper(),
                                     "payload": {"statuses": [{"properties": {"9": "1"}}]}}))
check("a websocket message for its MAC (in any case) reaches the fridge", ref.door_open, True)

# ---- lock rules of the description
ref = make()
check("lock rules: super cooling locks the fridge mode, super freeze the freezer mode, vacation both the fridge mode and My Zone",
      (ref._locked({"6": "1"}), ref._locked({"7": "1"}), ref._locked({"8": "1"}), ref._locked()),
      ({"3": "6"}, {"4": "7"}, {"3": "8", "5": "8"}, {}))

print("FAILS:", fails)
sys.exit(1 if fails else 0)
