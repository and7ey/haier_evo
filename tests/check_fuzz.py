"""Property-based checks (hypothesis): what the cloud sends, however broken, never
crashes the integration and never puts a broken value into Home Assistant.

Push messages, device descriptions (the fixture with random parts removed or
replaced), the device list page, websocket text, token expiries and louver
screens. The runs are derandomized: the same examples on every run, so CI
does not depend on luck; a failure prints the example that broke it.
"""
import sys, json, math, copy, os, tempfile, logging, traceback, warnings
from datetime import datetime
from pathlib import Path
_HERE = Path(__file__).resolve().parent
root = sys.argv[1] if len(sys.argv) > 1 else str(_HERE.parent)
sys.path.insert(0, root)
from hypothesis import given, settings, strategies as st, HealthCheck
from custom_components.haier_evo import api, config as CFG

logging.getLogger("custom_components.haier_evo").setLevel(logging.CRITICAL)
warnings.filterwarnings("ignore", module="hypothesis")
dump = json.loads((_HERE / "fixtures" / "evo_dump.json").read_text(encoding="utf-8"))
ACS = [d for d in dump["devices"] if not d["device_id"].startswith("5c:01")]
WH = next(d for d in dump["devices"] if d["device_id"].startswith("5c:01"))
EXAMPLES = int(os.environ.get("FUZZ_EXAMPLES", "300"))
SETTINGS = settings(max_examples=EXAMPLES, derandomize=True, deadline=None, database=None, print_blob=False,
                    suppress_health_check=list(HealthCheck))

fails = 0
def check(name, got, want=True):
    global fails
    ok = got == want
    fails += not ok
    print(("OK  " if ok else "FAIL"), name, "" if ok else f"\n      got:  {got!r}\n      want: {want!r}")

def _where(e):
    """The last frame of the integration in the traceback: file:line function."""
    frames = traceback.extract_tb(e.__traceback__)
    ours = [f for f in frames if "custom_components" in f.filename] or frames[-1:]
    f = ours[-1]
    return f"{Path(f.filename).name}:{f.lineno} {f.name}"

def holds(test):
    """None when the property held for every example, else what broke it: one
    line per distinct failure, with the place and the (shrunk) example."""
    try:
        test()
    except Exception as e:  # noqa: BLE001 - hypothesis re-raises the falsifying example's error
        found = list(e.exceptions) if isinstance(e, BaseExceptionGroup) else [e]
        out = []
        for one in found:
            example = " ".join(n for n in getattr(one, "__notes__", []) if not n.startswith(("Falsifying", "You can")))
            out.append(f"{type(one).__name__} at {_where(one)}: {str(one)[:160]} | {example[:400]}")
        return sorted(out)
    return None

tmp = tempfile.mkdtemp()
class Conf:
    def path(self, x): return os.path.join(tmp, x)
class Loop:
    def call_soon_threadsafe(self, cb): pass
class Hass: config = Conf(); loop = Loop()
class FakeHaier:
    def __init__(self): self.hass = Hass(); self.sent = []
    def send_message(self, payload): self.sent.append(json.loads(payload))
    def send_command(self, message): self.sent.append(dict(message))
    def get_device_by_id(self, mac): return None

clients = []  # a device holds its client by a weak reference
def make(cls, dev, data=None):
    clients.append(FakeHaier())
    return cls(haier=clients[-1], device_mac=dev["device_id"], device_serial="x", device_title="t",
               backend_data=copy.deepcopy(dev["backend_data"]) if data is None else data)

# ---- values as the cloud might send them
scalars = (st.none() | st.booleans() | st.integers(-10**12, 10**12) | st.floats(allow_nan=True, allow_infinity=True)
           | st.text(max_size=12) | st.sampled_from(["true", "false", "1", "0", "23.5", " 24 ", "NaN", "inf", "-1e309", "", "None",
                                                     "ONLINE", "OFFLINE", "GOOD"]))
json_values = st.recursive(scalars, lambda c: st.lists(c, max_size=4) | st.dictionaries(st.text(max_size=8), c, max_size=4),
                           max_leaves=10)

def codes_of(dev):
    return sorted({str(a.get("name")) for a in dev["backend_data"].get("attributes", [])})
