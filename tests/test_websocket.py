"""WebSocket: status parsing, commands, reconnects, availability."""
from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import patch

import pytest
from aiohttp import ClientError
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.haier_evo import const as C
from custom_components.haier_evo.api import Haier

from .conftest import MAC_1, status_url

CLIMATE_UID = f"{MAC_1}_AS25PHP3HRA"


def climate_id(hass: HomeAssistant) -> str:
    return er.async_get(hass).async_get_entity_id("climate", C.DOMAIN, CLIMATE_UID)


def status_message(properties: dict) -> dict:
    return {
        "event": "status",
        "macAddress": MAC_1.upper(),  # case must not matter
        "payload": {"statuses": [{"properties": properties}]},
    }


async def test_status_message_updates_state(hass, setup_entry, ws_factory):
    ws_factory.current.feed(status_message({"0": "19", "2": "4", "36": "20.5", "4": "1"}))
    await hass.async_block_till_done()
    state = hass.states.get(climate_id(hass))
    assert state.state == "heat"
    assert state.attributes["temperature"] == 19
    assert state.attributes["current_temperature"] == 20.5
    assert state.attributes["fan_mode"] == "high"


async def test_unknown_mode_is_not_none_string(hass, setup_entry, ws_factory, caplog):
    """#61/#73: an unmapped mode code gave "'None' is not a valid HVACMode"."""
    ws_factory.current.feed(status_message({"2": "99", "4": "77"}))
    await hass.async_block_till_done()
    state = hass.states.get(climate_id(hass))
    assert state.state == "unknown"
    assert state.attributes["fan_mode"] is None
    assert "is not a valid HVACMode" not in caplog.text
    # and the entity keeps working afterwards
    ws_factory.current.feed(status_message({"2": "1"}))
    await hass.async_block_till_done()
    assert hass.states.get(climate_id(hass)).state == "cool"


async def test_broken_messages_do_not_kill_the_socket(hass, setup_entry, ws_factory, caplog):
    ws = ws_factory.current
    ws.feed("not json")
    ws.feed({"event": "status", "macAddress": MAC_1, "payload": {"statuses": []}})
    ws.feed({"event": "status", "macAddress": MAC_1, "payload": {"statuses": [{}]}})
    ws.feed({"event": "status", "macAddress": "ff:ff:ff:ff:ff:ff", "payload": {}})
    ws.feed({"event": "somethingNew", "macAddress": MAC_1})
    ws.feed(status_message({"36": ""}))
    ws.feed(status_message({"0": "21"}))
    await hass.async_block_till_done()
    assert hass.states.get(climate_id(hass)).attributes["temperature"] == 21
    assert len(ws_factory.sockets) == 1
    assert "Traceback" not in caplog.text


async def test_device_offline_event(hass, setup_entry, ws_factory):
    """OFFLINE shows on the connectivity sensor; the entities stay available, as in 1.4+."""
    device = setup_entry.runtime_data.get_device_by_id(MAC_1)
    ws_factory.current.feed({"event": "deviceStatusEvent", "macAddress": MAC_1, "payload": {"status": "OFFLINE"}})
    await hass.async_block_till_done()
    assert not device.connected
    assert hass.states.get(climate_id(hass)).state == "cool"
    ws_factory.current.feed({"event": "deviceStatusEvent", "macAddress": MAC_1, "payload": {"status": "ONLINE"}})
    await hass.async_block_till_done()
    assert device.connected


async def test_set_hvac_mode_sends_group_command(hass, setup_entry, ws_factory):
    ws = ws_factory.current
    ws.sent.clear()
    await hass.services.async_call(
        "climate", "set_hvac_mode", {"entity_id": climate_id(hass), "hvac_mode": "heat"}, blocking=True
    )
    assert len(ws.sent) == 1
    sent = ws.sent[0]
    assert sent["action"] == "operation"
    assert sent["macAddress"] == MAC_1
    assert sent["commandName"] == "4"
    assert {"commandName": "2", "value": "4"} in sent["commands"]
    assert hass.states.get(climate_id(hass)).state == "heat"


