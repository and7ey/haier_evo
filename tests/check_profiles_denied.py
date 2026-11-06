"""Model profiles are not copied to /config any more; a device the cloud refuses
(HTTP 403/404) is skipped instead of stopping the start (1.5.3)."""
import sys, json, os, copy, tempfile
from pathlib import Path
_HERE = Path(__file__).resolve().parent
root = sys.argv[1] if len(sys.argv) > 1 else str(_HERE.parent)
dump_path = sys.argv[2] if len(sys.argv) > 2 else str(_HERE / "fixtures" / "evo_dump.json")
sys.path.insert(0, root)
import requests
from custom_components.haier_evo import api
from custom_components.haier_evo import config as CFG
dump = json.load(open(dump_path))

fails = 0
def check(name, got, want=True):
    global fails
    ok = got == want
    fails += not ok
    print(("OK  " if ok else "FAIL"), name, "" if ok else f"\n      got:  {got!r}\n      want: {want!r}")

BUNDLED = os.path.join(os.path.dirname(CFG.config_dir.__file__), "AS25S2SF2FA.yaml")

# ---------- profiles ----------
def profile_path(userpath, model="AS25S2SF2FA"):
    cfg = CFG.HaierDeviceConfig.__new__(CFG.HaierDeviceConfig)
    cfg._model, cfg._userpath = model, userpath
    return cfg._find_config_file()

tmp = tempfile.mkdtemp()
user = os.path.join(tmp, "haier_evo")
user_file = f"{user}.AS25S2SF2FA.yaml"

check("no user profile: the bundled one", profile_path(user), BUNDLED)
check("... and no copy is made", os.path.exists(user_file), False)

with open(BUNDLED, "rb") as src, open(user_file, "wb") as dst:
    dst.write(src.read())
check("a copy identical to the bundled profile: the bundled one is used", profile_path(user), BUNDLED)
check("... and the copy is removed", os.path.exists(user_file), False)

with open(user_file, "w") as f:
    f.write("attributes: []\n")
check("a profile of the user's own: it wins", profile_path(user), user_file)
check("... and stays", os.path.exists(user_file), True)
os.remove(user_file)

check("no user path: the bundled one", profile_path(""), BUNDLED)
check("unknown model: the default profile, no copy",
      (os.path.basename(profile_path(user, "XX000")), os.path.exists(f"{user}.XX000.yaml")), ("default.yaml", False))

# a device built from the fixture leaves nothing in /config
class C:
    def path(self, x): return os.path.join(tmp, x)
class L:
    def call_soon_threadsafe(self, cb): pass
class H: config = C(); loop = L()
ac_src = next(d for d in dump["devices"] if not d["device_id"].startswith("5c:01"))
h = api.Haier(H(), "e@x", None, "ru")
api.HaierAC(haier=h, device_mac=ac_src["device_id"], device_serial="x", device_title="t",
            backend_data=copy.deepcopy(ac_src["backend_data"]))
check("building a device writes no profile to /config", sorted(os.listdir(tmp)), [])

# ---------- a device the cloud refuses ----------
def http_error(code):
    resp = requests.Response()
    resp.status_code = code
    return requests.exceptions.HTTPError(f"{code}", response=resp)

def kind(mac): return "WH" if mac.startswith("5c:01") else "AC"
BY_MAC = {d["device_id"]: d for d in dump["devices"]}
ALL = sorted(BY_MAC)

def start(answers):
    """pull_data over the fixture's device list; answers: MAC -> HTTP code to fail with."""
    haier = api.Haier(H(), "e@x", None, "ru")
    haier.pull_data_from_api = lambda: copy.deepcopy(dump["backend_data"])
    haier.connect_in_thread = lambda: None
    def pull(mac):
        if mac in answers:
            raise http_error(answers[mac])
        return copy.deepcopy(BY_MAC[mac]["backend_data"])
    haier.pull_device_data = pull
    try:
        haier.pull_data()
        return haier, None
    except Exception as e:
        return haier, e

victim = ALL[1]
for code in (403, 404):
    haier, err = start({victim: code})
    check(f"HTTP {code} for one device: the others start", (err, sorted(d.device_id for d in haier.devices)),
          (None, [m for m in ALL if m != victim]))
haier, err = start({victim: 500})
check("HTTP 500 is not a refusal: the start fails (HA retries it)", isinstance(err, requests.exceptions.HTTPError) and not api.Haier.is_denied(err), True)
haier, err = start({victim: 401})
check("HTTP 401 is not a refusal either", api.Haier.is_denied(err) if err else None, False)
check("is_denied: only HTTP errors with 403/404",
      [api.Haier.is_denied(e) for e in (http_error(403), http_error(404), http_error(500), ValueError("x"), requests.exceptions.HTTPError("no response"))],
      [True, True, False, False, False])

# pull_device_data: a refusal is not asked twice, a dropped token is
haier = api.Haier(H(), "e@x", None, "ru")
haier.auth = lambda: None
calls = []
def get(url, **kw):
    calls.append(url)
    resp = requests.Response()
    resp.status_code = answer[0]
    resp._content = b"{}"
    return resp
haier._session.get = get
for code, want in ((403, 1), (404, 1), (401, 2), (500, 2)):
    answer, calls = [code], []
    try:
        haier.pull_device_data(victim)
    except requests.exceptions.HTTPError:
        pass
    check(f"pull_device_data: HTTP {code} asked {want} time(s)", len(calls), want)

# denied_devices: which of the new MACs the cloud refuses
haier = api.Haier(H(), "e@x", None, "ru")
def pull(mac):
    if mac == "02:00:00:00:00:01": raise http_error(403)
    if mac == "02:00:00:00:00:02": raise requests.exceptions.ConnectTimeout("net")
    return {}
haier.pull_device_data = pull
check("denied_devices: refused only, not network errors or answering devices",
      haier.denied_devices({"02:00:00:00:00:01", "02:00:00:00:00:02", "02:00:00:00:00:03"}), {"02:00:00:00:00:01"})
def reauth(mac): raise api.ReauthRequired("x")
haier.pull_device_data = reauth
try:
    haier.denied_devices({"02:00:00:00:00:01"}); err = None
except Exception as e:
    err = e
check("denied_devices: a rejected sign-in is passed on", isinstance(err, api.ReauthRequired), True)

print("FAILS:", fails)
sys.exit(1 if fails else 0)
