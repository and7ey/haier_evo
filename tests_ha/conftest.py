"""Fixtures of the tests on a real Home Assistant core.

Home Assistant itself is real here (pytest-homeassistant-custom-component):
config entries and their flows, the entity and device registries, the state
machine, services, storage (kept in memory), the diagnostics view, and the
harness's own checks after every test: no lingering tasks, timers or
threads, no connection to anything but 127.0.0.1.

The Haier Evo cloud is a local fake on 127.0.0.1, served by the test's event
loop: sign-in, token refresh, the app's home screen (the device list),
device descriptions and the websocket, with the anonymized cloud data of
tests/fixtures/evo_dump.json (a water heater ES80V-F7 and three AS25S2SF2FA).
The integration reaches it through its own network code: requests in Home
Assistant's executor, websocket-client in a thread of its own.
"""
from __future__ import annotations

import asyncio
import copy
import json
import socket
import threading
import time
from collections.abc import AsyncGenerator, Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from aiohttp import WSMsgType, web
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.haier_evo as integration
from custom_components.haier_evo import api
from custom_components.haier_evo import const as C

DOMAIN = C.DOMAIN
FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
DUMP = json.loads((FIXTURES / "evo_dump.json").read_text(encoding="utf-8"))
REGRESSION = json.loads((FIXTURES / "regression.json").read_text(encoding="utf-8"))
RELEASES = json.loads((FIXTURES / "entities_by_release.json").read_text(encoding="utf-8"))

BOILER = "5c:01:3b:00:00:01"   # ES80V-F7
AC_OFF = "a0:a3:b3:00:00:02"   # AS25S2SF2FA, off
AC_ON = "30:c9:22:00:00:04"    # AS25S2SF2FA, running in fan-only mode
SERIALS = [d["device_serial"] for d in DUMP["devices"]]
EMAIL = "user@example.com"
PASSWORD = "pw-secret-1"
MSK = timezone(timedelta(hours=3))

# threads of Home Assistant and of the harness; any other one alive after an
# unload belongs to the integration (its websocket, ping, timers, resyncs)
_HARNESS_THREADS = ("SyncWorker", "ImportExecutor", "asyncio_", "ThreadPoolExecutor", "waitpid-",
                    "Recorder", "_run_safe_shutdown_loop")


def cloud_error(message: str | None = None, **validation: str) -> dict:
    """An error as the cloud sends it: HTTP 200 and data null (see tests/check_http.py)."""
    return {"data": None, "error": {"code": 200, "message": message, "validation": validation or None}}


def last_release() -> dict:
    """What the newest release recorded in entities_by_release.json creates."""
    versions = [k for k in RELEASES if k.startswith("v")]
    return RELEASES[max(versions, key=lambda v: tuple(int(x) for x in v[1:].split(".")))]