async def test_set_temperature_sends_integer(hass, setup_entry, ws_factory):
    ws = ws_factory.current
    ws.sent.clear()
    await hass.services.async_call(
        "climate", "set_temperature", {"entity_id": climate_id(hass), "temperature": 23}, blocking=True
    )
    assert ws.sent[0]["commands"] == [{"commandName": "0", "value": "23"}]


async def test_turn_off(hass, setup_entry, ws_factory):
    ws = ws_factory.current
    ws.sent.clear()
    await hass.services.async_call("climate", "turn_off", {"entity_id": climate_id(hass)}, blocking=True)
    assert ws.sent[0]["commands"] == [{"commandName": "21", "value": "0"}]
    assert hass.states.get(climate_id(hass)).state == "off"


async def test_unsupported_fan_mode_sends_nothing(hass, setup_entry, ws_factory):
    """Used to send the literal value "None" to the unit."""
    ws = ws_factory.current
    ws.sent.clear()
    haier: Haier = setup_entry.runtime_data
    await haier.devices[0].set_fan_mode("turbo-max")
    assert ws.sent == []


async def test_command_after_server_closed_idle_socket(hass, setup_entry, ws_factory):
    """#89/#102: the first command after idle went into a dead socket."""
    first = ws_factory.current
    first.server_close()
    await hass.async_block_till_done()
    await hass.services.async_call(
        "climate", "set_temperature", {"entity_id": climate_id(hass), "temperature": 25}, blocking=True
    )
    second = await ws_factory.wait_for(2)
    assert first.sent == []
    assert second.sent[-1]["commands"] == [{"commandName": "0", "value": "25"}]


async def test_failed_write_is_resent_on_new_socket(hass, setup_entry, ws_factory):
    """Half-open socket: the write itself fails, the command must not be lost."""
    first = ws_factory.current
    first.fail_send = True
    await hass.services.async_call(
        "climate", "set_temperature", {"entity_id": climate_id(hass), "temperature": 26}, blocking=True
    )
    second = await ws_factory.wait_for(2)
    assert second.sent[-1]["commands"] == [{"commandName": "0", "value": "26"}]


async def test_no_cloud_fails_loudly(hass, setup_entry, ws_factory):
    """No silent success: the service call raises when the command cannot be sent."""
    ws_factory.fail = ClientError("cloud down")
    ws_factory.current.server_close()
    await hass.async_block_till_done()
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "climate", "set_temperature", {"entity_id": climate_id(hass), "temperature": 22}, blocking=True
        )
    # the old state is kept, not the one we failed to send
    assert hass.states.get(climate_id(hass)).attributes["temperature"] == 24
    ws_factory.fail = None
    await ws_factory.wait_for(2)


async def test_reconnects_after_close(hass, setup_entry, ws_factory):
    ws_factory.current.server_close()
    await ws_factory.wait_for(2)
    assert setup_entry.runtime_data.reconnects == 1


async def test_entities_unavailable_after_long_outage_and_resync(
    hass, setup_entry, ws_factory, aioclient_mock
):
    """#103: when the Haier cloud is down, entities must say so."""
    haier: Haier = setup_entry.runtime_data
    now = haier._now()
    ws_factory.fail = ClientError("cloud down")
    ws_factory.current.server_close()
    await hass.async_block_till_done()
    assert hass.states.get(climate_id(hass)).state == "cool"  # short outage: keep state

    with patch.object(Haier, "_now", return_value=now + C.WS_UNAVAILABLE_AFTER + 1):
        await asyncio.sleep(0.1)  # let the reconnect loop notify entities
        await hass.async_block_till_done()
        assert hass.states.get(climate_id(hass)).state == "unavailable"

        calls_before = sum(1 for c in aioclient_mock.mock_calls if status_url(MAC_1) in str(c[1]))
        ws_factory.fail = None
        await ws_factory.wait_for(2)
        await hass.async_block_till_done()
        await asyncio.sleep(0.05)
        await hass.async_block_till_done()
        assert hass.states.get(climate_id(hass)).state == "cool"
        calls_after = sum(1 for c in aioclient_mock.mock_calls if status_url(MAC_1) in str(c[1]))
        assert calls_after == calls_before + 1  # statuses re-read over REST


