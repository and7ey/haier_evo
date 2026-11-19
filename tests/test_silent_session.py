"""A session that goes silent long after it opened is replaced (WS_QUIET_REOPEN).

Seen on a live account: a washer switched on after a day off pushed nothing
(not even ONLINE) until the integration was reloaded; for a day before that
the air conditioners changed only at the periodic reads, never by a push.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.haier_evo import const as C
from custom_components.haier_evo.api import Haier, HaierAC


@pytest.fixture
def hub(hass):
    clock = SimpleNamespace(now=1000.0)
    client = SimpleNamespace(async_get_device_status=AsyncMock(return_value={}))
    hub = Haier(hass, client, Mock())
    hub._now = lambda: clock.now
    hub._last_message_at = clock.now
    device = Mock(spec=HaierAC)
    device.device_id = "00:00:5e:00:53:01"
    device.device_name = "AC"
    device.synced = True
    device.changed_at = 0
    device.absolute_commands = True
    device.described_differently.return_value = False
    device.raw_values = {"36": "27.5"}
    rest = {"value": "27.5"}

    def apply(_status, **_kwargs):
        device.raw_values = {"36": rest["value"]}

    device.update_from_backend.side_effect = apply
    hub.devices = [device]
    ws = SimpleNamespace(closed=False, close=AsyncMock())
    hub._ws = ws
    return hub, rest, ws, clock


def receive(hub, clock):
    """What the session does with a text frame."""
    hub._ws_messages += 1
    hub._last_message_at = clock.now


async def test_a_change_not_pushed_reopens_a_quiet_session(hass, hub):
    hub, rest, ws, clock = hub
    clock.now += 900
    rest["value"] = "28.0"
    await hub._async_periodic_refresh()
    await hass.async_block_till_done()
    ws.close.assert_awaited_once()
    assert not hub._connected.is_set()


async def test_a_change_while_pushes_come_is_no_silence(hass, hub):
    hub, rest, ws, clock = hub
    clock.now += 600
    receive(hub, clock)  # a push, then a change the REST read finds (format, lag)
    clock.now += 300
    rest["value"] = "28.0"
    await hub._async_periodic_refresh()
    await hass.async_block_till_done()
    ws.close.assert_not_awaited()


async def test_nothing_changed_and_nothing_came_for_an_hour(hass, hub):
    hub, _rest, ws, clock = hub
    for _ in range(3):
        clock.now += 900
        await hub._async_periodic_refresh()
        await hass.async_block_till_done()
        ws.close.assert_not_awaited()
    clock.now = 1000.0 + C.WS_QUIET_REOPEN
    await hub._async_periodic_refresh()
    await hass.async_block_till_done()
    ws.close.assert_awaited_once()
    # the hour counts anew from the reopening
    hub._ws = ws = SimpleNamespace(closed=False, close=AsyncMock())
    clock.now += 900
    await hub._async_periodic_refresh()
    await hass.async_block_till_done()
    ws.close.assert_not_awaited()


async def test_no_session_nothing_to_reopen(hass, hub):
    hub, rest, ws, clock = hub
    ws.closed = True
    clock.now += C.WS_QUIET_REOPEN
    rest["value"] = "28.0"
    await hub._async_periodic_refresh()
    await hass.async_block_till_done()
    ws.close.assert_not_awaited()