async def wait_for(predicate: Callable[[], Any], what: str, timeout: float = 15) -> None:
    """Let the event loop (and the fake cloud in it) run until predicate() holds."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {timeout} s waiting for {what}")
        await asyncio.sleep(0.02)


async def wait_state(hass: HomeAssistant, entity: str, state: str | None = None, timeout: float = 5,
                     **attributes: Any):
    """Wait until the entity shows this state and these attributes, then return
    it: a command is confirmed first, the cloud's echo with the new values of
    the device comes after it."""
    def matches() -> bool:
        current = hass.states.get(entity)
        return current is not None and (state is None or current.state == state) and all(
            current.attributes.get(k) == v for k, v in attributes.items())
    try:
        await wait_for(matches, f"{entity}: {state} {attributes}", timeout)
    except AssertionError:
        current = hass.states.get(entity)
        shown = (current.state, {k: current.attributes.get(k) for k in attributes}) if current else None
        raise AssertionError(f"{entity} shows {shown}, not {(state, attributes)}") from None
    return hass.states.get(entity)


def entity_id(hass: HomeAssistant, domain: str, unique_id: str) -> str:
    found = er.async_get(hass).async_get_entity_id(domain, DOMAIN, unique_id)
    assert found, f"no {domain} entity with unique_id {unique_id}"
    return found


def haier(hass: HomeAssistant, entry: MockConfigEntry) -> api.Haier:
    return hass.data[DOMAIN][entry.entry_id]


def store_key(entry: MockConfigEntry, suffix: str = "") -> str:
    return f"{DOMAIN}.{entry.entry_id}{suffix}"


class FakeCloud:
    """The Haier Evo cloud as the integration sees it."""

    def __init__(self) -> None:
        self.port = 0
        self.password = PASSWORD
        self.issued = 0
        self.access: set[str] = set()    # access tokens the cloud accepts now
        self.refresh: set[str] = set()   # refresh tokens it accepts now
        self.home = copy.deepcopy(DUMP["backend_data"])   # the app's home screen
        self.descriptions = {d["device_id"]: copy.deepcopy(d["backend_data"]) for d in DUMP["devices"]}
        self.requests: list[dict] = []
        self.planned: dict[str, list[tuple[int, Any]]] = {}
        self.down: set[str] = set()      # routes answering 503 for now
        self.denied: set[str] = set()    # MACs whose description the cloud refuses (403)
        self.sockets: list[web.WebSocketResponse] = []
        self.ws_tokens: list[str] = []
        self.ws_refused = False          # websocket upgrades answer 503
        self.received: list[dict] = []   # what the integration sent over the websocket
        self.err_no: int | str = 0       # errNo of every command_response
        self.answer_commands = True
        self.answer_close = True         # False: a half-dead connection, the close frame gets no answer
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        app = web.Application()
        app.router.add_post("/v2/{region}/users/auth/sign-in", self._sign_in)
        app.router.add_post("/v2/{region}/users/auth/refresh", self._refresh)
        app.router.add_get("/v2/{region}/pages/sduiRawPaginated/smartHome", self._home)
        app.router.add_get("/mobile-backend-service/api/v1/config/{mac}", self._description)
        app.router.add_get("/gateway-ws-service/ws/{token}", self._websocket)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self.port = self._runner.addresses[0][1]

    async def stop(self) -> None:
        for ws in list(self.sockets):
            await ws.close()
        if self._runner is not None:
            await self._runner.cleanup()

    # ---- what tests set up and look at

    def issue(self, minutes: int = 60) -> dict:
        """A new token pair, as the sign-in and refresh answers carry it."""
        self.issued += 1
        access, refresh = f"acc-{self.issued}", f"ref-{self.issued}"
        self.access.add(access)
        self.refresh.add(refresh)
        expire = (datetime.now(MSK) + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S+03:00")
        refresh_expire = (datetime.now(MSK) + timedelta(days=30)).isoformat()
        return {"data": {"token": {"accessToken": access, "expire": expire, "refreshToken": refresh,
                                   "refreshExpire": refresh_expire}}, "error": None}

    def tokens(self, minutes: int = 60) -> dict:
        """A token pair as the integration stores it (api.Haier.tokens_dict)."""
        token = self.issue(minutes)["data"]["token"]
        return {
            "token": token["accessToken"],
            "tokenexpire": api.parse_expire(token["expire"]).isoformat(),
            "refreshtoken": token["refreshToken"],
            "refreshexpire": api.parse_expire(token["refreshExpire"]).isoformat(),
        }

    def plan(self, route: str, *answers: tuple[int, Any]) -> None:
        """The next answers of a route ("sign-in", "refresh", "home", "description"),
        the usual ones after them."""
        self.planned.setdefault(route, []).extend(answers)

    def seen(self, route: str) -> list[dict]:
        return [r for r in self.requests if r["route"] == route]

    def commands(self, mac: str | None = None) -> list[dict]:
        """Websocket messages the integration sent, without their trace."""
        return [{k: v for k, v in m.items() if k != "trace"} for m in self.received
                if mac is None or m.get("macAddress") == mac]

    async def wait_connected(self, sessions: int = 1) -> None:
        await wait_for(lambda: len(self.sockets) >= sessions, f"{sessions} websocket session(s)")

    async def push(self, mac: str, properties: dict[str, str] | None = None, *,
                   event: str = "status", payload: Any = None) -> None:
        """An event of the cloud to every open session (a status by default)."""
        if payload is None:
            payload = {"statuses": [{"properties": properties or {}}]}
        message = json.dumps({"event": event, "macAddress": mac, "payload": payload}, ensure_ascii=False)
        for ws in list(self.sockets):
            await ws.send_str(message)

    async def send(self, message: dict) -> None:
        """Any message of the cloud to every open session (a program event, say)."""
        text = json.dumps(message, ensure_ascii=False)
        for ws in list(self.sockets):
            await ws.send_str(text)

    async def drop_sessions(self) -> None:
        for ws in list(self.sockets):
            await ws.close()

    # ---- the routes

    @staticmethod
    def _json(body: Any, status: int = 200) -> web.Response:
        text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
        return web.Response(status=status, text=text, content_type="application/json")

    def _record(self, route: str, request: web.Request, form: dict | None = None) -> web.Response | None:
        self.requests.append({"route": route, "headers": dict(request.headers), "form": form or {},
                              "path": request.path})
        if queue := self.planned.get(route):
            status, body = queue.pop(0)
            return self._json(body, status)
        if route in self.down:
            return self._json({"error": "Service Unavailable"}, 503)
        return None

    def _authorized(self, request: web.Request) -> bool:
        return request.headers.get("X-Auth-Token") in self.access

    async def _sign_in(self, request: web.Request) -> web.Response:
        form = dict(await request.post())
        if (answer := self._record("sign-in", request, form)) is not None:
            return answer
        if form.get("email") != EMAIL or form.get("password") != self.password:
            return self._json(cloud_error(password="Неверный логин или пароль"))
        return self._json(self.issue())

    async def _refresh(self, request: web.Request) -> web.Response:
        form = dict(await request.post())
        if (answer := self._record("refresh", request, form)) is not None:
            return answer
        token = form.get("refreshToken")
        if token not in self.refresh:
            return self._json(cloud_error(refreshToken="Токен недействителен"))
        self.refresh.discard(token)  # a refresh rotates the pair
        return self._json(self.issue())

    async def _home(self, request: web.Request) -> web.Response:
        if (answer := self._record("home", request)) is not None:
            return answer
        if not self._authorized(request):
            return self._json({"error": "Unauthorized"}, 401)
        return self._json({"data": self.home})

    async def _description(self, request: web.Request) -> web.Response:
        if (answer := self._record("description", request)) is not None:
            return answer
        if not self._authorized(request):
            return self._json({"error": "Unauthorized"}, 401)
        mac = request.match_info["mac"].lower()
        if mac in self.denied:
            return self._json({"error": "Forbidden"}, 403)
        if mac not in self.descriptions:
            return self._json({"error": "Not Found"}, 404)
        return self._json(self.descriptions[mac])

    async def _websocket(self, request: web.Request) -> web.StreamResponse:
        token = request.match_info["token"]
        self.ws_tokens.append(token)
        if self.ws_refused:
            return web.Response(status=503)
        if token not in self.access:
            return web.Response(status=401)
        # the close frame of the integration is answered below (or not)
        ws = web.WebSocketResponse(autoclose=False)
        await ws.prepare(request)
        self.sockets.append(ws)
        try:
            # as the cloud does: the state of the devices right after an opening
            for mac in self.descriptions:
                await ws.send_str(json.dumps({"event": "status", "macAddress": mac,
                                              "payload": {"statuses": [{"properties": {}}]}}))
            async for msg in ws:
                if msg.type is not WSMsgType.TEXT:
                    continue
                message = json.loads(msg.data)
                self.received.append(message)
                if not self.answer_commands:
                    continue
                mac = message.get("macAddress")
                await ws.send_str(json.dumps({
                    "event": "command_response", "macAddress": mac, "trace": message.get("trace"),
                    "errNo": self.err_no,
                }))
                if str(self.err_no) == "0":
                    # then, as the cloud does, the new values of the device
                    commands = message.get("commands") or [message.get("command") or {}]
                    values = {str(c["commandName"]): c["value"] for c in commands if "commandName" in c}
                    await self.push(mac, values)
            if self.answer_close:
                await ws.close()
            else:
                # a half-dead connection: the close frame gets no answer, the
                # connection stays as it is until the integration drops it
                transport, deadline = request.transport, time.monotonic() + 10
                while transport is not None and not transport.is_closing() and time.monotonic() < deadline:
                    await asyncio.sleep(0.05)
        finally:
            self.sockets.remove(ws)
        return ws


def _integration_threads(before: set[threading.Thread]) -> list[threading.Thread]:
    return [
        t for t in threading.enumerate()
        if t not in before and t.is_alive() and not isinstance(t, threading._DummyThread)
        and not t.name.startswith(_HARNESS_THREADS)
    ]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Home Assistant loads custom_components/haier_evo of this checkout."""


