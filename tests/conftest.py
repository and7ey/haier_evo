"""Fixtures: the Evo cloud is mocked, WebSocket is a fake in-memory socket."""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from aiohttp import ClientSession, WSMsgType
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.haier_evo import const as C

from .ci_report import (  # noqa: F401 - the report of a CI job (CHECKS_REPORT)
    pytest_runtest_logreport,
    pytest_sessionfinish,
    pytest_sessionstart,
)

FIXTURES = Path(__file__).parent / "fixtures"
MAC_1 = "00:00:5e:00:53:01"  # RFC 7042 documentation range, not a real device
MAC_2 = "00:00:5e:00:53:02"
EMAIL = "user@example.com"
PASSWORD = "test-password-123"
LOGIN_URL = f"{C.API_PATH}/v2/ru/users/auth/sign-in"
REFRESH_URL = f"{C.API_PATH}/v2/ru/users/auth/refresh"
DEVICES_URL = f"{C.API_PATH}/v2/ru/pages/sduiRawPaginated/smartHome"


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def status_url(mac: str) -> str:
    return C.API_STATUS.format(mac=mac).split("?")[0]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture(autouse=True)
def fast_timings():
    """Short backoff/timeouts so reconnect tests run in milliseconds."""
    with (
        patch.object(C, "WS_BACKOFF_MIN", 0.01),
        patch.object(C, "WS_BACKOFF_MAX", 0.05),
        patch.object(C, "WS_SEND_TIMEOUT", 1),
        patch.object(C, "TOKEN_RETRY_DELAY", 0),
        # the fake socket never answers command_response unless a test says so
        patch.object(C, "WS_ACK_TIMEOUT", 60),
        # a service call does not wait for an answer the fake socket never gives
        patch.object(C, "WS_COMMAND_WAIT", 0.05),
        patch.object(C, "API_RETRY_BACKOFF", 0),
    ):
        yield


class FakeWebSocket:
    """Minimal stand-in for aiohttp.ClientWebSocketResponse."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.closed = False
        self.close_code: int | None = None
        self.fail_send = False
        self._queue: asyncio.Queue = asyncio.Queue()

    def feed(self, message: dict | str) -> None:
        data = message if isinstance(message, str) else json.dumps(message, ensure_ascii=False)
        self._queue.put_nowait(SimpleNamespace(type=WSMsgType.TEXT, data=data))

    def server_close(self) -> None:
        """The server closes the socket (what happens after idle, #89)."""
        self.closed = True
        self.close_code = 1000
        self._queue.put_nowait(None)

    async def send_str(self, data: str) -> None:
        if self.closed or self.fail_send:
            raise ConnectionResetError("Cannot write to closing transport")
        self.sent.append(json.loads(data))

    async def close(self) -> bool:
        if not self.closed:
            self.closed = True
            self.close_code = 1000
            self._queue.put_nowait(None)
        return True

    def __aiter__(self):
        return self

    async def __anext__(self):
        msg = await self._queue.get()
        if msg is None:
            raise StopAsyncIteration
        return msg


class WebSocketFactory:
    """Replaces ClientSession.ws_connect; every call gives a new FakeWebSocket."""

    def __init__(self) -> None:
        self.sockets: list[FakeWebSocket] = []
        self.urls: list[str] = []
        self.fail: Exception | None = None

    async def __call__(self, _session: ClientSession, url: str, **kwargs: Any) -> FakeWebSocket:
        self.urls.append(url)
        if self.fail is not None:
            raise self.fail
        ws = FakeWebSocket()
        self.sockets.append(ws)
        return ws

    @property
    def current(self) -> FakeWebSocket:
        return self.sockets[-1]

    async def wait_for(self, count: int, timeout: float = 2) -> FakeWebSocket:
        async with asyncio.timeout(timeout):
            while len(self.sockets) < count:
                await asyncio.sleep(0.01)
        return self.sockets[count - 1]


@pytest.fixture
def ws_factory() -> AsyncGenerator[WebSocketFactory]:
    factory = WebSocketFactory()
    with patch.object(ClientSession, "ws_connect", lambda session, url, **kw: factory(session, url, **kw)):
        yield factory


@pytest.fixture
def mock_cloud(aioclient_mock: AiohttpClientMocker) -> Callable[..., AiohttpClientMocker]:
    """Register the happy-path cloud answers; tests may override before calling."""

    def _setup(
        devices: str = "devices_page.json",
        login: dict | None = None,
        login_status: int = 200,
    ) -> AiohttpClientMocker:
        aioclient_mock.post(LOGIN_URL, json=login or load_fixture("login.json"), status=login_status)
        aioclient_mock.post(REFRESH_URL, json=load_fixture("login.json"))
        aioclient_mock.get(DEVICES_URL, json=load_fixture(devices))
        aioclient_mock.get(status_url(MAC_1), json=load_fixture("status_as25php3hra.json"))
        aioclient_mock.get(status_url(MAC_2), json=load_fixture("status_as70s2sf2fa.json"))
        return aioclient_mock

    return _setup


@pytest.fixture
def config_entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=C.DOMAIN,
        title=f"{EMAIL} (ru)",
        unique_id=f"{EMAIL}_ru",
        data={"email": EMAIL, "password": PASSWORD, "region": "ru"},
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
async def setup_entry(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_cloud, ws_factory
) -> AsyncGenerator[MockConfigEntry]:
    mock_cloud()
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    await ws_factory.wait_for(1)
    await hass.async_block_till_done()
    yield config_entry
    await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()

