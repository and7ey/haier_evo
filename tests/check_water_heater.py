import sys, json, os, copy, tempfile
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
class FakeHaier:
    def __init__(self): self.hass = FakeHass(); self.sent = []
    def send_message(self, payload): self.sent.append(json.loads(payload))
    def send_command(self, message): self.sent.append(dict(message))
    def get_device_by_id(self, mac): return None
    def pull_device_data(self, mac): return copy.deepcopy(by_mac(mac[:5])["backend_data"])

def by_mac(prefix):
    return [d for d in dump["devices"] if d["device_id"].startswith(prefix)][0]

def make(cls, prefix, model=None):
    h = FakeHaier()
    bd = copy.deepcopy(by_mac(prefix)["backend_data"])
    if model: bd["info"]["model"] = model
    d = cls(haier=h, device_mac=by_mac(prefix)["device_id"], device_serial="x", device_title="Бойлер", backend_data=bd)
    return h, d

def sent(h):
    out = [(m["command"]["commandName"], m["command"]["value"]) for m in h.sent]; h.sent.clear(); return out

def ws(d, props=None, alarms=None):
    st = {"properties": props or {}}
    if alarms is not None: st["alarms"] = alarms
    d.on_message({"event": "status", "macAddress": d.device_id, "payload": {"statuses": [st]}})

fails = 0
def check(name, got, exp):
    global fails
    ok = got == exp
    fails += not ok
    print(("OK  " if ok else "FAIL"), name, "->", got, "" if ok else f"(expected {exp})")

def locked_key(fn):
    try:
        fn(); return None
    except api.HaierCommandLocked as e:
        return e.translation_key

for model in ("ES80V-F7", "ES30V-F7"):   # with model yaml / description-only mapping (no ES30VF7.yaml)
    print("=== model", model)
    h, d = make(api.HaierWH, "5c:01", model)
    print(d.config)
    check("current", d.current_temperature, 69.0)
    check("target", d.target_temperature, 75.0)
    check("range", (d.min_temperature, d.max_temperature), (35.0, 75.0))
    check("status", d.status, True)
    check("mode", d.heating_mode, "performance")
    check("modes", d.get_heating_modes(), ["eco", "performance"])
    check("heating", d.heating, False)
    check("steril", d.sterilization_on, False)
    check("alarms", d.alarms, [])
    check("command_name", d.config.command_name, None if model == "ES80V-F7" else "")
    d.set_temperature(60.4); check("set_temp", sent(h), [("1", "60")]); check("target opt", d.target_temperature, 60.0)
    d.set_temperature(99); check("set_temp clamp", sent(h), [("1", "75")])
    d.set_heating_mode("eco"); check("mode eco", sent(h), [("41", "1")]); check("mode opt", d.heating_mode, "eco")
    d.set_heating_mode("eco"); check("mode same is re-sent", sent(h), [("41", "1")])
    ws(d, {"43": "2", "0": "55"}); check("ws heating", (d.heating, d.current_temperature), (True, 55.0))
    d.switch_off(); check("off", sent(h), [("35", "0")]); check("status opt", d.status, False)
    check("off locks temp", locked_key(lambda: d.set_temperature(50)), "locked_off")
    check("off locks steril", locked_key(lambda: d.set_sterilization_on(True)), "locked_off")
    d.set_heating_mode("performance"); check("mode while off -> on+mode", sent(h), [("35", "1"), ("41", "2")])
    d.update_from_backend({"status": "ONLINE", "attributes": [{"name": "1", "currentValue": "70"}], "alarms": ["upperTempSensorErr"]})
    check("resync values+alarms", (d.target_temperature, d.alarms), (70.0, ["upperTempSensorErr"]))
    d.update_from_backend({"status": "ONLINE", "attributes": [{"name": "1", "currentValue": "75"}], "alarms": []})
    d.switch_on(); check("on is re-sent when on", sent(h), [("35", "1")])
    d.set_sterilization_on(True); check("steril on", sent(h), [("38", "1")])
    ws(d, {"41": "0", "38": "1"}); check("mode kept during BPS", d.heating_mode, "performance")
    check("BPS locks temp", locked_key(lambda: d.set_temperature(50)), "locked_by")
    d.set_heating_mode("eco"); check("mode allowed during BPS", sent(h), [("41", "1")])
    ws(d, {"41": "2", "38": "0"}); check("BPS end", (d.heating_mode, d.sterilization_on), ("performance", False))
    ws(d, {}, ["dryHeatingAlarm"]); check("alarm", (d.problem, d.alarm_texts), (True, ["[H0] Датчик температуры воды зафиксировал перегрев."]))
    check("alarm locks temp", locked_key(lambda: d.set_temperature(50)), "locked_alarm")
    check("alarm locks power", locked_key(lambda: d.switch_off()), "locked_alarm")
    ws(d, {}, [{"name": "fooErr"}]); check("unknown alarm text", d.alarm_texts, ["Неизвестная ошибка (fooErr)"])
    ws(d, {}, ["alarmCancel"]); check("alarmCancel clears", d.problem, False)
    ws(d, {"35": "false"}); check("bool as text", d.status, False)
    check("to_dict json", bool(json.dumps(d.to_dict(), ensure_ascii=False)), True)
    from custom_components.haier_evo import water_heater, switch, sensor, binary_sensor  # noqa: F401 - the platforms import
    ents = d.create_entities_water_heater() + d.create_entities_switch() + d.create_entities_sensor() + d.create_entities_binary_sensor()
    print("entities:", [(type(e).__name__, e.unique_id, e.name) for e in ents])
    wh = ents[0]
    check("wh op list", wh.operation_list, ["off", "eco", "performance"])
    check("wh current op (off)", wh.current_operation, "off")
    ws(d, {"35": "1"}); check("wh current op", wh.current_operation, "performance")
    check("wh features", int(wh.supported_features), 1 | 2 | 8)

print("=== AC regression")
h, ac = make(api.HaierAC, "a0:a3")
print(json.dumps(ac.to_dict()["config"], ensure_ascii=False, sort_keys=True)[:300])
open(os.path.join(tmp, "ac.json"), "w").write(json.dumps({k: v for k, v in ac.to_dict().items() if k != "backend_data"}, ensure_ascii=False, sort_keys=True, default=str))
print("AC_DUMP", os.path.join(tmp, "ac.json"))
print("FAILS:", fails)
sys.exit(1 if fails else 0)