@pytest.fixture
def hass_config_dir(hass_tmp_config_dir: str) -> str:
    """A config directory of each test's own: the integration looks there for
    the tokens file of 1.1.x and for profiles of the user."""
    return hass_tmp_config_dir


@pytest.fixture
async def cloud(hass: HomeAssistant, socket_enabled: None, monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[FakeCloud]:
    fake = FakeCloud()
    await fake.start()
    base = f"http://127.0.0.1:{fake.port}"
    monkeypatch.setattr(C, "API_PATH", base)
    monkeypatch.setattr(C, "API_STATUS", base + "/mobile-backend-service/api/v1/config/{mac}?type=DETAILED")
    monkeypatch.setattr(C, "API_WS_PATH", f"ws://127.0.0.1:{fake.port}/gateway-ws-service/ws/")
    # plain TCP to the fake instead of TLS to iot-platform:443
    monkeypatch.setattr(api.Haier, "_open_socket", lambda self: socket.create_connection(
        ("127.0.0.1", fake.port), timeout=C.WS_CONNECT_TIMEOUT))
    # seconds, not minutes: reconnects, retries and waits
    monkeypatch.setattr(api.Haier.connect_limits, "period", 0.2)
    monkeypatch.setattr(api.Haier.connect_limits, "_init_period", 0.2)
    monkeypatch.setattr(C, "WS_SEND_WAIT", 5)
    monkeypatch.setattr(C, "WS_ACK_TIMEOUT", 3)
    monkeypatch.setattr(C, "TOKEN_RETRY_DELAY", 0.05)
    monkeypatch.setattr(integration, "CACHE_RETRY_FIRST", 0.2)
    monkeypatch.setattr(integration, "CACHE_RETRY_MAX", 1)
    monkeypatch.setattr(integration, "_INCOMPLETE_SETUPS", {})
    before = set(threading.enumerate())
    yield fake
    # whatever a test left set up is unloaded: then nothing of the integration
    # may keep running (the harness checks the threads once more after
    # Home Assistant has stopped)
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state in (ConfigEntryState.LOADED, ConfigEntryState.SETUP_RETRY):
            assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    deadline = time.monotonic() + 10
    while (left := _integration_threads(before)) and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    await fake.stop()
    assert not left, f"still running after the unload: {[t.name for t in left]}"


@pytest.fixture
def make_entry(hass: HomeAssistant) -> Callable[..., MockConfigEntry]:
    def make(options: dict | None = None, **data: Any) -> MockConfigEntry:
        entry = MockConfigEntry(
            domain=DOMAIN, title=f"{EMAIL} (ru)", unique_id=f"{EMAIL}_ru",
            data={"email": EMAIL, "region": "ru", **data}, options=options or {},
        )
        entry.add_to_hass(hass)
        return entry
    return make


@pytest.fixture
def stored_entry(cloud: FakeCloud, make_entry, hass_storage: dict[str, Any]) -> MockConfigEntry:
    """An entry of an installation that has run before: its tokens are in storage."""
    entry = make_entry()
    hass_storage[store_key(entry)] = {
        "version": C.STORAGE_VERSION, "minor_version": 1, "key": store_key(entry), "data": cloud.tokens(),
    }
    return entry


@pytest.fixture
async def setup_entry(hass: HomeAssistant, cloud: FakeCloud, stored_entry: MockConfigEntry) -> MockConfigEntry:
    assert await hass.config_entries.async_setup(stored_entry.entry_id)
    await hass.async_block_till_done()
    assert stored_entry.state is ConfigEntryState.LOADED
    await cloud.wait_connected()
    return stored_entry
