"""1.2.0: rate limiter, token handling / login, diagnostics redaction, model sanitizing."""
import sys, json, time, threading, copy
from pathlib import Path
_HERE = Path(__file__).resolve().parent
root = sys.argv[1] if len(sys.argv) > 1 else str(_HERE.parent)
dump_path = sys.argv[2] if len(sys.argv) > 2 else str(_HERE / "fixtures" / "evo_dump.json")
dump = json.load(open(dump_path))
sys.path.insert(0, root)
from custom_components.haier_evo import api, limits, config as CFG
from custom_components.haier_evo import diagnostics
import requests

fails = 0
def check(name, cond, info=""):
    global fails
    fails += not cond
    print(("OK  " if cond else "FAIL"), name, info)

# ---------- limits ----------
L = limits.ResettableLimits(calls=2, period=1)
calls = []
f = L(lambda: calls.append(1))
f(); f()
try:
    f(); raised = False
except limits.RateLimitException as e:
    raised = 0 < e.period_remaining <= 1
check("limiter: 3rd call within period raises with remaining", raised)
g = L.sleep_and_retry(L(lambda: calls.append(2)))
t = time.monotonic(); g(); dt = time.monotonic() - t
check("limiter: sleep_and_retry waits the rest of the period", 0.5 < dt < 1.3, f"{dt:.2f}s")
L2 = limits.ResettableLimits(calls=1, period=10, max=25)
h2 = L2.sleep_and_retry(L2(lambda: None)); h2()
threading.Timer(0.3, L2.reset).start()
t = time.monotonic(); h2(); dt = time.monotonic() - t
check("limiter: reset() wakes a sleeper", dt < 1.5, f"{dt:.2f}s")
check("limiter: add_period capped by max", (L2.add_period(10), L2.add_period(10), L2.add_period(10)) == (20, 25, 25))
L2.set_period(); check("limiter: set_period restores", L2.period == 10)

# ---------- tokens / login ----------
class FakeResp:
    def __init__(self, n):
        self.access_token, self.refresh_token = f"a{n}", f"r{n}"
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        self.access_expire, self.refresh_expire = now + timedelta(minutes=10), now + timedelta(days=365)

saved = []
def mk(password=None, tokens=None):
    h = api.Haier(None, "e@x", password, "ru", tokens=tokens, token_saver=saved.append)
    h.n = {"login": 0, "refresh": 0}
    return h

legacy = {"token": "T", "tokenexpire": "2020-01-01 00:00:00+03:00", "refreshtoken": "R", "refreshexpire": "2099-01-01 00:00:00+03:00"}
h = mk(tokens=legacy)
check("legacy file format parsed", h.token == "T" and h.refreshtoken == "R" and h.refreshexpire.year == 2099)
def ok_refresh():
    h.n["refresh"] += 1; return FakeResp(1)
h.auth_refresh = ok_refresh
saved.clear(); h.auth()
check("expired access + valid refresh -> refresh, tokens saved", h.n["refresh"] == 1 and h.token == "a1" and saved and saved[-1]["refreshtoken"] == "r1")
check("tokens_dict roundtrip", api.Haier(None, "e", None, "ru", tokens=saved[-1]).tokens_dict() == saved[-1])
saved.clear(); h.auth()
check("valid access -> no network, nothing saved", h.n["refresh"] == 1 and not saved)

h = mk(tokens=legacy)
def rejected_refresh():
    h.n["refresh"] += 1
    h.clear_tokens()
    raise api.AuthValidationError("refresh token invalid")
h.auth_refresh = rejected_refresh
try:
    h.auth(); err = None
except Exception as e:
    err = e
check("refresh rejected + no password -> ReauthRequired", isinstance(err, api.ReauthRequired) and h.n["refresh"] == 1, repr(err))

h = mk(password="secret", tokens=legacy)
h.auth_refresh = rejected_refresh
def ok_login():
    h.n["login"] += 1; return FakeResp(2)
h.auth_login = ok_login
h.auth()
check("refresh rejected + password (pre-1.2 entry) -> sign in", h.n["login"] == 1 and h.token == "a2")

h = mk()
try:
    h.auth(); err = None
except Exception as e:
    err = e
check("no tokens, no password -> ReauthRequired without network", isinstance(err, api.ReauthRequired))

h = mk(password="bad")
def bad_login():
    raise api.AuthUserError("wrong password")
h.auth_login = bad_login
try:
    h.login(); err = None
except Exception as e:
    err = e
check("wrong password -> AuthUserError", isinstance(err, api.AuthUserError), repr(err))

h = mk(password="x")
def net_down():
    raise requests.exceptions.ConnectTimeout("timeout")
h.auth_login = net_down
try:
    h.login(); err = None
except Exception as e:
    err = e
check("network error -> InvalidAuth caused by ConnectTimeout", isinstance(err, api.InvalidAuth) and isinstance(err.__cause__, requests.exceptions.ConnectTimeout), repr(err))

check("no debug HTTP view any more", not hasattr(api, "HaierAPI") and not hasattr(api.Haier, "register_view"))
check("MAC check", bool(api.MAC_RE.match("5c:01:3b:00:00:01")) and not api.MAC_RE.match("5c:01:3b:00:00:01/../x") and not api.MAC_RE.match("x"))

