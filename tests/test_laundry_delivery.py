"""Laundry actions are never duplicated after ambiguous delivery."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from unittest.mock import patch

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.haier_evo import api
from custom_components.haier_evo import const as C

from . import test_devices as base
from .conftest import status_url


async def prepare(hass, aioclient_mock, ws_factory):
    entry = await base.start(hass, aioclient_mock, ws_factory, base.LAUNDRY, {"laundry_control": True})
    device = base.device_by_mac(entry, base.HW90["device_id"])
    device._raw[device.codes["onOffStatus"]] = "1"
    device._raw[device.codes["remoteCtrValid"]] = "1"
    device._raw[device.codes["cyclePhase"]] = "0"
    device.program_status = "FINISHED"
    device.set_selected_program("Смешанная")
    return entry, device


def operations(factory):
    return [message for socket in factory.sockets for message in socket.sent
            if message.get("action") == "operation"]


async def test_lost_ack_reads_state_without_repeating_start(hass, aioclient_mock, ws_factory):
    entry, device = await prepare(hass, aioclient_mock, ws_factory)
    try:
        idle = deepcopy(base.HW90["backend_data"])
        for attr in idle["smartDeviceControl"]["attributes"]:
            if str(attr.get("name")) == device.codes["onOffStatus"]:
                attr["currentValue"] = "1"
            elif str(attr.get("name")) == device.codes["cyclePhase"]:
                attr["currentValue"] = "0"
        device._program_block(idle)["status"] = "FINISHED"
        aioclient_mock.clear_requests()
        aioclient_mock.get(status_url(device.device_id), json=idle)
        before = len(aioclient_mock.mock_calls)
        with patch.object(C, "WS_ACK_TIMEOUT", 0.01), patch.object(C, "WS_COMMAND_WAIT", 0):
            await device.start()
            await asyncio.sleep(0.05)
            await hass.async_block_till_done()
        assert len(operations(ws_factory)) == 1
        assert entry.runtime_data._pending == {}
        assert entry.runtime_data.lost_commands == 1
        assert len(aioclient_mock.mock_calls) > before
        assert device._start_pending  # an idle snapshot is not proof the queued start was refused
    finally:
        await base.stop(hass, entry)


async def test_write_error_after_transmission_does_not_repeat_action(hass, aioclient_mock, ws_factory):
    entry, device = await prepare(hass, aioclient_mock, ws_factory)
    try:
        socket = ws_factory.current
        send = socket.send_str

        async def uncertain_write(data):
            await send(data)
            raise ConnectionResetError("the write may have reached the cloud")

        with patch.object(socket, "send_str", side_effect=uncertain_write):
            with pytest.raises(HomeAssistantError):
                await device.start()
        await hass.async_block_till_done()
        assert len(operations(ws_factory)) == 1
    finally:
        await base.stop(hass, entry)


async def test_rejected_start_is_not_success_just_because_parameters_match(hass, aioclient_mock, ws_factory):
    entry, device = await prepare(hass, aioclient_mock, ws_factory)
    call = None
    try:
        snapshot = deepcopy(base.HW90["backend_data"])
        attributes = snapshot["smartDeviceControl"]["attributes"]
        for command in device._start_commands(next(p for p in device.programs if p["title"] == device.selected_program)):
            attr = next((a for a in attributes if str(a.get("name")) == command["commandName"]), None)
            if attr is None:
                attributes.append({"name": command["commandName"], "currentValue": command["value"]})
            else:
                attr["currentValue"] = command["value"]
        aioclient_mock.clear_requests()
        aioclient_mock.get(status_url(device.device_id), json=snapshot)
        call = asyncio.create_task(device.start())
        async with asyncio.timeout(2):
            while not operations(ws_factory):
                await asyncio.sleep(0.001)
        payload = operations(ws_factory)[-1]
        assert api.command_applied(payload, snapshot)
        ws_factory.current.feed({"event": "command_response", "trace": payload["trace"],
                                 "macAddress": device.device_id, "errNo": 7})
        with pytest.raises(HomeAssistantError):
            await call
        assert len(operations(ws_factory)) == 1
        assert not device._start_pending
    finally:
        if call is not None and not call.done():
            call.cancel()
        await base.stop(hass, entry)
