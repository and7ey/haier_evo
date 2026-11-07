"""Tokens, push parsing, outdoor unit, deferred setpoint, stale snapshots, availability (1.6.0)."""
import sys, json, os, copy, tempfile, time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock
from pathlib import Path
_HERE = Path(__file__).resolve().parent
root = sys.argv[1] if len(sys.argv) > 1 else str(_HERE.parent)
dump_path = sys.argv[2] if len(sys.argv) > 2 else str(_HERE / "fixtures" / "evo_dump.json")
sys.path.insert(0, root)
import requests
from homeassistant.const import EntityCategory
from custom_components.haier_evo import api, climate, sensor, water_heater
from custom_components.haier_evo import const as C
dump = json.load(open(dump_path))

fails = 0
def check(name, got, want=True):
    global fails
    ok = got == want
    fails += not ok
    print(("OK  " if ok else "FAIL"), name, "" if ok else f"\n      got:  {got!r}\n      want: {want!r}")

tmp = tempfile.mkdtemp()
class Cfg:
    def path(self, x): return os.path.join(tmp, x)
class L:
    def __init__(s): s.calls = []
    def call_soon_threadsafe(s, cb, *a): s.calls.append(cb)
class H:
    def __init__(s): s.config = Cfg(); s.loop = L()
class Hr:
    def __init__(s): s.hass = H(); s.sent = []
    def send_command(s, m): s.sent.append(copy.deepcopy(m))

def response(code, body=None):
    resp = requests.Response()
    resp.status_code = code
    resp._content = json.dumps(body if body is not None else {}).encode()
    return resp

ac_src = next(d for d in dump["devices"] if not d["device_id"].startswith("5c:01"))
wh_src = next(d for d in dump["devices"] if d["device_id"].startswith("5c:01"))
def make_ac(haier=None, **values):
    data = copy.deepcopy(ac_src["backend_data"])
    for attr in data["attributes"]:
        if attr["name"] in values:
            attr["currentValue"] = values[attr["name"]]
    haier = haier or Hr()
    return api.HaierAC(haier=haier, device_mac=ac_src["device_id"], device_serial="x", device_title="t", backend_data=data), haier

def push(dev, *statuses):
    dev.on_message({"event": "status", "macAddress": dev.device_id, "payload": {"statuses": list(statuses)}})

# ---------- token expiry ----------
pe = api.parse_expire
check("expiry with +03:00", pe("2026-11-07T04:00:00+03:00"), datetime(2026, 11, 7, 1, 0, tzinfo=timezone.utc))
check("expiry with +0300", pe("2026-11-07T04:00:00+0300"), datetime(2026, 11, 7, 1, 0, tzinfo=timezone.utc))
check("expiry without a zone is Moscow time", pe("2026-11-07T04:00:00"), datetime(2026, 11, 7, 1, 0, tzinfo=timezone.utc))
check("unreadable / missing expiry -> None", [pe(v) for v in ("soon", "", None, 5)], [None] * 4)

ok_body = {"data": {"token": {"accessToken": "A2", "expire": "2099-01-01T00:00:00+03:00",
                              "refreshToken": "R2", "refreshExpire": "2099-02-01T00:00:00+03:00"}}}
bare = {"data": {"token": {"accessToken": "A3"}}}
r = api.AuthResponse(response(200, bare))
left = r.access_expire - datetime.now(timezone.utc)
check("no expiry in the answer: valid for about an hour, not refreshed on every request",
      timedelta(minutes=59) < left <= C.TOKEN_FALLBACK_TTL, True)
check("no refresh token in the answer -> None, no assertion", (r.refresh_token, r.refresh_expire), (None, None))

h = api.Haier(H(), "e@x", None, "ru", tokens={"token": "A1", "refreshtoken": "R1"})
h.auth_refresh = lambda: api.AuthResponse(response(200, bare))
h.login(refresh=True)
check("a refresh answer without a new refresh token keeps the old one", (h.token, h.refreshtoken), ("A3", "R1"))

# ---------- renewal margin ----------
def haier_with(expire_in):
    h = api.Haier(H(), "e@x", None, "ru")
    h.token, h.refreshtoken = "A1", "R1"
    h.tokenexpire = datetime.now(timezone.utc) + expire_in
    h.refreshes = []
    def login(refresh=False):
        h.refreshes.append(refresh)
        h.token, h.tokenexpire, h._token_issued = "A2", datetime.now(timezone.utc) + timedelta(hours=1), time.monotonic()
    h.login = login
    return h
h = haier_with(timedelta(seconds=30)); h.auth()
check("a token expiring in 30 s is renewed in advance", (h.refreshes, h.token), ([True], "A2"))
h = haier_with(timedelta(minutes=5)); h.auth()
check("a token valid for 5 min is used as is", (h.refreshes, h.token), ([], "A1"))

