"""Washing machines and dryers in the cloud's current format (fixtures/laundry_dump.json:
HW60-BP12929AE, HW90-B14367U1 and the dryer HD80-A2367U1 from upstream issues, anonymized):
what each code means comes from the app's screens, the state, program, phase and time
left, pushes and program events; nothing is sent unless the control (beta) is on, and
then only what the cloud's description defines, when the machine allows it."""
import sys, json, os, copy, tempfile, logging
from pathlib import Path
_HERE = Path(__file__).resolve().parent
root = sys.argv[1] if len(sys.argv) > 1 else str(_HERE.parent)
sys.path.insert(0, root)
logging.getLogger("custom_components.haier_evo").setLevel(logging.ERROR)
from custom_components.haier_evo import api

dump = json.loads((_HERE / "fixtures" / "laundry_dump.json").read_text(encoding="utf-8"))
PKG = Path(root) / "custom_components" / "haier_evo"
HW60, HW90, HD80 = dump["devices"]
TYPES = {"HW60": "WM", "HW90": "WM", "HD80": "DRY"}

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
def make(src, kind=None, data=None):
    clients.append(FakeHaier())
    kind = kind or TYPES[src["device_name"].split()[-1][:4]]
    return api.HaierDevice.create(haier=clients[-1], device_type=kind, device_mac=src["device_id"], device_serial=src["device_serial"],
                                  device_title=src["device_name"], backend_data=copy.deepcopy(src["backend_data"] if data is None else data))

def push(device, **values):
    device.on_message({"event": "status", "macAddress": device.device_id,
                       "payload": {"statuses": [{"properties": {k.lstrip("_"): v for k, v in values.items()}}]}})

def event(device, name, **data):
    device.on_message({"event": name, "macAddress": device.device_id, "data": data})

def view(m):
    return (m.state, m.program_title, m.phase, m.remaining_time)

# ---- what is read
check("the app's device list: a washing machine and a dryer",
      [(d["device_type"], d["device_title"]) for d in api.Haier._parse_device_list(dump["backend_data"])],
      [("WM", "Washing Machine HW90-B14367U1"), ("DRY", "Dryer HD80-A2367U1")])
hw60, hw90, hd80 = make(HW60), make(HW90), make(HD80)
check("built as laundry machines, model and firmware from smartDeviceControl",
      [(type(m).__name__, m.device_model, m.sw_version, m.is_dryer) for m in (hw60, hw90, hd80)],
      [("HaierLaundry", "HW60BP12929", "1.5.8", False), ("HaierLaundry", "HW90B14367U", "1.5.8", False),
       ("HaierLaundry", "HD80A2367U1", "1.5.8", True)])
KEYS = ("onOffStatus", "remoteCtrValid", "childLockStatus", "cyclePhase", "remainingTimeHH", "remainingTimeMM", "remainingDryTime")
check("what the codes mean comes from the app's screens: other models, other codes",
      [{k: m.codes.get(k) for k in KEYS} for m in (hw60, hw90, hd80)],
      [{"onOffStatus": "25", "remoteCtrValid": "24", "childLockStatus": "14", "cyclePhase": "18",
        "remainingTimeHH": "31", "remainingTimeMM": "32", "remainingDryTime": None},
       {"onOffStatus": "25", "remoteCtrValid": "24", "childLockStatus": "14", "cyclePhase": "18",
        "remainingTimeHH": "32", "remainingTimeMM": "33", "remainingDryTime": None},
       {"onOffStatus": "6", "remoteCtrValid": "3", "childLockStatus": "4", "cyclePhase": "22",
        "remainingTimeHH": None, "remainingTimeMM": None, "remainingDryTime": "8"}])
check("state, program, phase and minutes left of the description",
      [view(m) for m in (hw60, hw90, hd80)],
      [("running", "Ежедневная", "Этап 5", 54), ("running", "Постельное бельё", "Этап 5", 86),
       ("running", "Быстрая 30 мин", "Нагрев", 4)])