async def test_handshake_401_refreshes_token(hass, setup_entry, ws_factory, aioclient_mock):
    from aiohttp import RequestInfo, WSServerHandshakeError
    from multidict import CIMultiDict, CIMultiDictProxy
    from yarl import URL

    info = RequestInfo(URL("wss://x/ws/secret"), "GET", CIMultiDictProxy(CIMultiDict()), URL("wss://x/ws/secret"))
    setup_entry.runtime_data.client._token_issued -= 60  # not one issued seconds ago
    ws_factory.fail = WSServerHandshakeError(info, (), status=401)
    ws_factory.current.server_close()
    await asyncio.sleep(0.05)
    ws_factory.fail = None
    await ws_factory.wait_for(2)
    assert any(str(c[1]).endswith("/auth/refresh") for c in aioclient_mock.mock_calls)


async def test_command_response_confirms_command(hass, setup_entry, ws_factory):
    haier: Haier = setup_entry.runtime_data
    ws = ws_factory.current
    with patch.object(C, "WS_ACK_TIMEOUT", 0.3):
        await hass.services.async_call(
            "climate", "set_temperature", {"entity_id": climate_id(hass), "temperature": 22}, blocking=True
        )
        trace = ws.sent[-1]["trace"]
        ws.feed({"event": "command_response", "macAddress": MAC_1, "trace": trace, "errNo": 0})
        await asyncio.sleep(0.5)
        await hass.async_block_till_done()
    assert haier._pending == {}
    assert len(ws_factory.sockets) == 1  # nothing re-sent
    assert sum(1 for m in ws.sent if m["trace"] == trace) == 1


async def test_unconfirmed_command_is_not_resent_over_a_newer_one(hass, setup_entry, ws_factory):
    """22 °C unconfirmed, 24 °C sent since: re-sending 22 would leave the unit at 22, the UI at 24."""
    first = ws_factory.current
    with patch.object(C, "WS_ACK_TIMEOUT", 0.2):
        for temperature in (22, 24):
            await hass.services.async_call(
                "climate", "set_temperature", {"entity_id": climate_id(hass), "temperature": temperature},
                blocking=True,
            )
        old, new = first.sent[-2]["trace"], first.sent[-1]["trace"]
        second = await ws_factory.wait_for(2)
        async with asyncio.timeout(2):
            while not second.sent:
                await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        assert [m["trace"] for m in second.sent] == [new]  # only the newer one is re-sent
        assert second.sent[-1]["commands"] == [{"commandName": "0", "value": "24"}]
        second.feed({"event": "command_response", "macAddress": MAC_1, "trace": new})
        await asyncio.sleep(0.3)
        await hass.async_block_till_done()
    assert old not in setup_entry.runtime_data._pending


async def test_command_to_another_attribute_does_not_stop_a_resend(hass, setup_entry, ws_factory):
    """Absolute values: a newer fan mode does not make the unconfirmed setpoint wrong."""
    haier: Haier = setup_entry.runtime_data
    sent = [{"action": "operation", "macAddress": MAC_1, "commandName": "4",
             "commands": [{"commandName": "0", "value": "22"}], "trace": "t-old"},
            {"action": "operation", "macAddress": MAC_1, "commandName": "4",
             "commands": [{"commandName": "5", "value": "2"}], "trace": "t-new"}]
    for payload in sent:
        haier._count_sent(payload["trace"], payload)
    assert not haier._superseded("t-old", sent[0])
    same = {**sent[1], "commands": [{"commandName": "0", "value": "24"}], "trace": "t-newer"}
    haier._count_sent("t-newer", same)
    assert haier._superseded("t-old", sent[0])


