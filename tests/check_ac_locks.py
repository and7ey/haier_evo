"""AC locks, deferred setpoint, alarms and new entities on the real office AC data."""
import sys, json, os, copy, tempfile, asyncio
from pathlib import Path
_HERE = Path(__file__).resolve().parent
root = sys.argv[1] if len(sys.argv) > 1 else str(_HERE.parent)
dump_path = sys.argv[2] if len(sys.argv) > 2 else str(_HERE / "fixtures" / "evo_dump.json")
dump = json.load(open(dump_path))
sys.path.insert(0, root)
from custom_components.haier_evo import api

tmp = tempfile.mkdtemp()
class FakeConfig:
    def path(self, x): return os.path.join(tmp, x)
class FakeLoop:
    def call_soon_threadsafe(self, cb): pass
class FakeHass:
    config = FakeConfig()
    loop = FakeLoop()
    async def async_add_executor_job(self, fn, *a): return fn(*a)
class FakeHaier:
    def __init__(self): self.hass = FakeHass(); self.sent = []
    def send_command(self, m): self.sent.append(m)
    def send_message(self, p): self.sent.append(json.loads(p))
    def get_device_by_id(self, mac): return None

src = [d for d in dump["devices"] if d["device_id"].startswith("30:c9:22:00:00:03")][0]
h = FakeHaier()
ac = api.HaierAC(haier=h, device_mac=src["device_id"], device_serial="x", device_title="Кабинет",
                 backend_data=copy.deepcopy(src["backend_data"]))

def sent():
    out = []
    for m in h.sent:
        cmds = m.get("commands") or [m.get("command")]
        out.append([(c["commandName"], c["value"]) for c in cmds])
    h.sent.clear()
    return out

def ws(props=None, alarms=None):
    st = {"properties": props or {}}
    if alarms is not None:
        st["alarms"] = alarms
    ac.on_message({"event": "status", "macAddress": ac.device_id, "payload": {"statuses": [st]}})

fails = 0
def check(name, got, exp):
    global fails
    ok = got == exp
    fails += not ok
    print(("OK  " if ok else "FAIL"), name, "->", got, "" if ok else f"(expected {exp})")

def locked(fn):
    try:
        fn()
        return None
    except api.HaierCommandLocked as e:
        return e.translation_key, e.translation_placeholders

D = "Кондиционер 3"
check("initial: off, auto", (ac.status, ac.mode, ac._values.get("21"), ac._values.get("2")), (0, "auto", "0", "0"))
check("off: turbo locked", locked(lambda: ac.set_turbo_on(True)), ("locked_off", {"device": D, "attribute": "Турбо"}))
check("off: fan locked", locked(lambda: ac.set_fan_mode("low")), ("locked_off", {"device": D, "attribute": "Скорость вентилятора"}))
check("off: sound allowed", locked(lambda: ac.set_sound_on(False)), None); sent()
check("off: setpoint deferred, nothing sent", (locked(lambda: ac.set_temperature(19)), sent(), ac.target_temperature, ac._deferred_target), (None, [], 19, 19))

ac.switch_on("fan_only")
cmds = sent()
check("on in fan_only: power+mode, setpoint dropped (fan_only has none), unit's own shown",
      (cmds[0][0], ("2", "6") in cmds[0], any(c[0] == "0" for c in cmds[0]), ac._deferred_target, ac.target_temperature), (("21", "1"), True, False, None, 18.0))
check("fan_only: fan auto not allowed", locked(lambda: ac.set_fan_mode("auto")),
      ("value_not_allowed", {"device": D, "attribute": "Скорость вентилятора", "allowed": "Быстрый режим, Средний режим, Медленный режим"}))
check("fan_only: fan low allowed", (locked(lambda: ac.set_fan_mode("low")), ("4", "3") in sent()[0]), (None, True))
check("fan_only: setpoint locked by mode", locked(lambda: ac.set_temperature(20)),
      ("locked_mode", {"device": D, "attribute": "Целевая температура", "mode": "Вентилятор"}))
check("fan_only: turbo locked by mode", locked(lambda: ac.set_turbo_on(True))[0], "locked_mode")
check("fan_only: preset boost locked", locked(lambda: ac.set_preset_mode("boost"))[0], "locked_mode")
check("fan_only: preset none passes", locked(lambda: ac.set_preset_mode("none")), None); sent()