check("remote control and child lock", [(m.remote_control, m.child_lock) for m in (hw60, hw90, hd80)],
      [(False, False), (True, False), (False, False)])
check("the phase titles of the app: the dryer names every phase, a washer a few",
      (hd80.phase_titles.get("0"), hd80.phase_titles.get("6"), hw60.phase_titles.get("1"), hw60.phase_titles.get("14")),
      ("Режим ожидания", "Завершение программы", "Взвешивание белья", "Завершение программы"))
check("the state is JSON", all(json.dumps(m.to_dict(), ensure_ascii=False, allow_nan=False) for m in (hw60, hw90, hd80)), True)

# ---- entities: read only
def entities(m):
    return {p: getattr(m, f"create_entities_{p}")() for p in ("climate", "water_heater", "switch", "select", "sensor", "binary_sensor")}
ents = entities(hw60)
mac = HW60["device_id"]
check("entities: 4 sensors and 3 binary sensors, nothing that sends a command",
      {p: [e._attr_unique_id.removeprefix(f"{mac}_HW60BP12929_") for e in es] for p, es in ents.items()},
      {"climate": [], "water_heater": [], "switch": [], "select": [],
       "sensor": ["state", "program", "phase", "remaining_time"], "binary_sensor": ["problem", "remote_control", "child_lock"]})
check("sensor values", [e.native_value for e in ents["sensor"]], ["running", "Ежедневная", "Этап 5", 54])
check("the state sensor: an enum with its options translated in en and ru",
      (ents["sensor"][0]._attr_options, {lang: sorted(json.loads((PKG / "translations" / f"{lang}.json").read_text(encoding="utf-8"))
                                                      ["entity"]["sensor"]["laundry_state"]["state"]) for lang in ("en", "ru")}),
      (list(api.LAUNDRY_STATES), {lang: sorted(api.LAUNDRY_STATES) for lang in ("en", "ru")}))
check("the time left in minutes", (str(ents["sensor"][3]._attr_device_class), ents["sensor"][3]._attr_native_unit_of_measurement),
      ("duration", "min"))
check("the dryer has the same entities", [len(es) for es in entities(hd80).values()], [0, 0, 0, 0, 4, 3])
check("nothing was sent to any machine", [c.sent for c in clients], [[] for _ in clients])

# ---- pushes and events
m = make(HW60)
push(m, _18="14", _31="0", _32="3")
check("a push: the phase titled by the app, the time from hours and minutes", (m.phase, m.remaining_time), ("Завершение программы", 3))
push(m, _31="1", _32="5")
check("... 1 h 5 min", m.remaining_time, 65)
push(m, _24="true", _14="1")
check("remote control on, child lock on", (m.remote_control, m.child_lock), (True, True))
event(m, "ProgramWasherBaseEvent", program={"title": "Ежедневная", "status": "PAUSED"})
check("a program event: paused", m.state, "paused")
event(m, "ProgramWasherBaseEvent", program={"title": "Ежедневная", "status": "PROGRAM_FINISHED"})
check("... finished (with the PROGRAM_ prefix too)", m.state, "finished")
event(m, "ProgramWasherBaseEvent", status="PROGRAM_STARTED")
check("a start without the program: running, the title kept", (m.state, m.program_title), ("running", "Ежедневная"))
event(m, "ProgramWasherBaseEvent", status="PROGRAM_FINISHED")
check("a finish without the program: finished", m.state, "finished")
push(m, _25="false")
check("switched off: off, no time left (the machine keeps the last value)", (m.state, m.remaining_time), ("off", 0))
d = make(HD80)
push(d, _22="7", _8="12")
check("the dryer: anti-crease phase, minutes as one value", (d.phase, d.remaining_time), ("Анти-сминание", 12))
event(d, "ProgramDryerBaseEvent", program={"title": "Быстрая 30 мин", "status": "CANCELED"})
check("the dryer: a cancelled program", d.state, "cancelled")
d.on_message({"event": "alarms", "macAddress": d.device_id, "payload": {"alarms": ["E21"]}})
check("an alarms event: the problem sensor is on", (d.problem, d.alarms), (True, ["E21"]))
d.on_message({"event": "alarms", "macAddress": d.device_id, "payload": {"alarms": ["alarmCancel"]}})
check("alarmCancel clears it", d.problem, False)
for bad in ({"event": "ProgramWasherBaseEvent", "data": None}, {"event": "ProgramDryerBaseEvent", "data": {"program": "x"}},
            {"event": "alarms", "payload": None}, {"event": "status", "payload": {"statuses": [{"properties": {"22": None}}]}}):
    d.on_message(dict(bad, macAddress=d.device_id))
