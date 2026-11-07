"""The HTTP side of api.Haier on the real requests stack against a local fake
Haier cloud: sign-in, token refresh, the device list and device descriptions,
the cloud's error answers, timeouts, retries and what reaches the log."""
import sys, json, threading, time, asyncio, socket, logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
_HERE = Path(__file__).resolve().parent
root = sys.argv[1] if len(sys.argv) > 1 else str(_HERE.parent)
sys.path.insert(0, root)
from custom_components.haier_evo import api, const as C
from aiohttp import web

dump = json.loads((_HERE / "fixtures" / "evo_dump.json").read_text(encoding="utf-8"))
PASSWORD = "pw-secret-1"
MAC = dump["devices"][1]["device_id"]

# ---- the fake cloud: answers are queued per route, the last one repeats
PLAN, SEEN = {}, []
ROUTES = {
    ("POST", "/v2/ru/users/auth/sign-in"): "sign-in",
    ("POST", "/v2/ru/users/auth/refresh"): "refresh",
    ("GET", "/v2/ru/pages/sduiRawPaginated/smartHome"): "devices",
    ("GET", "/mobile-backend-service/api/v1/config/{mac}"): "status",
}

def answer(status=200, body=None, delay=0.0):
    return (status, body, delay)

def cloud_error(message=None, code=200, **validation):
    """An error as the cloud sends it (from logs in upstream issues #1, #29, #66):
    HTTP 200 and {"data": null, "error": {...}}."""
    return {"data": None, "error": {"code": code, "message": message, "validation": validation or None}}

def plan(**routes):
    PLAN.clear(); SEEN.clear()
    PLAN.update({k.replace("_", "-"): list(v) if isinstance(v, list) else [v] for k, v in routes.items()})

def handler(name):
    async def handle(request):
        form = dict(await request.post()) if request.method == "POST" else {}
        SEEN.append({"route": name, "headers": dict(request.headers), "form": form, "query": dict(request.query)})
        queue = PLAN.get(name) or [answer(500, {"error": "no answer planned"})]
        status, body, delay = queue.pop(0) if len(queue) > 1 else queue[0]
        if delay:
            await asyncio.sleep(delay)
        text = body if isinstance(body, str) else json.dumps(body)
        return web.Response(status=status, text=text, content_type="application/json")
    return handle

def run_server(ready):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    app = web.Application()
    for (method, path), name in ROUTES.items():
        app.router.add_route(method, path, handler(name))
    runner = web.AppRunner(app)
    loop.run_until_complete(runner.setup())
    site = web.TCPSite(runner, "127.0.0.1", 0)
    loop.run_until_complete(site.start())
    ready.append(site._server.sockets[0].getsockname()[1])
    loop.run_forever()

ready = []
threading.Thread(target=run_server, args=(ready,), daemon=True).start()
while not ready:
    time.sleep(0.05)
PORT = ready[0]
C.API_PATH = f"http://127.0.0.1:{PORT}"
C.API_STATUS = f"http://127.0.0.1:{PORT}/mobile-backend-service/api/v1/config/{{mac}}?type=DETAILED"
C.TOKEN_RETRY_DELAY = 0.1
READ_TIMEOUT = C.API_READ_TIMEOUT

# everything the integration logs, debug included: no password or token may get there
class Records(logging.Handler):
    lines = []
    def emit(self, record):
        Records.lines.append(record.getMessage())
logging.getLogger("custom_components.haier_evo").addHandler(Records())
logging.getLogger("custom_components.haier_evo").setLevel(logging.DEBUG)
logging.getLogger("custom_components.haier_evo").propagate = False

fails = 0
def check(name, got, want=True):
    global fails
    ok = got == want
    fails += not ok
    print(("OK  " if ok else "FAIL"), name, "" if ok else f"\n      got:  {got!r}\n      want: {want!r}")