def alarm_names(dev):
    return ["alarmCancel", "upperTempSensorErr", "dryHeatingAlarm", "filterAlarm", "fooErr"]

def statuses_of(dev):
    codes = st.sampled_from(codes_of(dev)) | st.text(max_size=4)
    real_values = st.sampled_from([str(v) for v in range(0, 12)] + ["-1", "100", "35.5"])
    status = st.fixed_dictionaries({}, optional={
        "properties": st.dictionaries(codes, real_values | json_values, max_size=6) | json_values,
        "alarms": st.lists(st.sampled_from(alarm_names(dev)) | st.dictionaries(st.just("name"), st.text(max_size=8)) | json_values,
                           max_size=3) | json_values,
    })
    return st.lists(status | json_values, max_size=3) | json_values

def messages_of(dev):
    events = st.sampled_from(["status", "info", "deviceStatusEvent", "deviceConnectionStatusEvent", "command_response", "other"])
    payload = st.fixed_dictionaries({}, optional={"statuses": statuses_of(dev), "status": json_values, "swVersion": json_values})
    return st.fixed_dictionaries({"event": events | json_values, "macAddress": st.just(dev["device_id"]),
                                  "payload": payload | json_values})

def walk(value):
    if isinstance(value, dict):
        for v in value.values():
            yield from walk(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from walk(v)
    else:
        yield value

def names_of(device, attr):
    a = device.config.get_attr_by_name(attr)
    return {i.name for i in a.list} if a else set()

def state_is_sane(device, cloud_said_none=False):
    """What Home Assistant gets from the device. The string "None" is the code's
    own mistake (str(None)) unless the cloud itself sent that text."""
    problems = []
    state = device.to_dict()
    state.pop("backend_data", None)  # the cloud's own answer, kept as it came
    try:
        json.dumps(state, allow_nan=False)
    except (TypeError, ValueError) as e:
        problems.append(f"to_dict is not JSON: {e}")
    if not cloud_said_none and any(v == "None" for v in walk(state)):
        problems.append("the string 'None' in the state")
    if not isinstance(device._available, bool):
        problems.append(f"available={device._available!r}")
    for name in ("current_temperature", "target_temperature"):
        value = getattr(device, name, None)
        if value is not None and (not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value)):
            problems.append(f"{name}={value!r}")
    if isinstance(device, api.HaierAC):
        # the louvers may sit where the list does not reach (the IR remote):
        # the climate entity adds the current position to its list itself
        for attr in ("mode", "fan_mode", "eco_sensor"):
            value = getattr(device, attr)
            if value is not None and value not in names_of(device, attr):
                problems.append(f"{attr}={value!r}")
        for attr in ("swing_mode", "swing_horizontal_mode"):
            value = getattr(device, attr)
            if value is not None and not (isinstance(value, str) and value):
                problems.append(f"{attr}={value!r}")
    if isinstance(device, api.HaierWH):
        if device.heating_mode is not None and device.heating_mode not in names_of(device, "heating_mode"):
            problems.append(f"heating_mode={device.heating_mode!r}")
    return problems

# ---- push messages
def push_test(dev, cls):
    @SETTINGS
    @given(st.lists(messages_of(dev), min_size=1, max_size=4))
    def test(messages):
        device = make(cls, dev)
        device.synced = True
        for message in messages:
            device.on_message(message)
        said = '"None"' in json.dumps(messages)
        assert not state_is_sane(device, said), state_is_sane(device, said)
    return test
for dev in ACS[:2]:
    check(f"push messages to {dev['device_name']}: no crash, the state stays readable", holds(push_test(dev, api.HaierAC)), None)
check(f"push messages to {WH['device_name']}: no crash, the state stays readable", holds(push_test(WH, api.HaierWH)), None)

@SETTINGS
@given(st.text(max_size=200) | json_values.map(json.dumps)
       | messages_of(ACS[0]).map(json.dumps) | messages_of(ACS[0]).map(lambda m: json.dumps(dict(m, macAddress=m["macAddress"].upper()))))
def test_raw(text):
    h = api.Haier(None, "e", "p", "ru")
    h.devices = [make(api.HaierAC, ACS[0])]
    h._on_message(None, text)
check("websocket text of any kind: the client does not crash", holds(test_raw), None)

