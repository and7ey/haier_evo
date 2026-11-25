"""HOQ-F6QSB4GBU discovery on the #128 diagnostics and commands on explicit app links."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from unittest.mock import patch

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr

from custom_components.haier_evo import const as C  # noqa: N812
from custom_components.haier_evo.device_ov import HaierOV

from .conftest import load_fixture
from .test_devices import device_by_mac, eid, start, stop

DUMP = load_fixture("oven_dump.json")
MAC = DUMP["devices"][0]["device_id"]
PREFIX = f"{MAC}_HOQ-F6QSB4GBU"


def push(ws, properties):
    ws.feed({"event": "status", "macAddress": MAC, "payload": {"statuses": [{"properties": properties}]}})


def control_account():
    """Simulate unredacted app links. The published diagnostics mask all four."""
    dump = deepcopy(DUMP)
    data = dump["devices"][0]["backend_data"]
    main = data["device"]["mainControl"]
    for key, code in (("power", "0"), ("remoteControlMode", "51"), ("lock", "2")):
        main[key]["link"] = {"type": "LIST", "name": code}
    data["device"]["monitoringScreen"]["temperature"]["link"] = {"type": "STEP", "name": "19"}
    data["smartDeviceControl"]["status"] = "ONLINE"
    return dump


async def test_oven_from_issue_128(hass, aioclient_mock, ws_factory):
    entry = await start(hass, aioclient_mock, ws_factory, DUMP)
    try:
        oven = device_by_mac(entry, MAC)
        assert isinstance(oven, HaierOV)
        device = dr.async_get(hass).async_get_device_by_identifier((C.DOMAIN, MAC), entry.entry_id)
        assert device.model == "HOQ-F6QSB4GBU" and device.sw_version == "1.5.3"

        def state(platform, suffix):
            return hass.states.get(eid(hass, platform, f"{PREFIX}_{suffix}"))

        assert state("binary_sensor", "connected").state == "off"
        assert state("sensor", "mode").state == "Выпечка"
        assert float(state("sensor", "target_temperature").state) == 180
        assert int(float(state("sensor", "remaining_time").state)) == 0
        assert not eid(hass, "switch", f"{PREFIX}_power")
        assert not eid(hass, "number", f"{PREFIX}_setpoint")
        push(ws_factory.current, {"5": "7", "6": "220", "11": "1", "12": "05"})
        await hass.async_block_till_done()
        assert state("sensor", "mode").state == "Пицца"
        assert float(state("sensor", "remaining_time").state) == 65
        assert float(state("sensor", "target_temperature").state) == 220
        push(ws_factory.current, {"5": "5", "6": "11", "12": "bad"})
        await hass.async_block_till_done()
        assert state("sensor", "mode").state == "Гриль"
        assert state("sensor", "target_temperature").state == "unknown"
        assert state("sensor", "remaining_time").state == "unknown"
    finally:
        await stop(hass, entry)


async def test_oven_controls_respect_device_state(hass, aioclient_mock, ws_factory):
    entry = await start(hass, aioclient_mock, ws_factory, control_account())
    try:
        oven = device_by_mac(entry, MAC)
        ws = ws_factory.current
        power = eid(hass, "switch", f"{PREFIX}_power")
        setpoint = eid(hass, "number", f"{PREFIX}_setpoint")
        assert float(hass.states.get(eid(hass, "sensor", f"{PREFIX}_current_temperature")).state) == 25
        assert hass.states.get(power).state == "unavailable"
        with pytest.raises(HomeAssistantError):
            await oven.set_power(True)
        push(ws, {"51": "1"})
        await hass.async_block_till_done()
        assert hass.states.get(power).state == "off"
        await hass.services.async_call("switch", "turn_on", {"entity_id": power}, blocking=True)
        assert ws.sent[-1]["command"] == {"commandName": "0", "value": "1"}
        assert hass.states.get(power).state == "off"  # waits for the oven's status

        push(ws, {"0": "1", "5": "48", "6": "70", "14": "3"})
        await hass.async_block_till_done()
        number = hass.states.get(setpoint)
        assert (number.attributes["min"], number.attributes["max"]) == (60, 90)
        assert float(number.state) == 70
        await hass.services.async_call("number", "set_value", {"entity_id": setpoint, "value": 80}, blocking=True)
        assert ws.sent[-1]["command"] == {"commandName": "6", "value": "80"}
        before = len(ws.sent)
        for value in (50, 100, 70.5):
            with pytest.raises(HomeAssistantError):
                await oven.set_temperature(value)
        for changes in ({"0": "0"}, {"0": "1", "2": "1"}, {"2": "0", "14": "0"},
                        {"14": "3", "51": "0"}, {"51": "1", "5": "88"}, {"5": "5"}):
            push(ws, changes)
            await hass.async_block_till_done()
            with pytest.raises(HomeAssistantError):
                await oven.set_temperature(80)
        assert len(ws.sent) == before
        await hass.services.async_call("switch", "turn_off", {"entity_id": power}, blocking=True)
        assert ws.sent[-1]["command"] == {"commandName": "0", "value": "0"}
    finally:
        await stop(hass, entry)


async def test_uncertain_oven_write_is_not_repeated(hass, aioclient_mock, ws_factory):
    entry = await start(hass, aioclient_mock, ws_factory, control_account())
    try:
        oven = device_by_mac(entry, MAC)
        ws = ws_factory.current
        push(ws, {"51": "1"})
        await hass.async_block_till_done()
        send = ws.send_str

        async def uncertain_write(data):
            await send(data)
            raise ConnectionResetError

        with patch.object(ws, "send_str", side_effect=uncertain_write), pytest.raises(HomeAssistantError):
            await oven.set_power(True)
        await ws_factory.wait_for(2)
        assert [message["command"] for socket in ws_factory.sockets for message in socket.sent] == [
            {"commandName": "0", "value": "1"}
        ]
    finally:
        await stop(hass, entry)


async def test_stale_or_missing_permission_never_authorizes_a_write(hass, aioclient_mock, ws_factory):
    dump = control_account()
    entry = await start(hass, aioclient_mock, ws_factory, dump)
    try:
        oven = device_by_mac(entry, MAC)
        ws = ws_factory.current
        push(ws, {"0": "1", "51": "1", "14": "3"})
        await hass.async_block_till_done()
        assert oven.can_control("target_temperature")

        oven.synced = False  # startup with a saved description
        push(ws, {"19": "100"})
        await hass.async_block_till_done()
        assert not oven.can_control("power")

        snapshot = dump["devices"][0]["backend_data"]
        oven.update_from_backend(snapshot)
        push(ws, {"51": "1", "14": "3", "0": "1"})
        await hass.async_block_till_done()
        assert oven.can_control("power")
        missing = deepcopy(snapshot)
        next(a for a in missing["smartDeviceControl"]["attributes"] if a["name"] == "51").pop("currentValue")
        oven.update_from_backend(missing)
        with pytest.raises(HomeAssistantError):
            await oven.set_power(True)

        push(ws, {"51": "1", "0": "1", "14": "3"})
        ws.feed({"event": "status", "macAddress": MAC, "payload": {"statuses": [{"alarms": ["masterTempSensorErr"]}]}})
        await hass.async_block_till_done()
        with pytest.raises(HomeAssistantError):
            await oven.set_temperature(180)
        assert ws.sent == []
    finally:
        await stop(hass, entry)


async def test_unacknowledged_temperature_is_read_back_without_replay(hass, aioclient_mock, ws_factory):
    entry = await start(hass, aioclient_mock, ws_factory, control_account())
    try:
        oven = device_by_mac(entry, MAC)
        push(ws_factory.current, {"51": "1", "0": "1", "14": "3"})
        await hass.async_block_till_done()
        with patch.object(C, "WS_ACK_TIMEOUT", 0.05), patch.object(C, "WS_COMMAND_WAIT", 0):
            await oven.set_temperature(190)
            await ws_factory.wait_for(2)
            async with asyncio.timeout(3):
                while entry.runtime_data.lost_commands == 0:
                    await asyncio.sleep(0.01)
            await hass.async_block_till_done()
        assert [message["command"] for ws in ws_factory.sockets for message in ws.sent] == [
            {"commandName": "6", "value": "190"}
        ]
        assert oven.target_temperature == 180
    finally:
        await stop(hass, entry)