check("broken events change nothing", (d.state, d.phase, d.problem), ("cancelled", "Анти-сминание", False))

client = api.Haier(None, "e", "p", "ru")
m = make(HW60)
client.devices = [m]
client._on_message(None, json.dumps({"event": "ProgramWasherBaseEvent", "macAddress": mac.upper(),
                                     "data": {"program": {"title": "Хлопок", "status": "IN_PROGRESS"}}}))
check("a program event over the websocket reaches its machine", (m.state, m.program_title), ("running", "Хлопок"))

# ---- the state read again (REST) and a start from the saved description
m = make(HW60)
fresh = copy.deepcopy(HW60["backend_data"])
fresh["control"]["currentProgram"]["status"] = "FINISHED"
for attr in fresh["smartDeviceControl"]["attributes"]:
    if attr.get("name") == "18":
        attr["currentValue"] = "14"
m.update_from_backend(fresh)
check("a fresh REST state: the program and the values inside smartDeviceControl", (m.state, m.phase), ("finished", "Завершение программы"))
fresh["control"].pop("currentProgram")
m.update_from_backend(fresh)
check("no program in it: the phase decides (not 0: running)", (m.program_title, m.state), (None, "running"))
saved = m.cache_dict()
again = make(HW60, data=saved["config"])
check("started from the saved description: the same codes and entities",
      (type(again).__name__, again.codes == m.codes, [len(es) for es in entities(again).values()]), ("HaierLaundry", True, [0, 0, 0, 0, 4, 3]))
check("the description signature reads smartDeviceControl", len(api.HaierDevice.description_signature(HW60["backend_data"])[2]) > 50, True)

# ---- what is not this format
old = make(HW60, kind="WM", data={"attributes": [], "info": {"model": "CASWD129C1CU"}})
check("a washer description without smartDeviceControl stays with the old HaierWM", type(old).__name__, "HaierWM")
check("an air conditioner type with smartDeviceControl is not taken for a washer", type(make(HW60, kind="AC")).__name__, "HaierAC")
bare = copy.deepcopy(HW60["backend_data"])
for key in ("allProgram", "control", "controlBlock", "remoteControlMode"):
    bare.pop(key)
m = make(HW60, data=bare)
check("without the app's screens: none of these meanings, no guessing, only the state and program sensors",
      ({k: v for k, v in m.codes.items() if k in KEYS}, m.state, [e._attr_unique_id.rsplit("_", 1)[-1] for e in entities(m)["sensor"]]),
      ({}, None, ["state", "program"]))

# ---- control (beta): only with the entry's option on
class ControlHaier(FakeHaier):
    laundry_control = True

def make_ctl(src, kind=None, data=None):
    clients.append(ControlHaier())
    kind = kind or TYPES[src["device_name"].split()[-1][:4]]
    return api.HaierDevice.create(haier=clients[-1], device_type=kind, device_mac=src["device_id"], device_serial=src["device_serial"],
                                  device_title=src["device_name"], backend_data=copy.deepcopy(src["backend_data"] if data is None else data))

def raised(fn):
    try:
        fn()
    except Exception as e:  # noqa: BLE001 - the checks look at what came out
        return getattr(e, "translation_key", type(e).__name__)
    return None

def program_state(m, status):
    event(m, "ProgramDryerBaseEvent" if m.is_dryer else "ProgramWasherBaseEvent", program={"title": m.program_title, "status": status})