MSK = timezone(timedelta(hours=3))
def token(n, minutes=60, refresh=True):
    exp = (datetime.now(MSK) + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S+03:00")
    body = {"accessToken": f"acc{n}", "expire": exp}
    if refresh:
        body.update(refreshToken=f"ref{n}", refreshExpire=(datetime.now(MSK) + timedelta(days=30)).isoformat())
    return answer(200, {"data": {"token": body}})

def client(password=PASSWORD, tokens=None):
    saved = []
    h = api.Haier(None, "user@example.com", password, "ru", tokens=tokens, token_saver=saved.append, device_id="dev-1")
    # in production the cloud is https: the same retries for the fake http cloud
    h._session.mount("http://", h._session.adapters["https://"])
    h.saved = saved
    return h

def expired(**extra):
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    future = (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()
    return {"token": "acc-old", "tokenexpire": past, "refreshtoken": "ref-old", "refreshexpire": future, **extra}

def raised(fn):
    try:
        fn()
    except Exception as e:  # noqa: BLE001 - the checks look at what came out
        return e
    return None

def hits(route):
    return [s for s in SEEN if s["route"] == route]

# ---- sign-in
plan(sign_in=token(1))
h = client()
h.login()
req = hits("sign-in")[0]
check("sign-in: the tokens of the answer are taken and saved",
      (h.token, h.refreshtoken, h.tokenexpire > datetime.now(timezone.utc), h.saved[-1]["token"], h.saved[-1]["refreshtoken"]),
      ("acc1", "ref1", True, "acc1", "ref1"))
check("sign-in: a form with the email and the password", req["form"], {"email": "user@example.com", "password": PASSWORD})
check("sign-in: the headers of the app and the entry's Device-Id",
      (req["headers"].get("User-Agent"), req["headers"].get("Platform"), req["headers"].get("Device-Id")), ("evo-mobile", "android", "dev-1"))

def sign_in_fails(*answers, password=PASSWORD):
    plan(sign_in=list(answers))
    h = client(password=password)
    return h, raised(h.login)

for field in ("password", "email"):
    h, err = sign_in_fails(answer(200, cloud_error(**{field: "Введён некорректный " + field})))
    check(f"rejected {field} (error.validation.{field}): AuthUserError, the client stops",
          (type(err).__name__, h.disconnect_requested), ("AuthUserError", True))
n = len(SEEN)
check("after a rejected sign-in nothing more goes out", (type(raised(h.login)).__name__, len(SEEN) - n), ("InvalidAuth", 0))

h, err = sign_in_fails(answer(200, cloud_error("Не удалось подключиться к CRM.")))
check("an error message of the cloud: InvalidAuth, the next sign-in waits 60 s more",
      (type(err).__name__, api.Haier.auth_login_limits.period), ("InvalidAuth", C.LOGIN_LIMIT_PERIOD + C.LOGIN_LIMIT_500))
h, err = sign_in_fails(answer(429, cloud_error("Превышено количество запросов", code=429)))
check("HTTP 429: InvalidAuth from ManyRequestsError, the next sign-in waits 60 s more",
      (type(err).__name__, type(err.__cause__).__name__, api.Haier.auth_login_limits.period),
      ("InvalidAuth", "ManyRequestsError", C.LOGIN_LIMIT_PERIOD + C.LOGIN_LIMIT_429))
odd = {
    "HTTP 502 with an HTML page": answer(502, "<html><body>Bad Gateway</body></html>"),
    "200 with a broken body": answer(200, '{"data": {"token": '),
    "200 without a token": answer(200, {"data": {}}),
    "200 with an empty access token": answer(200, {"data": {"token": {"accessToken": ""}}}),
    "an error that is a string": answer(200, {"error": "something"}),
    "an error object without message or validation": answer(200, cloud_error()),
    "a body that is a list": answer(200, "[1, 2]"),
}
got = {}
for name, reply in odd.items():
    h, err = sign_in_fails(reply)
    got[name] = (type(err).__name__, h.token)
check("odd answers to the sign-in: InvalidAuth, no token, no crash", got, {name: ("InvalidAuth", None) for name in odd})
h, err = sign_in_fails(token(1), password=None)
check("no tokens and no password: ReauthRequired without a request", (type(err).__name__, len(SEEN)), ("ReauthRequired", 0))

# ---- refresh
plan(refresh=token(2))
h = client(tokens=expired())
h.auth()
check("refresh: an expired access token is renewed with the refresh token, the new pair kept and saved",
      ([s["route"] for s in SEEN], hits("refresh")[0]["form"], h.token, h.refreshtoken, h.saved[-1]["token"]),
      (["refresh"], {"refreshToken": "ref-old"}, "acc2", "ref2", "acc2"))
plan(refresh=token(3, refresh=False))
h = client(tokens=expired())
h.auth()
check("refresh: an answer without a refresh token keeps the old one", (h.token, h.refreshtoken), ("acc3", "ref-old"))
REFRESH_REJECTED = cloud_error(refreshToken="Поле «Токен обновления» содержит некорректное значение")
plan(refresh=answer(200, REFRESH_REJECTED), sign_in=token(4))
h = client(tokens=expired())
h.auth()
check("refresh token rejected: the password signs in",
      ([s["route"] for s in SEEN], h.token, h.refreshtoken), (["refresh", "sign-in"], "acc4", "ref4"))
plan(refresh=answer(200, REFRESH_REJECTED))
h = client(password=None, tokens=expired())
err = raised(h.auth)
check("refresh token rejected, no password: ReauthRequired, the tokens cleared and saved so",
      (type(err).__name__, h.refreshtoken, h.saved[-1]["refreshtoken"], [s["route"] for s in SEEN]),
      ("ReauthRequired", None, None, ["refresh"]))
for status in (500, 503):
    plan(refresh=answer(status, "<html>oops</html>"))
    h = client(tokens=expired())
    err = raised(h.auth)
    check(f"refresh: HTTP {status} keeps the refresh token for the next try", (type(err).__name__, h.refreshtoken), ("InvalidAuth", "ref-old"))
plan(refresh=answer(200, cloud_error("Не удалось подключиться к CRM.")))
h = client(tokens=expired())
err = raised(h.auth)
check("refresh: an error message of the cloud keeps the tokens, the next refresh waits 60 s more",
      (type(err).__name__, h.refreshtoken, api.Haier.auth_refresh_limits.period),
      ("InvalidAuth", "ref-old", C.REFRESH_LIMIT_PERIOD + C.REFRESH_LIMIT_500))
plan(refresh=token(5), sign_in=token(6))
h = client(tokens=dict(expired(), refreshexpire=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat()))
h.auth()
check("an expired refresh token is not sent: the password signs in", ([s["route"] for s in SEEN], h.token), (["sign-in"], "acc6"))

# ---- network
C.API_READ_TIMEOUT = 1
plan(sign_in=answer(200, token(7)[1], delay=2.5))
h = client()
started = time.monotonic()
err = raised(h.login)
check("sign-in times out: InvalidAuth, the POST is not sent again",
      (type(err).__name__, type(err.__cause__).__name__, len(hits("sign-in")), time.monotonic() - started < 5),
      ("InvalidAuth", "ReadTimeout", 1, True))
plan(devices=answer(200, {"data": {}}, delay=2.5))
h = client(tokens=expired(tokenexpire=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()))
check("the device list times out: retried as a GET, then nothing ({})", (h.pull_data_from_api(), len(hits("devices"))), ({}, 3))
C.API_READ_TIMEOUT = READ_TIMEOUT
time.sleep(3)  # the fake cloud finishes the delayed answers

with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    closed = s.getsockname()[1]
saved_path = C.API_PATH
C.API_PATH = f"http://127.0.0.1:{closed}"
h = client()
started = time.monotonic()
err = raised(h.login)
check("nothing listens: InvalidAuth from a ConnectionError after the retries",
      (type(err).__name__, type(err.__cause__).__name__, time.monotonic() - started < 15), ("InvalidAuth", "ConnectionError", True))
C.API_PATH = saved_path

# ---- the device list
valid = expired(tokenexpire=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
plan(devices=answer(200, {"data": dump["backend_data"]}))
h = client(tokens=valid)
data = h.pull_data_from_api()
req = hits("devices")[0]
check("device list: the app's page as it came, four devices in it",
      (data == dump["backend_data"], len(api.Haier._parse_device_list(data))), (True, 4))
check("device list: the token, the Device-Id and the page of the app",
      (req["headers"].get("X-Auth-Token"), req["headers"].get("Device-Id"), req["query"]),
      ("acc-old", "dev-1", {"part": "1", "partitionWeight": "6"}))
odd = {"HTTP 401": answer(401, {"error": "Unauthorized"}), "HTTP 500": answer(500, "<html/>"),
       "data is a list": answer(200, {"data": [1]}), "a broken body": answer(200, "{"), "no data": answer(200, {})}
got = {}
for name, reply in odd.items():
    plan(devices=reply)
    got[name] = client(tokens=valid).pull_data_from_api()
check("device list: errors and odd answers give {} (unknown), no exception", got, {name: {} for name in odd})
plan(devices=answer(200, {"data": {}}))
h = client(tokens=valid)
check("an empty list is not 'no devices': unknown (None)", h.list_device_ids(), None)

# ---- device descriptions
backend = dump["devices"][1]["backend_data"]
plan(status=answer(200, backend))
h = client(tokens=valid)
check("description: the cloud's JSON as it came, for this MAC",
      (h.pull_device_data(MAC) == backend, hits("status")[0]["query"]), (True, {"type": "DETAILED"}))
for status in (403, 404):
    plan(status=answer(status, {"error": "Forbidden"}))
    err = raised(lambda: client(tokens=valid).pull_device_data(MAC))
    check(f"description: HTTP {status} is a refused device, one request, no retry",
          (api.Haier.is_denied(err), len(hits("status"))), (True, 1))
plan(status=[answer(500, "<html/>"), answer(500, "<html/>")])
err = raised(lambda: client(tokens=valid).pull_device_data(MAC))
check("description: HTTP 500 twice is raised after the second try", (type(err).__name__, len(hits("status"))), ("HTTPError", 2))
plan(status=[answer(401, {"error": "Unauthorized"}), answer(200, backend)], sign_in=token(8))
h = client()
h.login()
SEEN.clear()
data = h.pull_device_data(MAC)
check("description: 401 for a token just issued: the same token once more after a pause, no new sign-in",
      (data == backend, [s["route"] for s in SEEN], [s["headers"].get("X-Auth-Token") for s in SEEN]),
      (True, ["status", "status"], ["acc8", "acc8"]))
plan(status=[answer(401, {"error": "Unauthorized"}), answer(200, backend)], refresh=token(9))
h = client(tokens=valid)
data = h.pull_device_data(MAC)
check("description: 401 for an older token: renewed, the second request with the new one",
      (data == backend, [s["route"] for s in SEEN], [s["headers"].get("X-Auth-Token") for s in SEEN if s["route"] == "status"]),
      (True, ["status", "refresh", "status"], ["acc-old", "acc9"]))
plan(status=answer(403, {"error": "Forbidden"}))
h = client(tokens=valid)
check("refused devices are found, the others not", h.denied_devices([MAC]), {MAC})

# ---- stop
plan(sign_in=token(10))
h = client()
h.stop()
err = raised(h.login)
check("after stop() no request goes out", (type(err).__name__, len(SEEN)), ("InvalidAuth", 0))

# ---- the log
secrets = [PASSWORD] + [f"acc{n}" for n in range(1, 11)] + [f"ref{n}" for n in range(1, 11)] + ["acc-old", "ref-old"]
leaked = sorted({s for s in secrets for line in Records.lines if s in line})
check("the log has neither the password nor a token (debug included)", leaked, [])
check("... and the log was written at all", len(Records.lines) > 20, True)

print("FAILS:", fails)
sys.exit(1 if fails else 0)