# ---------- a refused token ----------
h = haier_with(timedelta(hours=1))
check("refusal of an older token keeps the current one", (h.token_rejected("A0"), h.tokenexpire is not None), (False, True))
h._token_issued = time.monotonic()
check("refusal of a token issued seconds ago: retry it as is", (h.token_rejected("A1"), h.tokenexpire is not None), (True, True))
h._token_issued = time.monotonic() - C.TOKEN_FRESH - 1
check("refusal of an older current token: renewed on the next use", (h.token_rejected("A1"), h.tokenexpire), (False, None))

def pull_with(answers, issued_ago):
    h = haier_with(timedelta(hours=1))
    h._token_issued = time.monotonic() - issued_ago
    seen = []
    def get(url, headers=None, **kw):
        seen.append(headers["X-Auth-Token"])
        return response(answers.pop(0))
    h._session.get = get
    with mock.patch.object(api.time, "sleep") as sleep:
        try:
            h.pull_device_data(ac_src["device_id"]); err = None
        except Exception as e:
            err = e
    return h, seen, sleep.call_count, err
h, seen, sleeps, err = pull_with([401, 200], issued_ago=1)
check("401 for a token just issued: same token again after a pause, no renewal",
      (seen, sleeps, h.refreshes, err), (["A1", "A1"], 1, [], None))
h, seen, sleeps, err = pull_with([401, 200], issued_ago=600)
check("401 for an older token: renewed, the new one used", (seen, sleeps, h.refreshes, err), (["A1", "A2"], 0, [True], None))

# ---------- Device-Id ----------
h = api.Haier(H(), "e@x", None, "ru", device_id="dev-1")
check("Device-Id on every request, the sign-in included", h._session.headers.get("Device-Id"), "dev-1")
check("entry Device-Id: stable, one per entry",
      (api.entry_device_id("E1") == api.entry_device_id("E1"), api.entry_device_id("E1") != api.entry_device_id("E2")), (True, True))
check("without an entry: a random one", bool(api.Haier(H(), "e@x", None, "ru")._session.headers.get("Device-Id")), True)

# ---------- push parsing ----------
ac, _ = make_ac()
CUR, TGT, POWER = str(ac.config["current_temperature"]), str(ac.config["target_temperature"]), str(ac.config["status"])
push(ac, {"properties": {CUR: "21"}}, {"properties": {TGT: "25"}})
check("every status entry applies, not only the first", (ac.current_temperature, ac.target_temperature), (21.0, 25.0))
push(ac, {"properties": {CUR: "n/a", TGT: "26", POWER: "true"}})
check("an unreadable value is skipped, the rest of the message applies", (ac.current_temperature, ac.target_temperature, ac.status), (21.0, 26.0, 1))
push(ac, {"properties": {POWER: "false"}})
check("power as true/false", ac.status, 0)
ac.on_message({"event": "status", "macAddress": ac.device_id, "payload": None})
check("a message without statuses changes nothing", ac.target_temperature, 26.0)

h = api.Haier(H(), "e@x", None, "ru")
h.devices.append(SimpleNamespace(device_id="30:C9:22:00:00:03"))
check("MAC lookup ignores case", h.get_device_by_id("30:c9:22:00:00:03") is h.devices[0], True)

# ---------- unknown values ----------
ac, haier = make_ac(**{POWER: "1"})
light = ac.config.get_attr_by_name("light")
check("unknown value name -> None, not 'None'", (light.get_item_code("dim"), light.get_item_code("dim", "0")), (None, "0"))
check("a command for an unknown value is not sent", ac.get_commands("light", "dim"), [])
status = ac.config.get_attr_by_name("status")
saved = status["list"]
status["list"] = {"data": []}
check("power without a value list still goes out as 1/0",
      (api.HaierDevice.get_commands(ac, "status", "on")[0]["value"], api.HaierDevice.get_commands(ac, "status", "off")[0]["value"]), ("1", "0"))
status["list"] = saved

# ---------- outdoor unit ----------
ac, _ = make_ac(**{"38": "7"})
raw = {a["name"]: a.get("currentValue") for a in ac_src["backend_data"]["attributes"]}
check("code 38 is the outdoor unit, 36 stays the room", (ac.config["outdoor_temperature"], ac.config["current_temperature"]), ("38", "36"))
check("values: room from 36, outdoor unit from 38", (ac.current_temperature, ac.outdoor_temperature), (float(raw["36"]), 7.0))
push(ac, {"properties": {"38": "-3"}})
check("outdoor unit follows the push", ac.outdoor_temperature, -3.0)
ents = [e for e in ac.create_entities_sensor() if isinstance(e, sensor.HaierACOutdoorTemperatureSensor)]
check("diagnostic sensor 'Температура наружного блока'",
      [(e.native_value, e.entity_category, e.name.endswith("Температура наружного блока"), e.unique_id.endswith("_outdoor_temperature")) for e in ents],
      [(-3.0, EntityCategory.DIAGNOSTIC, True, True)])