def remote(m, on):
    push(m, **{"_" + m.codes["remoteCtrValid"]: "1" if on else "0"})

check("control off (the default): no program to start and no buttons, as before",
      [(m.create_entities_select(), m.create_entities_button()) for m in (make(HW60), make(HW90), make(HD80))], [([], [])] * 3)
w60, w90, d80 = make_ctl(HW60), make_ctl(HW90), make_ctl(HD80)
business = {name: {b["name"]: b["commandParameters"] for b in src["backend_data"]["smartDeviceControl"]["businessAttributes"]}
            for name, src in (("HW60", HW60), ("HW90", HW90), ("HD80", HD80))}
check("programs from the app's catalogue that have a definition: every one once, in the app's order",
      [(len(m.programs), len({p["title"] for p in m.programs}), m.programs[0]["title"]) for m in (w60, w90, d80)],
      [(28, 28, "Кашемир"), (29, 29, "Белые вещи"), (20, 20, "Хлопок")])
check("a program is its definition's group command with every value it has (defaults; the machine sets the rest)",
      all(p["group"] == business[n][p["link"]]["name"] and p["commands"] == [
          {"commandName": str(f["name"]), "value": str(f["defaultValue"])}
          for f in business[n][p["link"]]["attrNameList"] if f.get("defaultValue") is not None]
          for n, m in (("HW60", w60), ("HW90", w90), ("HD80", d80)) for p in m.programs), True)
check("the delayed start's status of a washer (no value in the definition) is not sent",
      "15" in {str(f["name"]) for f in business["HW60"]["washTypeAntiAllergy"]["attrNameList"]}
      and "15" not in {c["commandName"] for p in w60.programs if p["link"] == "washTypeAntiAllergy" for c in p["commands"]}, True)
dryer_program = d80.programs[0]
check("a dryer's program turns the machine on and sets it running, in the same command",
      ({"commandName": "6", "value": "true"} in dryer_program["commands"], {"commandName": "5", "value": "1"} in dryer_program["commands"]),
      (True, True))
check("start, pause and cancel as the app's control screens give them (the codes differ between models)",
      [m.actions for m in (w60, w90, d80)],
      [{"pause": {"commandName": "19", "value": "2"}, "resume": {"commandName": "19", "value": "1"}, "cancel": {"commandName": "194", "value": "1"}},
       {"pause": {"commandName": "19", "value": "2"}, "resume": {"commandName": "19", "value": "1"}, "cancel": {"commandName": "144", "value": "1"}},
       {"pause": {"commandName": "5", "value": "2"}, "resume": {"commandName": "5", "value": "1"}, "cancel": {"commandName": "44", "value": "1"}}])
check("the program suggested to start: the machine's last one, as the app does",
      [m.selected_program for m in (w60, w90, d80)], ["Ежедневная", "Постельное бельё", "Быстрая 30 мин"])
ents = {"select": w90.create_entities_select(), "button": w90.create_entities_button()}
mac = HW90["device_id"]
check("control on: a program to start and four buttons",
      {k: [(e._attr_unique_id.removeprefix(f"{mac}_HW90B14367U_"), e._attr_name.removeprefix("Washing Machine HW90-B14367U1 ")) for e in v]
       for k, v in ents.items()},
      {"select": [("laundry_program", "Программа для запуска")],
       "button": [("laundry_start", "Старт"), ("laundry_pause", "Пауза"), ("laundry_resume", "Продолжить"), ("laundry_cancel", "Отменить программу")]})
check("the program select: the programs' titles, the suggested one chosen",
      (ents["select"][0]._attr_options[:3], ents["select"][0].current_option), (["Белые вещи", "Деликатная", "Кашемир"], "Постельное бельё"))

sent = clients[-2].sent  # w90's client
w90.set_selected_program("Деликатная")
check("choosing a program sends nothing", (w90.selected_program, sent), ("Деликатная", []))
check("a program that is not offered cannot be chosen", (raised(lambda: w90.set_selected_program("Нет такой")), w90.selected_program),
      ("laundry_unknown_program", "Деликатная"))

