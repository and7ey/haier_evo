"""Websocket/ack/reconnect logic of api.Haier against a local fake Haier server."""
import sys, json, threading, time, asyncio, socket, logging
from pathlib import Path
_HERE = Path(__file__).resolve().parent
root = sys.argv[1] if len(sys.argv) > 1 else str(_HERE.parent)
sys.path.insert(0, root)
from custom_components.haier_evo import api, const as C
from aiohttp import web, WSMsgType
import requests

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
C.WS_PING_INTERVAL = 2
C.WS_PING_TIMEOUT = 1
C.WS_SEND_WAIT = 8
C.WS_ACK_TIMEOUT = 1.5
C.WS_CONNECT_TIMEOUT = 2

PORT = 18765
MAC = "5c:01:3b:00:00:01"
S = {"mode": "normal", "connections": 0, "commands": [], "ping_reply": True, "active": 0}


async def ws_handler(request):
    S["connections"] += 1
    conn = S["connections"]
    ws = web.WebSocketResponse(autoping=S["ping_reply"])
    await ws.prepare(request)
    S["active"] += 1
    try:
        await serve(ws, conn)
    finally:
        S["active"] -= 1
    return ws


async def serve(ws, conn):
    async for msg in ws:
        if msg.type != WSMsgType.TEXT:
            continue  # with autoping=False pings land here and stay unanswered
        m = json.loads(msg.data)
        S["commands"].append((conn, m))
        mode = S["mode"]
        if mode == "silent" or (mode == "silent_first" and conn == 1):
            continue
        ack = {"event": "command_response", "macAddress": m["macAddress"], "errNo": 5 if mode == "reject" else 0}
        if mode != "notrace":
            ack["trace"] = m["trace"]
        await ws.send_str(json.dumps(ack))


def run_server():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    app = web.Application()
    app.router.add_get("/ws/{token}", ws_handler)
    runner = web.AppRunner(app)
    loop.run_until_complete(runner.setup())
    loop.run_until_complete(web.TCPSite(runner, "127.0.0.1", PORT).start())
    loop.run_forever()


threading.Thread(target=run_server, daemon=True).start()
time.sleep(0.5)


class Dev:
    def __init__(self, haier, send_on_open=False):
        self.device_id, self.device_name, self.haier = MAC, "Бойлер", haier
        self.resyncs = 0
        self.send_on_open = send_on_open
    def on_message(self, m): pass
    def write_ha_state(self): pass
    resync_data = None
    def resync(self):
        self.resyncs += 1
        return self.resync_data
    def init_if_needed(self):
        if self.send_on_open:
            self.send_on_open = False
            t = time.time()
            self.haier.send_command(cmd("1", "70"))
            self.open_send_time = time.time() - t


class TestHaier(api.Haier):
    port = PORT
    def register_view(self): pass
    def unregister_view(self): pass
    def auth(self): self.token = "tok"
    def _open_socket(self):
        return socket.create_connection(("127.0.0.1", self.port), timeout=C.WS_CONNECT_TIMEOUT)


def cmd(code, value):
    return {"action": "command", "macAddress": MAC, "command": {"commandName": code, "value": value}}


fails = 0
def check(name, cond, info=""):
    global fails
    fails += not cond
    print(("OK  " if cond else "FAIL"), name, info)


def scenario(mode, ping_reply=True, port=PORT, send_on_open=False):
    S.update(mode=mode, connections=0, commands=[], ping_reply=ping_reply)
    C.API_WS_PATH = f"ws://127.0.0.1:{port}/ws/"
    h = TestHaier(None, "e", "p", "ru")
    h.port = port
    h.devices = [Dev(h, send_on_open)]
    api.Haier.connect_limits.reset()
    h.connect_in_thread()
    return h


def timed(fn):
    t = time.time()
    try:
        fn()
        return None, time.time() - t
    except Exception as e:
        return e, time.time() - t


# 1. normal: one send, one ack
h = scenario("normal")
err, dt = timed(lambda: h.send_command(cmd("1", "74")))
check("normal: confirmed", err is None and len(S["commands"]) == 1 and dt < 2, f"err={err!r} dt={dt:.2f}s cmds={len(S['commands'])}")
check("normal: trace added", "trace" in S["commands"][0][1])
h.stop(); time.sleep(0.3)