# ---------- model sanitizing ----------
check("safe model names", all(CFG.SAFE_MODEL.fullmatch(m) for m in ("ES80VF7", "AS25S2SF2FA", "HSU09HTT103", "AS25PS1HRAM")))
cfg = CFG.HaierDeviceConfig.__new__(CFG.HaierDeviceConfig)
cfg._model, cfg._userpath = "..", "/tmp/should_not_exist"
p = cfg._find_config_file()
check("'..' model -> default profile, no copy", p.endswith("default.yaml"), p)

# ---------- diagnostics ----------
class FakeEntry:
    data = {"email": "user@example.com", "region": "ru", "tokens": {"token": "T", "refreshtoken": "R"}, "password": "pw"}
    entry_id = "E"
# the cloud's icon links are signed for a week; the fixture has them stripped, so add one
SIGNED = ("https://storage.example.invalid/icon?response-content-disposition=attachment"
          "&X-Amz-Credential=YCAJEexample%2F20261106%2Fru-central1%2Fs3%2Faws4_request"
          "&X-Amz-Signature=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef")
class FakeHaier:
    def to_dict(self):
        devices = [dict(d, backend_data=copy.deepcopy(d["backend_data"])) for d in dump["devices"]]
        devices[0]["backend_data"]["icon"] = SIGNED
        return {"socket_status": 2, "devices": devices, "backend_data": dump["backend_data"]}
class FakeHass:
    data = {"haier_evo": {"E": FakeHaier()}}
import asyncio
out = asyncio.run(diagnostics.async_get_config_entry_diagnostics(FakeHass(), FakeEntry()))
text = json.dumps(out, ensure_ascii=False)
import re
macs = re.findall(r"\b[0-9a-f]{2}(?::[0-9a-f]{2}){5}\b", text, re.I)
ips = re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", text)
serial_hits = [s for s in ("SNWH0000000000000001", "SNAC0000000000000002") if s in text]
check("diagnostics: no MAC / IP / serial", not macs and not ips and not serial_hits, f"macs={macs[:3]} ips={ips[:3]} serials={serial_hits}")
check("diagnostics: no email / password / tokens", "user@example.com" not in text and '"pw"' not in text and '"R"' not in text and '"T"' not in text)
check("diagnostics: no signed URL secrets", "X-Amz-Signature=**REDACTED**" in text and not re.search(r"X-Amz-Signature=[0-9a-f]{10}", text))
check("diagnostics: device data kept", '"ES80V-F7"' in text and '"Бойлер"' in text, f"size={len(text)}")

# fields the cloud does not send today but may (sharing, Wi-Fi, the home's
# place), and identifiers in forms the fixtures do not have (1.7.1)
PII = {
    "owner": {"ownerEmail": "owner@mail.ru", "phone": "+7 916 123-45-67", "userId": "u-77"},
    "sharedWith": [{"login": "wife@yandex.ru", "name": "Мария", "note": "звонить 8 (916) 765-43-21"}],
    "network": {"SSID": "MyHomeWiFi", "bssid": "AA-BB-CC-DD-EE-FF", "routerMac": "aabbccddeeff"},
    "location": {"latitude": 55.7558, "longitude": 37.6173, "homeAddress": "Москва, ул. Пример, 1"},
    "links": ["https://cdn.example.invalid/x.png?token=SECRET123&Signature=SIG456&Expires=1",
              "haierevo://device/settings?mac=5c%3A01%3A3b%3A12%3A34%3A56", "текст с 5C-01-3B-12-34-56 внутри"],
    "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.c2lnbmF0dXJl",
    "sn": "AA0B1C2D3E4F5", "imei": "356938035643809",
    "kept": {"uuid": "6f1c2b3a-1d2e-4f5a-9b8c-0123456789ab", "model": "AS25S2SF2FA-B", "value": "24", "id": "31"},
}
class PIIHaier:
    def to_dict(self):
        return {"socket_status": 2, "devices": [{"backend_data": copy.deepcopy(PII)}], "backend_data": {}}
class PIIHass:
    data = {"haier_evo": {"E": PIIHaier()}}
out = asyncio.run(diagnostics.async_get_config_entry_diagnostics(PIIHass(), FakeEntry()))
text = json.dumps(out, ensure_ascii=False)
probes = ["owner@mail.ru", "916 123-45-67", "u-77", "wife@yandex.ru", "765-43-21", "MyHomeWiFi", "AA-BB-CC-DD-EE-FF",
          "aabbccddeeff", "55.7558", "37.6173", "ул. Пример", "SECRET123", "SIG456", "5c%3A01%3A3b%3A12%3A34%3A56",
          "5C-01-3B-12-34-56", "eyJhbGciOiJIUzI1NiJ9", "AA0B1C2D3E4F5", "356938035643809"]
leaked = [p for p in probes if p in text]
check("diagnostics: e-mails, phones, user ids, Wi-Fi, places, link secrets, JWTs and MACs of any form hidden",
      not leaked, f"leaked={leaked}")
check("diagnostics: UUIDs, models, values and names stay",
      all(v in text for v in ("6f1c2b3a-1d2e-4f5a-9b8c-0123456789ab", "AS25S2SF2FA-B", '"24"', '"31"', "Мария", "Expires=")), text[:300])
print("FAILS:", fails)
sys.exit(1 if fails else 0)