remote(w90, False)
program_state(w90, "FINISHED")
check("remote control off on the machine: nothing starts, nothing is sent", (raised(w90.start), sent), ("laundry_remote_off", []))
remote(w90, True)
program_state(w90, "IN_PROGRESS")
check("a program running: no second start", (raised(w90.start), raised(w90.resume), sent), ("laundry_cannot_start", "laundry_cannot_resume", []))
w90.pause()
check("pause while running: the runningMode command", sent[-1:],
      [{"action": "command", "macAddress": mac, "command": {"commandName": "19", "value": "2"}}])
program_state(w90, "PAUSED")
check("paused: no pause, resume goes", (raised(w90.pause), raised(w90.resume)), ("laundry_cannot_pause", None))
check("resume: runningMode 1", sent[-1]["command"], {"commandName": "19", "value": "1"})
w90.cancel()
check("cancel while paused: returnStandby (144 on HW90)", sent[-1]["command"], {"commandName": "144", "value": "1"})
program_state(w90, "CANCELED")
check("cancelled: nothing to cancel or pause", (raised(w90.cancel), raised(w90.pause)), ("laundry_cannot_cancel", "laundry_cannot_pause"))
sent.clear()
w90.start()
chosen = next(p for p in w90.programs if p["title"] == "Деликатная")
check("start: the chosen program's group command with all its values, as one message",
      sent, [{"action": "operation", "macAddress": mac, "commandName": "8", "commands": chosen["commands"]}])
w90._control_lock.acquire()
check("a second press while a command is on its way is refused", raised(w90.start), "laundry_busy")
w90._control_lock.release()
w90.selected_program = None
check("no program chosen: nothing to start", raised(w90.start), "laundry_no_program")
w90._haier.laundry_control = False
check("the option turned off: no command even when called", raised(w90.cancel), "laundry_control_off")

remote(d80, True)
program_state(d80, "IN_PROGRESS")
d80.pause()
check("a dryer: pause and cancel with its own codes", clients[-1].sent[-1]["command"], {"commandName": "5", "value": "2"})
check("a machine that does not say whether remote control is on: refused",
      raised(make_ctl(HW60, data={k: v for k, v in HW60["backend_data"].items() if k != "remoteControlMode"}).start), "laundry_remote_off")

picked = copy.deepcopy(HW60["backend_data"])
first = picked["allProgram"]["blocks"][0]["programs"][0]
temperature = next(f for f in business["HW60"][first["programConfig"]["link"]["name"]]["attrNameList"] if str(f["name"]) == "48")
allowed = [o["data"] for o in temperature["valueRange"]["list"]["data"]]
first["programConfig"]["selectedValues"] = [{"attrName": "48", "attrValue": allowed[-1]}, {"attrName": "61", "attrValue": "99999"}]
m = make_ctl(HW60, data=picked)
values = {c["commandName"]: c["value"] for c in m.programs[0]["commands"]}
check("the program's selected values go instead of the defaults; a value the definition does not allow does not",
      (values["48"], values["61"]), (allowed[-1], next(str(f["defaultValue"]) for f in business["HW60"][first["programConfig"]["link"]["name"]]["attrNameList"] if str(f["name"]) == "61")))
check("broken descriptions give no programs and no actions, no error",
      [(api.laundry_programs(x), api.laundry_actions(x)) for x in (None, {}, {"allProgram": "x"}, {"controlBlock": {"pause": {"link": "19"}}},
                                                                    {"smartDeviceControl": {"businessAttributes": [None, {"name": 5}]}, "allProgram": {"blocks": [{"programs": [None, {"preview": "x"}]}]}})],
      [([], {})] * 5)
check("the diagnostics show the control: on, programs, actions, the chosen program",
      {k: (len(v) if isinstance(v, list) else v) for k, v in w60.to_dict()["control"].items()},
      {"enabled": True, "programs": 28, "actions": w60.actions, "selected_program": "Ежедневная"})

print("FAILS:", fails)
sys.exit(1 if fails else 0)