# 2. first socket swallows commands: re-sent over a new connection
h = scenario("silent_first")
err, dt = timed(lambda: h.send_command(cmd("1", "74")))
conns = [c for c, _ in S["commands"]]
check("silent socket: re-sent on new connection", err is None and conns == [1, 2], f"err={err!r} dt={dt:.1f}s conns={conns}")
check("silent socket: no resync needed", h.devices[0].resyncs == 0)
h.stop(); time.sleep(0.3)

# 3. rejected
h = scenario("reject")
err, dt = timed(lambda: h.send_command(cmd("1", "74")))
check("reject: HaierCommandRejected", isinstance(err, api.HaierCommandRejected) and err.translation_key == "command_rejected"
      and err.translation_placeholders == {"device": "Бойлер", "code": "5"}, repr(err))
check("reject: state read back", h.devices[0].resyncs == 1)
h.stop(); time.sleep(0.3)

# 3b. non-zero errNo for a value the device already has: not an error
h = scenario("reject")
h.devices[0].resync_data = {"attributes": [{"name": "1", "currentValue": "74"}]}
err, dt = timed(lambda: h.send_command(cmd("1", "74")))
check("errNo for unchanged value: no error", err is None and h.devices[0].resyncs == 1, repr(err))
h.devices[0].resync_data = {"attributes": [{"name": "1", "currentValue": "75"}]}
err, dt = timed(lambda: h.send_command(cmd("1", "74")))
check("errNo and value differs: rejected", isinstance(err, api.HaierCommandRejected), repr(err))
h.stop(); time.sleep(0.3)

aa = api.already_applied
grp = {"action": "operation", "commands": [{"commandName": "0", "value": "24"}, {"commandName": "21", "value": "1"}]}
check("already_applied: group, 24 == 24.00, 1 == true",
      aa(grp, {"attributes": [{"name": "0", "currentValue": "24.00"}, {"name": "21", "currentValue": "true"}]}))
check("already_applied: group, one differs",
      not aa(grp, {"attributes": [{"name": "0", "currentValue": "24.00"}, {"name": "21", "currentValue": "0"}]}))
check("already_applied: unknown attribute", not aa(cmd("99", "1"), {"attributes": [{"name": "1", "currentValue": "1"}]}))

# 4. never confirmed: two attempts, no exception, state read back
h = scenario("silent")
err, dt = timed(lambda: h.send_command(cmd("1", "74")))
check("never acked: 2 attempts, resync, no raise", err is None and len(S["commands"]) == 2 and h.devices[0].resyncs == 1,
      f"err={err!r} cmds={len(S['commands'])} dt={dt:.1f}s")
h.stop(); time.sleep(0.3)

# 5. ack without trace is matched by device
h = scenario("notrace")
err, dt = timed(lambda: h.send_command(cmd("1", "74")))
check("ack without trace", err is None and len(S["commands"]) == 1, f"err={err!r}")
h.stop(); time.sleep(0.3)

# 6. dead socket (no pongs) is detected and replaced
h = scenario("normal", ping_reply=False)
time.sleep(9)
check("dead socket detected by ping_timeout", S["connections"] >= 2, f"connections={S['connections']} in 9 s")
h.stop(); time.sleep(0.3)

# 7. nothing listening: command fails loudly instead of being lost
h = scenario("normal", port=18799)
err, dt = timed(lambda: h.send_command(cmd("1", "74")))
check("no connection: HaierNotConnected", isinstance(err, api.HaierNotConnected) and err.translation_key == "not_connected"
      and 7.5 < dt < 10, f"{err!r} dt={dt:.1f}s")
try:
    h.send_message("{}"); raw = None
except Exception as e:
    raw = e
check("raw send without connection raises", isinstance(raw, api.HaierNotConnected), repr(raw))
h.stop(); time.sleep(0.3)

# 8. command sent from a websocket callback must not wait for its own ack
h = scenario("normal", send_on_open=True)
time.sleep(2)
dev = h.devices[0]
check("send from on_open: no deadlock", getattr(dev, "open_send_time", 99) < 0.5 and S["connections"] == 1 and len(S["commands"]) == 1,
      f"t={getattr(dev, 'open_send_time', None)} conns={S['connections']}")