async def test_laundry_start_is_not_resent_after_a_cancel(hass):
    """Washer actions are not absolute: Start → Cancel → a re-sent Start would run the program."""
    from types import SimpleNamespace

    haier = Haier(hass, SimpleNamespace(), SimpleNamespace())
    washer = SimpleNamespace(device_id="aa:bb:cc:dd:ee:ff", absolute_commands=False)
    haier.devices = [washer]
    start = {"action": "operation", "macAddress": washer.device_id, "commandName": "start_group",
             "commands": [{"commandName": "10", "value": "1"}], "trace": "t-start"}
    cancel = {"action": "command", "macAddress": washer.device_id,
              "command": {"commandName": "11", "value": "1"}, "trace": "t-cancel"}
    haier._count_sent("t-start", start)
    haier._count_sent("t-cancel", cancel)
    assert haier._superseded("t-start", start)


async def test_unconfirmed_command_is_resent_once(hass, setup_entry, ws_factory, caplog):
    """The silent session: writes succeed, nothing comes back (#89)."""
    haier: Haier = setup_entry.runtime_data
    first = ws_factory.current
    with patch.object(C, "WS_ACK_TIMEOUT", 0.05):
        await hass.services.async_call(
            "climate", "set_temperature", {"entity_id": climate_id(hass), "temperature": 27}, blocking=True
        )
        trace = first.sent[-1]["trace"]
        second = await ws_factory.wait_for(2)
        async with asyncio.timeout(2):
            while not second.sent:
                await asyncio.sleep(0.01)
        assert second.sent[-1]["trace"] == trace
        second.feed({"event": "command_response", "macAddress": MAC_1, "trace": trace})
        await asyncio.sleep(0.15)
        await hass.async_block_till_done()
    assert haier.lost_commands == 0
    assert "did not confirm a command in" in caplog.text


async def test_two_unconfirmed_commands_reconnect_once(hass, setup_entry, ws_factory):
    """The second watchdog must not tear down the socket the first one just opened."""
    haier: Haier = setup_entry.runtime_data
    first = ws_factory.current
    with patch.object(C, "WS_ACK_TIMEOUT", 0.1):
        # two attributes: a second setpoint would supersede the first one instead
        await hass.services.async_call(
            "climate", "set_temperature", {"entity_id": climate_id(hass), "temperature": 25}, blocking=True
        )
        await hass.services.async_call(
            "climate", "set_fan_mode", {"entity_id": climate_id(hass), "fan_mode": "low"}, blocking=True
        )
        traces = [m["trace"] for m in first.sent[-2:]]
        second = await ws_factory.wait_for(2)
        async with asyncio.timeout(2):
            while len(second.sent) < 2:
                await asyncio.sleep(0.01)
        for trace in traces:
            second.feed({"event": "command_response", "macAddress": MAC_1, "trace": trace})
        await asyncio.sleep(0.3)
        await hass.async_block_till_done()
    assert len(ws_factory.sockets) == 2
    assert not second.closed
    assert haier.lost_commands == 0


async def test_rejected_command_reads_state_back(hass, setup_entry, ws_factory, aioclient_mock):
    ws = ws_factory.current
    before = sum(1 for c in aioclient_mock.mock_calls if status_url(MAC_1) in str(c[1]))
    await hass.services.async_call(
        "climate", "set_temperature", {"entity_id": climate_id(hass), "temperature": 29}, blocking=True
    )
    ws.feed({"event": "command_response", "macAddress": MAC_1, "trace": ws.sent[-1]["trace"], "errNo": 5})
    await asyncio.sleep(0.05)
    await hass.async_block_till_done()
    after = sum(1 for c in aioclient_mock.mock_calls if status_url(MAC_1) in str(c[1]))
    assert after == before + 1
    assert hass.states.get(climate_id(hass)).attributes["temperature"] == 24