data = copy.deepcopy(ac_src["backend_data"]); data["sensors"] = {"items": []}
no_sensors = api.HaierAC(haier=Hr(), device_mac="02:00:00:00:00:01", device_serial="x", device_title="t", backend_data=data)
check("no sensors block: the outdoor unit is not guessed", no_sensors.config["outdoor_temperature"], None)

# ---------- deferred setpoint ----------
ac, haier = make_ac(**{POWER: "0", TGT: "22"})
ac.set_temperature(24)
check("off: setpoint kept and shown", (ac._deferred_target, ac.target_temperature, haier.sent), (24, 24, []))
ac.update_from_backend(copy.deepcopy(ac.status_data))
check("a REST snapshot with the cloud's old setpoint does not hide it", (ac._deferred_target, ac.target_temperature), (24, 24))
push(ac, {"properties": {POWER: "1"}})
check("turned on with the remote: the kept one is dropped, the unit's own shown", (ac._deferred_target, ac.target_temperature), (None, 22.0))
ac, haier = make_ac(**{POWER: "0", TGT: "22"})
ac.set_temperature(24)
ac.switch_on("cool")
push(ac, {"properties": {POWER: "1", TGT: "24"}})
check("turned on from HA: the kept setpoint went out with it and stays", (str(haier.sent[0]["commands"]).count("'24'") >= 1, ac.target_temperature), (True, 24.0))

# ---------- stale snapshots ----------
ac, _ = make_ac(**{CUR: "20"})
snap = copy.deepcopy(ac.status_data)
before = time.monotonic()
push(ac, {"properties": {CUR: "23"}})
check("a snapshot requested before the last event is stale", ac.is_stale(before), True)
ac.apply_fresh(copy.deepcopy(snap), read_at=before)
check("... and does not overwrite the newer value", ac.current_temperature, 23.0)
ac.apply_fresh(copy.deepcopy(snap), read_at=time.monotonic())
check("a snapshot requested after it applies", ac.current_temperature, 20.0)
ac.synced = False
push(ac, {"properties": {CUR: "23"}})
ac.apply_fresh(copy.deepcopy(snap), read_at=before)
check("a device not read yet takes even an older snapshot", (ac.current_temperature, ac.synced), (20.0, True))

class Pulling(Hr):
    def __init__(s, dev_ref): super().__init__(); s.ref = dev_ref
    def pull_device_data(s, mac):
        push(s.ref[0], {"properties": {CUR: "23"}})  # an event while the request is in flight
        return copy.deepcopy(snap)
ref = []
haier = Pulling(ref)
ac, _ = make_ac(haier=haier)
ref.append(ac)
ac.resync(guard=True)
check("resync after a reconnect skips a snapshot older than an event", ac.current_temperature, 23.0)
ac.resync()
check("resync after a lost command always applies", ac.current_temperature, 20.0)

ac, haier = make_ac(**{POWER: "1"})
t0 = time.monotonic(); ac.set_light_on(False)
check("a command counts as a change", ac.changed_at >= t0, True)

# ---------- availability without the websocket ----------
h = api.Haier(H(), "e@x", None, "ru")
ac, _ = make_ac(haier=h)
check("at the start: available", (h.cloud_available, ac.available), (True, True))
h._offline_since = time.monotonic() - C.WS_UNAVAILABLE_AFTER - 1
check("no websocket for 3 min: unavailable", (h.cloud_available, ac.available), (False, False))
h.devices.append(ac)
ac.add_write_ha_state_callback(lambda: None)
h._came_online()
check("connected again: available, the entities are told", (h.cloud_available, ac.available, len(h.hass.loop.calls) >= 1), (True, True, True))
with mock.patch.object(api.C, "WS_UNAVAILABLE_AFTER", 0):
    n = len(h.hass.loop.calls)
    h._went_offline()
    armed = h._offline_timer is not None
    time.sleep(1.3)
check("lost: a timer tells the entities when they go unavailable", (armed, len(h.hass.loop.calls) > n, h._offline_timer), (True, True, None))
h._went_offline(); h.stop()
check("stop cancels the timer", h._offline_timer, None)

# ---------- Home Assistant 2026.11 names ----------
ac, _ = make_ac(**{CUR: "21", TGT: "24"})
ent = climate.HaierACEntity(ac)
check("climate: native_* members alongside the old ones",
      (ent.native_current_temperature, ent.native_target_temperature, ent.native_temperature_unit),
      (ent.current_temperature, ent.target_temperature, ent.temperature_unit))
wh = api.HaierWH(haier=Hr(), device_mac=wh_src["device_id"], device_serial="x", device_title="t", backend_data=copy.deepcopy(wh_src["backend_data"]))
went = water_heater.HaierWHEntity(wh)
check("water heater: native_* members alongside the old ones",
      (went.native_current_temperature, went.native_target_temperature, went._attr_native_temperature_unit),
      (went.current_temperature, went.target_temperature, went._attr_temperature_unit))

print("FAILS:", fails)
sys.exit(1 if fails else 0)