h.stop(); time.sleep(0.3)

# 9-10 need an address whose SYNs vanish (like a dead Haier backend); TEST-NET-1
# does that on most networks, but some CI networks refuse it at once
def blackholed(host="192.0.2.1", port=443):
    try:
        socket.create_connection((host, port), timeout=1).close()
    except OSError as e:
        return isinstance(e, TimeoutError), repr(e)
    return False, "connected"

dropped, why = blackholed()
if dropped:
    # 9. real _open_socket: unreachable address fails after WS_CONNECT_TIMEOUT, not ~127 s
    C.API_WS_PATH = "wss://192.0.2.1/gateway-ws-service/ws/"
    err, dt = timed(lambda: api.Haier._open_socket(None))
    check("websocket connect timeout", err is not None and 1.5 < dt < 3.5, f"{err!r} dt={dt:.1f}s")

    # 10. HTTP session: connect retries for all methods
    C.API_CONNECT_TIMEOUT = 1
    sess = api.Haier._build_session()
    err, dt = timed(lambda: sess.get("https://192.0.2.1/", timeout=(1, 1)))
    check("http connect: 4 attempts (~1+1+1+1 s + backoff 0+1+2 s)", isinstance(err, requests.exceptions.ConnectionError) and 6 < dt < 9, f"{type(err).__name__} dt={dt:.1f}s")
else:
    print(f"SKIP connect timeouts: this network does not drop 192.0.2.1 ({why})")
    C.API_CONNECT_TIMEOUT = 1
    sess = api.Haier._build_session()

# 10b. read retries for GET only

hits = {"n": 0}
def drop_server():
    srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 18766)); srv.listen(16)
    while True:
        c, _ = srv.accept(); hits["n"] += 1; c.recv(4096); c.close()
threading.Thread(target=drop_server, daemon=True).start(); time.sleep(0.2)
sess.mount("http://", sess.adapters["https://"])
err, _ = timed(lambda: sess.get("http://127.0.0.1:18766/", timeout=(1, 2)))
check("http GET: dropped connection retried 2 times", hits["n"] == 3, f"hits={hits['n']} {type(err).__name__}")
hits["n"] = 0
err, _ = timed(lambda: sess.post("http://127.0.0.1:18766/", data={"a": 1}, timeout=(1, 2)))
check("http POST: not re-sent after it reached the server", hits["n"] == 1, f"hits={hits['n']} {type(err).__name__}")

# 12. one command never holds a thread longer than the budget
C.WS_COMMAND_BUDGET = 3
h = scenario("silent")
err, dt = timed(lambda: h.send_command(cmd("1", "74")))
check("budget: silent cloud, command returns within budget", err is None and dt < 4, f"err={err!r} dt={dt:.1f}s")
h.stop(); time.sleep(0.3)
C.WS_COMMAND_BUDGET = 30

# 13. at most N commands wait at once, the rest fail fast
C.WS_MAX_PENDING_COMMANDS = 2
h = scenario("silent")
time.sleep(0.5)
bg = [threading.Thread(target=lambda: timed(lambda: h.send_command(cmd("1", "70"))), daemon=True) for _ in range(2)]
[t.start() for t in bg]
time.sleep(0.3)
err, dt = timed(lambda: h.send_command(cmd("1", "71")))
check("slots: third concurrent command -> too_many_commands fast",
      isinstance(err, api.HaierNotConnected) and err.translation_key == "too_many_commands" and dt < 2, f"{err!r} dt={dt:.1f}s")
h.stop(); time.sleep(0.3)
C.WS_MAX_PENDING_COMMANDS = 4

# 11. stop() while the connection is being opened / after it is open: nothing stays connected
for delay in (0, 0.01, 0.05, 1):
    h = scenario("normal")
    time.sleep(delay)
    h.stop()
    time.sleep(2.5)
    check(f"stop() after {delay}s leaves no open connection", S["active"] == 0, f"active={S['active']} connections={S['connections']}")

print("FAILS:", fails)
sys.exit(1 if fails else 0)