async def test_unavailable_without_any_new_event(hass, setup_entry, ws_factory):
    """After a credentials failure the loop stops: a timer still has to flip the entities."""
    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    from custom_components.haier_evo.client import HaierAuthError

    haier: Haier = setup_entry.runtime_data
    now = haier._now()
    with patch.object(haier.client, "async_get_token", side_effect=HaierAuthError("nope")):
        ws_factory.current.server_close()
        await asyncio.sleep(0.05)
        await hass.async_block_till_done()
        assert haier._task.done()
    assert hass.states.get(climate_id(hass)).state == "cool"
    with patch.object(Haier, "_now", return_value=now + C.WS_UNAVAILABLE_AFTER + 2):
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=C.WS_UNAVAILABLE_AFTER + 2))
        await hass.async_block_till_done()
        assert hass.states.get(climate_id(hass)).state == "unavailable"


async def test_lost_command_reads_state_back(hass, setup_entry, ws_factory, aioclient_mock):
    haier: Haier = setup_entry.runtime_data
    with patch.object(C, "WS_ACK_TIMEOUT", 0.05):
        before = sum(1 for c in aioclient_mock.mock_calls if status_url(MAC_1) in str(c[1]))
        await hass.services.async_call(
            "climate", "set_temperature", {"entity_id": climate_id(hass), "temperature": 28}, blocking=True
        )
        async with asyncio.timeout(3):
            while haier.lost_commands == 0:
                await asyncio.sleep(0.02)
        await hass.async_block_till_done()
    after = sum(1 for c in aioclient_mock.mock_calls if status_url(MAC_1) in str(c[1]))
    assert after == before + 1
    # the REST status says 24: HA shows the real value, not the lost 28
    assert hass.states.get(climate_id(hass)).attributes["temperature"] == 24


async def test_command_error_is_logged(hass, setup_entry, ws_factory, caplog):
    ws_factory.current.feed({"event": "command_response", "macAddress": MAC_1, "errNo": -1})
    await hass.async_block_till_done()
    assert "errNo=-1" in caplog.text


async def test_foreign_command_response_is_not_our_ack(hass, setup_entry, ws_factory, caplog):
    """The cloud echoes responses to app/Alice commands; they must not confirm ours."""
    haier: Haier = setup_entry.runtime_data
    ws = ws_factory.current
    with patch.object(C, "WS_ACK_TIMEOUT", 1):
        await hass.services.async_call(
            "climate", "set_temperature", {"entity_id": climate_id(hass), "temperature": 22}, blocking=True
        )
        trace = ws.sent[-1]["trace"]
        ws.feed({"event": "command_response", "macAddress": MAC_1, "trace": "from-the-app", "errNo": -1})
        await hass.async_block_till_done()
        assert trace in haier._pending
        assert "rejected a command" not in caplog.text
        ws.feed({"event": "command_response", "macAddress": MAC_1, "trace": trace, "errNo": 0})
        await hass.async_block_till_done()
    assert haier._pending == {}


async def test_quiet_and_turbo_follow_the_unit(hass, setup_entry, ws_factory):
    """#67: the "Тихий" switch never followed the unit."""
    device = setup_entry.runtime_data.devices[0]
    assert device.quiet_on is False
    ws_factory.current.feed(status_message({"20": "1"}))
    await hass.async_block_till_done()
    assert device.quiet_on is True
    assert hass.states.get(climate_id(hass)).attributes["preset_mode"] == "sleep"


async def test_turn_on_always_sends_status(hass, setup_entry, ws_factory):
    """The unit is on in HA already, but status=on still goes out (MaxSezin fork)."""
    ws = ws_factory.current
    ws.sent.clear()
    await hass.services.async_call(
        "climate", "set_hvac_mode", {"entity_id": climate_id(hass), "hvac_mode": "cool"}, blocking=True
    )
    assert {"commandName": "21", "value": "1"} in ws.sent[0]["commands"]


async def test_no_command_on_connect(hass, setup_entry, ws_factory):
    """#67: HA restart made every unit beep."""
    assert ws_factory.current.sent == []
    ws_factory.current.server_close()
    second = await ws_factory.wait_for(2)
    await hass.async_block_till_done()
    assert second.sent == []