ac.switch_on("cool")
cmds = sent()
check("cool while on: power on and mode, no setpoint (nothing deferred), no cleaning extras",
      (("2", "1") in cmds[0], any(c[0] == "0" for c in cmds[0]), cmds[0][0], any(c[0] in ("6", "31") for c in cmds[0])),
      (True, False, ("21", "1"), False))
check("cool: cleaning allowed", (locked(lambda: ac.set_cleaning_on(True)), sent()[0][0]), (None, ("6", "1")))
check("cleaning on: setpoint locked by cleaning", locked(lambda: ac.set_temperature(22)),
      ("locked_by", {"device": D, "attribute": "Целевая температура", "cause": "Стерильная очистка"}))
check("cleaning on: autohumidity locked", locked(lambda: ac.set_autohumidity_on(True))[0], "locked_by")
check("cleaning off allowed", locked(lambda: ac.set_cleaning_on(False)), None); sent()
check("auto mode: turbo locked", (ac.switch_on("auto"), sent() and None, locked(lambda: ac.set_turbo_on(True))[0])[2], "locked_mode")
ac.switch_off(); sent()

# deferred setpoint is dropped when the unit is turned on elsewhere
ac.set_temperature(25); sent()
check("deferred before external on", ac._deferred_target, 25)
ws({"21": "1"})
check("external power on drops deferred setpoint", ac._deferred_target, None)
ws({"21": "0"})

# climate.set_temperature with hvac_mode on an off unit -> one command
from custom_components.haier_evo import climate
ent = climate.HaierACEntity(ac)
ent.hass = FakeHass()
asyncio.run(ent.async_set_temperature(temperature=21, hvac_mode="cool"))
cmds = sent()
check("set_temperature+hvac_mode on off AC: one command with power, mode, setpoint",
      (len(cmds), cmds[0][0], ("2", "1") in cmds[0], ("0", "21") in cmds[0]), (1, ("21", "1"), True, True))
try:
    asyncio.run(ent.async_set_temperature(temperature=22, hvac_mode="fan_only"))
    err = None
except api.HaierCommandLocked as e:
    err = e.translation_key
cmds = sent()
check("running AC + fan_only: mode switched first, then setpoint refused", (("2", "6") in cmds[0], err), (True, "locked_mode"))
ac.switch_off(); sent()

# alarms: filter reminder is separate and does not lock the AC
ws({}, ["filterCleaningAlarm"])
check("filter alarm: filter on, problem off", (ac.filter_cleaning, ac.problem), (True, False))
ac.switch_on("cool"); sent()
check("filter alarm does not lock controls", (locked(lambda: ac.set_temperature(23)), sent()), (None, [[("0", "23")]]))
ws({}, ["indoorFanErr"])
check("fault alarm: problem with cloud text", (ac.problem, ac.alarm_texts[0][:4]), (True, ac.status_data["errors"]["alarmToResult"]["indoorFanErr"]["text"][:4]))
ws({}, ["alarmCancel"])
check("alarmCancel clears", (ac.problem, ac.filter_cleaning), (False, False))

# connectivity
ac.on_message({"event": "deviceStatusEvent", "macAddress": ac.device_id, "payload": {"status": "OFFLINE"}})
check("offline", (ac.connected, ac.available), (False, True))
ac.on_message({"event": "deviceStatusEvent", "macAddress": ac.device_id, "payload": {"status": "ONLINE"}})
check("online", ac.connected, True)

ents = ac.create_entities_sensor() + ac.create_entities_binary_sensor()
print("new entities:", [(type(e).__name__, e.unique_id, e.name) for e in ents])
check("new entity classes", [type(e).__name__ for e in ents],
      ["HaierACTemperatureSensor", "HaierACOutdoorTemperatureSensor", "HaierProblemSensor", "HaierFilterSensor",
       "HaierConnectivitySensor", "HaierFirmwareUpdateSensor"])
check("temperature sensor value", ents[0].native_value, ac.current_temperature)
check("to_dict json", bool(json.dumps(ac.to_dict(), ensure_ascii=False, default=str)), True)
print("FAILS:", fails)
sys.exit(1 if fails else 0)
