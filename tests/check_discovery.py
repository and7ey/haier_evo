"""The app's device list: found by component name, not by the layout UUID (1.5.1)."""
import sys, json, copy
from pathlib import Path
_HERE = Path(__file__).resolve().parent
root = sys.argv[1] if len(sys.argv) > 1 else str(_HERE.parent)
dump_path = sys.argv[2] if len(sys.argv) > 2 else str(_HERE / "fixtures" / "evo_dump.json")
sys.path.insert(0, root)
from custom_components.haier_evo import api
dump = json.load(open(dump_path))

fails = 0
def check(name, got, want=True):
    global fails
    ok = got == want
    fails += not ok
    print(("OK  " if ok else "FAIL"), name, "" if ok else f"\n      got:  {got!r}\n      want: {want!r}")

page = dump["backend_data"]
parse = api.Haier._parse_device_list
MACS = sorted(d["device_id"] for d in dump["devices"])

def macs(listed):
    return sorted(d["device_mac"] for d in listed)

def containers(p):
    return p["presentation"]["layout"]["scrollContainer"]

def device_list(p):
    return next(c for c in containers(p) if c["trackingData"]["component"]["componentName"] == "deviceList")

# 1. the page as the cloud sends it
listed = parse(copy.deepcopy(page))
check("all devices of the fixture", macs(listed), MACS)
check("type, serial and title of each", sorted((d["device_type"], d["device_title"]) for d in listed),
      sorted(("WH" if d["device_id"].startswith("5c:01") else "AC", d["device_name"]) for d in dump["devices"]))
check("serials", all(d["device_serial"].startswith("SN") for d in listed))

# 2. the cloud's answer is not changed by parsing
before = json.dumps(page, sort_keys=True)
parse(page)
check("the page is not modified", json.dumps(page, sort_keys=True), before)

# 3. a new layout UUID does not hide the devices
p = copy.deepcopy(page)
device_list(p)["trackingData"]["component"]["componentId"] = "00000000-0000-4000-8000-000000000000"
check("changed layout UUID: devices still found", macs(parse(p)), MACS)

# 4. several device list blocks: all count, duplicates once
p = copy.deepcopy(page)
extra = copy.deepcopy(device_list(p))
state = json.loads(extra["state"]) if isinstance(extra["state"], str) else extra["state"]
state["items"] = state["items"][:1] + [dict(state["items"][0], action={"link": "haierevo://x?type=AC&deviceId=02%3A00%3A00%3A00%3A00%3A09&serialNum=SNX"}, title="Ещё")]
extra["state"] = json.dumps(state, ensure_ascii=False)
containers(p).append(extra)
got = parse(p)
check("two blocks: devices of both, a repeated one once", macs(got), sorted(MACS + ["02:00:00:00:00:09"]))

# 5. a block that cannot be read is skipped, the others still count
p = copy.deepcopy(page)
broken = copy.deepcopy(device_list(p)); broken["state"] = "{not json"
containers(p).insert(0, broken)
check("unreadable block skipped", macs(parse(p)), MACS)
p = copy.deepcopy(page)
device_list(p)["state"] = json.dumps({"items": "x"})
check("items not a list -> nothing", parse(p), [])

# 6. empty answers
p = copy.deepcopy(page)
dl = device_list(p); st = json.loads(dl["state"]) if isinstance(dl["state"], str) else dl["state"]
st["items"] = []; dl["state"] = json.dumps(st)
check("header only (no items) -> nothing", parse(p), [])
p = copy.deepcopy(page)
containers(p)[:] = [c for c in containers(p) if c["trackingData"]["component"]["componentName"] != "deviceList"]
check("no device list block -> nothing", parse(p), [])
check("garbage pages -> nothing", [parse(x) for x in ({}, {"presentation": "x"}, {"presentation": {"layout": {"scrollContainer": "x"}}},
                                                      {"presentation": {"layout": {"scrollContainer": [None, 1, {"trackingData": "x"}]}}})], [[]] * 4)

# 7. a device whose id is not a MAC is skipped
p = copy.deepcopy(page)
dl = device_list(p); st = json.loads(dl["state"]) if isinstance(dl["state"], str) else dl["state"]
st["items"].append(dict(st["items"][0], action={"link": "haierevo://x?type=AC&deviceId=..%2F..%2Fetc"}))
dl["state"] = json.dumps(st)
check("non-MAC device id skipped", macs(parse(p)), MACS)

# 8. the background refresh sees the same list
h = api.Haier.__new__(api.Haier)
p = copy.deepcopy(page)
device_list(p)["trackingData"]["component"]["componentId"] = "11111111-1111-4111-8111-111111111111"
h.pull_data_from_api = lambda: p
check("list_device_ids with a new UUID", h.list_device_ids(), {m.lower() for m in MACS})

print("FAILS:", fails)
sys.exit(1 if fails else 0)
