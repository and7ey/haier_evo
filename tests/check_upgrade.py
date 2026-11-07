"""Upgrading from any release since 1.0.0 keeps what users built on: every entity
an earlier release created for the fixture's devices still exists with the same
unique_id (automations, dashboards and history hang on it), or it was retired on
purpose and setup removes it from the entity registry; the devices keep their
identifiers.

fixtures/entities_by_release.json holds what every release created. After a
release, add it, with that release's code checked out at <dir>:
  python3 tests/check_upgrade.py <dir> --record vX.Y.Z
"""
import sys, json, os, copy, tempfile, inspect, logging
from pathlib import Path
_HERE = Path(__file__).resolve().parent
args = [a for a in sys.argv[1:] if not a.startswith("--")]
root = args[0] if args else str(_HERE.parent)
sys.path.insert(0, root)
logging.getLogger("custom_components.haier_evo").setLevel(logging.CRITICAL)
from custom_components.haier_evo import api

FIXTURE = _HERE / "fixtures" / "entities_by_release.json"
dump = json.loads((_HERE / "fixtures" / "evo_dump.json").read_text(encoding="utf-8"))
# entities retired on purpose, by unique_id suffix: platform and how setup removes them
RETIRED = {
    "sound": ("switch", "hidden_entities"),                 # the beep switch, gone in 1.5.0
    "outdoor_temperature": ("sensor", "RETIRED_SENSORS"),  # the outdoor unit of 1.6.0
}

tmp = tempfile.mkdtemp()
class Conf:
    def path(self, *x): return os.path.join(tmp, *x)
class Loop:
    def call_soon_threadsafe(self, cb, *a): pass
class Hass:
    config = Conf(); loop = Loop(); data = {}
    def async_create_task(self, *a, **k): pass
class FakeHaier:
    def __init__(self): self.hass = Hass(); self.devices = []
    def send_message(self, p): pass
    def send_command(self, m): pass
    def get_device_by_id(self, mac): return None
    def pull_device_data(self, mac): raise RuntimeError("no cloud")

clients = []  # a device holds its client by a weak reference
def devices():
    """The fixture's devices as the code at root builds them; works with every release since 1.0.0."""
    out = []
    for dev in dump["devices"]:
        clients.append(FakeHaier())
        cls = api.HaierWH if dev["device_id"].startswith("5c:01") else api.HaierAC
        params = inspect.signature(cls.__init__).parameters
        kwargs = dict(haier=clients[-1], device_mac=dev["device_id"], device_serial="x", device_title=dev["device_name"])
        if "backend_data" in params or any(p.kind == p.VAR_KEYWORD for p in params.values()):
            kwargs["backend_data"] = copy.deepcopy(dev["backend_data"])
        out.append(cls(**kwargs))
    return out

def entities_of(built):
    entities = []
    for device in built:
        for platform in ("climate", "water_heater", "switch", "select", "sensor", "binary_sensor"):
            make = getattr(device, f"create_entities_{platform}", None)
            for entity in (make() if make else []) or []:
                entities.append([platform, getattr(entity, "_attr_unique_id", None) or getattr(entity, "unique_id", None)])
    return sorted(entities)

def identifiers_of(built):
    return [sorted(list(i) for i in d.device_info["identifiers"]) for d in built]

built = devices()
now = {"entities": entities_of(built), "devices": identifiers_of(built)}

if "--record" in sys.argv:
    tag = sys.argv[sys.argv.index("--record") + 1]
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    data[tag] = now
    lines = ["{"]
    for n, (key, value) in enumerate(data.items()):
        end = "," if n < len(data) - 1 else ""
        if key.startswith("_"):
            lines.append(f" {json.dumps(key)}: {json.dumps(value, ensure_ascii=False)}{end}")
            continue
        entities = value["entities"]
        lines += [f' "{key}": {{', '  "devices": ' + json.dumps(value["devices"]) + ",", '  "entities": [']
        lines += ["   " + json.dumps(e) + ("," if i < len(entities) - 1 else "") for i, e in enumerate(entities)]
        lines += ["  ]", " }" + end]
    FIXTURE.write_text("\n".join(lines + ["}"]) + "\n", encoding="utf-8")
    print(f"recorded {tag}: {len(now['entities'])} entities")
    sys.exit(0)

fails = 0
def check(name, got, want=True):
    global fails
    ok = got == want
    fails += not ok
    print(("OK  " if ok else "FAIL"), name, "" if ok else f"\n      got:  {got!r}\n      want: {want!r}")

releases = {k: v for k, v in json.loads(FIXTURE.read_text(encoding="utf-8")).items() if not k.startswith("_")}
check("every release since 1.0.0 is recorded, each with its entities",
      ("v1.0.0" in releases, len(releases) >= 13, all(r["entities"] for r in releases.values())), (True, True, True))

current = {tuple(e) for e in now["entities"]}
check("no unique_id twice", len(current), len(now["entities"]))
check("every unique_id is set", all(uid for _, uid in current), True)
check("the devices keep their identifiers since every release",
      sorted(t for t, r in releases.items() if r["devices"] != now["devices"]), [])

def suffix(device, uid):
    prefix = f"{device.device_id}_{device.device_model}_"
    return uid[len(prefix):] if uid.startswith(prefix) else None

by_id = {d.device_id: d for d in built}
gone, unexplained = {}, []
for tag, release in releases.items():
    for platform, uid in release["entities"]:
        if (platform, uid) in current:
            continue
        device = by_id.get(uid.split("_", 1)[0])
        name = suffix(device, uid) if device else None
        if name in RETIRED and RETIRED[name][0] == platform:
            gone.setdefault((platform, uid), tag)
        else:
            unexplained.append(f"{tag}: {platform} {uid}")
check("every entity of an earlier release still exists, or was retired on purpose",
      sorted(set(unexplained)), [])

from custom_components.haier_evo import sensor  # noqa: E402 - after the path is set
hidden = {tuple(h) for d in built for h in getattr(d, "hidden_entities", [])}
not_removed = []
for (platform, uid), tag in sorted(gone.items()):
    how = RETIRED[suffix(by_id[uid.split("_", 1)[0]], uid)][1]
    if how == "hidden_entities" and (platform, uid) not in hidden:
        not_removed.append(f"{uid} ({tag}) is not in hidden_entities")
    if how == "RETIRED_SENSORS" and suffix(by_id[uid.split("_", 1)[0]], uid) not in sensor.RETIRED_SENSORS:
        not_removed.append(f"{uid} ({tag}) is not in sensor.RETIRED_SENSORS")
check("every retired entity is removed from the registry at setup", not_removed, [])
check("... the retired ones were found at all (the check looks at something)",
      sorted({suffix(by_id[uid.split('_', 1)[0]], uid) for _, uid in gone}), sorted(RETIRED))

print("FAILS:", fails)
sys.exit(1 if fails else 0)