# ---- device descriptions with random parts removed or replaced
def paths(value, prefix=()):
    yield prefix
    if isinstance(value, dict):
        for k, v in value.items():
            yield from paths(v, prefix + (k,))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from paths(v, prefix + (i,))

def mutated(dev):
    every = [p for p in paths(dev["backend_data"]) if p]
    change = st.tuples(st.sampled_from(every), st.sampled_from(["delete", "replace"]), json_values)
    def apply(changes):
        data = copy.deepcopy(dev["backend_data"])
        for path, how, new in changes:
            node = data
            try:
                for key in path[:-1]:
                    node = node[key]
                if how == "delete":
                    del node[path[-1]]
                else:
                    node[path[-1]] = new
            except (KeyError, IndexError, TypeError):
                continue  # an earlier change removed it
        return data
    return st.lists(change, min_size=1, max_size=4).map(apply)

def description_test(dev, cls):
    """A description the code cannot read stops the start (Home Assistant
    retries it: ConfigEntryNotReady); a device built from one must be sane."""
    @SETTINGS
    @given(mutated(dev), messages_of(dev))
    def test(data, message):
        try:
            device = make(cls, dev, data)
        except Exception:  # noqa: BLE001 - not built: the start is retried
            return
        device.synced = True
        said = '"None"' in json.dumps(data)
        problems = state_is_sane(device, said)
        assert not problems, ("built", problems)
        if isinstance(device, api.HaierAC):
            for options in (device.get_hvac_modes(), device.get_fan_modes(), device.get_swing_modes(),
                            device.get_swing_horizontal_modes(), device.get_preset_modes(), device.get_eco_sensor_options()):
                assert all(isinstance(o, str) and o not in ("", "None", "unknown") for o in options), options
                assert len(options) == len(set(options)), ("an option twice", options)
        device.on_message(message)
        problems = state_is_sane(device, said or '"None"' in json.dumps(message))
        assert not problems, ("after a message", problems)
    return test
check(f"descriptions of {ACS[0]['device_name']} with broken parts: a device built from one is sane, takes messages",
      holds(description_test(ACS[0], api.HaierAC)), None)
check(f"descriptions of {WH['device_name']} with broken parts: a device built from one is sane, takes messages",
      holds(description_test(WH, api.HaierWH)), None)

# ---- the device list page
page = dump["backend_data"]
every_page_path = [p for p in paths(page) if p]
@SETTINGS
@given(json_values | st.lists(st.tuples(st.sampled_from(every_page_path), json_values), min_size=1, max_size=3))
def test_page(change):
    if isinstance(change, list) and change and isinstance(change[0], tuple):
        data = copy.deepcopy(page)
        for path, new in change:
            node = data
            try:
                for key in path[:-1]:
                    node = node[key]
                node[path[-1]] = new
            except (KeyError, IndexError, TypeError):
                continue
    else:
        data = change
    listed = api.Haier._parse_device_list(data)
    assert isinstance(listed, list)
    for d in listed:
        assert api.MAC_RE.match(d["device_mac"]), d
        assert isinstance(d["device_type"], str) and isinstance(d["device_title"], str | None), d
    assert len({d["device_mac"].lower() for d in listed}) == len(listed), "a device twice"
check("device list pages, broken or not: a list of valid devices, each once", holds(test_page), None)

# ---- small parsers
@SETTINGS
@given(json_values | st.datetimes().map(lambda d: d.isoformat()) | st.datetimes().map(lambda d: d.strftime("%Y-%m-%dT%H:%M:%S%z")))
def test_expire(value):
    parsed = api.parse_expire(value)
    assert parsed is None or (isinstance(parsed, datetime) and parsed.tzinfo is not None), parsed
check("token expiries of any form: an aware datetime or None", holds(test_expire), None)

modes = ACS[0]["backend_data"].get("modes")
@SETTINGS
@given(json_values | st.just(modes))
def test_screens(value):
    layout = CFG.louver_layout(value)
    assert isinstance(layout, dict) and all(isinstance(v, list) for v in layout.values())
    screens = CFG.app_screens(value if not isinstance(value, dict) else {"modes": value})
    assert isinstance(screens, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in screens.items())
check("louver screens and app screens of any form: dicts, no crash", holds(test_screens), None)

print("FAILS:", fails)
sys.exit(1 if fails else 0)
