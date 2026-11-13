"""Start is sent once; native reconnects and retries of safe commands still work."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import timedelta
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.haier_evo import api
from custom_components.haier_evo import const as C  # noqa: N812
from custom_components.haier_evo.device_laundry import START_PENDING_TIMEOUT, laundry_codes

from . import test_devices as base
from .conftest import status_url


def idle_account():
    dump = deepcopy(base.LAUNDRY)
    for item in dump["devices"]:
        data = item["backend_data"]
        codes = laundry_codes(data)
        data["program"] = {"title": "Mixed", "status": "FINISHED"}
        values = {codes.get("onOffStatus"): "1", codes.get("remoteCtrValid"): "1", codes.get("cyclePhase"): "0"}
        for attribute in api.device_part(data)["attributes"]:
            if str(attribute.get("name")) in values:
                attribute["currentValue"] = values[str(attribute["name"])]
    return dump


async def prepare(hass, aioclient_mock, ws_factory, mac=None):
    entry = await base.start(hass, aioclient_mock, ws_factory, idle_account(), {"laundry_control": True})
    device = base.device_by_mac(entry, mac or base.HW90["device_id"])
    device.set_selected_program(device.programs[0]["title"])
    return entry, device


def sent(factory, action):
    return [message for socket in factory.sockets for message in socket.sent if message.get("action") == action]


async def wait_for(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.001)


@pytest.mark.parametrize("mac", [base.HW90["device_id"], base.HD80["device_id"]])
async def test_lost_start_ack_closes_laundry_only_socket_without_replaying(hass, aioclient_mock, ws_factory, mac):
    entry, device = await prepare(hass, aioclient_mock, ws_factory, mac)
    try:
        old_socket = ws_factory.current
        before = len(aioclient_mock.mock_calls)
        with patch.object(C, "WS_ACK_TIMEOUT", 0.05), patch.object(C, "WS_COMMAND_WAIT", 0):
            await device.start()
            await ws_factory.wait_for(2)
            await wait_for(lambda: entry.runtime_data.lost_commands == 1)
            await hass.async_block_till_done()
            assert old_socket.closed
            assert len(sent(ws_factory, "operation")) == 1
            assert ws_factory.current.sent == []
            assert len(aioclient_mock.mock_calls) > before
            assert not device._start_pending  # the watchdog read a fresh idle REST snapshot
            await device.start()
            assert len(sent(ws_factory, "operation")) == 2  # an explicit manual retry
            payload = sent(ws_factory, "operation")[-1]
            ws_factory.current.feed({"event": "command_response", "trace": payload["trace"], "errNo": 0})
    finally:
        await base.stop(hass, entry)


async def test_uncertain_start_write_is_not_retried(hass, aioclient_mock, ws_factory):
    entry, device = await prepare(hass, aioclient_mock, ws_factory)
    try:
        old_socket = ws_factory.current
        send = old_socket.send_str

        async def uncertain_write(data):
            await send(data)
            raise ConnectionResetError("the bytes may have reached the cloud")

        with patch.object(old_socket, "send_str", side_effect=uncertain_write):
            with pytest.raises(HomeAssistantError):
                await device.start()
        await ws_factory.wait_for(2)
        assert old_socket.closed
        assert len(sent(ws_factory, "operation")) == 1
        assert ws_factory.current.sent == []
        with pytest.raises(HomeAssistantError) as error:
            await device.start()
        assert error.value.translation_key == "laundry_start_pending"
    finally:
        await base.stop(hass, entry)


async def test_double_click_during_the_start_write_sends_one_operation(hass, aioclient_mock, ws_factory):
    entry, device = await prepare(hass, aioclient_mock, ws_factory)
    task = None
    try:
        task = asyncio.create_task(device.start())
        await wait_for(lambda: bool(sent(ws_factory, "operation")))
        with pytest.raises(HomeAssistantError) as error:
            await device.start()
        assert error.value.translation_key == "laundry_start_pending"
        payload = sent(ws_factory, "operation")[0]
        ws_factory.current.feed({"event": "command_response", "trace": payload["trace"], "errNo": 0})
        await task
        assert len(sent(ws_factory, "operation")) == 1
    finally:
        if task is not None and not task.done():
            task.cancel()
        await base.stop(hass, entry)

@pytest.mark.parametrize("path", ["watchdog", "rejection"])
@pytest.mark.parametrize("mac", [base.HW90["device_id"], base.HD80["device_id"]])
async def test_late_idle_readback_keeps_a_newer_running_push(hass, aioclient_mock, ws_factory, path, mac):
    entry, device = await prepare(hass, aioclient_mock, ws_factory, mac)
    task = None
    entered, release, returned = asyncio.Event(), asyncio.Event(), asyncio.Event()
    idle = deepcopy(device.status_data)

    async def late_idle(_mac):
        entered.set()
        await release.wait()
        returned.set()
        return deepcopy(idle)

    try:
        with (
            patch.object(entry.runtime_data.client, "async_get_device_status", AsyncMock(side_effect=late_idle)),
            patch.object(C, "WS_ACK_TIMEOUT", 0.05 if path == "watchdog" else 600),
            patch.object(C, "WS_COMMAND_WAIT", 0 if path == "watchdog" else 2),
        ):
            if path == "watchdog":
                await device.start()
            else:
                task = asyncio.create_task(device.start())
                await wait_for(lambda: bool(sent(ws_factory, "operation")))
                payload = sent(ws_factory, "operation")[-1]
                ws_factory.current.feed({"event": "command_response", "trace": payload["trace"], "errNo": 7})
            await entered.wait()
            device.on_message({
                "event": "ProgramDryerBaseEvent" if device.is_dryer else "ProgramWasherBaseEvent",
                "data": {"program": {"title": "Mixed", "status": "IN_PROGRESS"}},
            })
            assert device.state == "running"
            release.set()
            if task is not None:
                with pytest.raises(HomeAssistantError) as error:
                    await task
                assert error.value.translation_key == "command_rejected"
            await returned.wait()
            await hass.async_block_till_done()
            assert device.state == "running"
            assert not device._start_pending
            with pytest.raises(HomeAssistantError) as error:
                await device.start()
            assert error.value.translation_key == "laundry_cannot_start"
            assert len(sent(ws_factory, "operation")) == 1
    finally:
        release.set()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await base.stop(hass, entry)


async def test_old_inflight_rest_cannot_unlock_start_but_new_idle_rest_can(hass, aioclient_mock, ws_factory):
    entry, device = await prepare(hass, aioclient_mock, ws_factory)
    task = None
    try:
        entered, release = asyncio.Event(), asyncio.Event()
        idle = deepcopy(device.status_data)

        async def old_read(_mac):
            entered.set()
            await release.wait()
            return deepcopy(idle)

        with patch.object(entry.runtime_data.client, "async_get_device_status", AsyncMock(side_effect=old_read)) as read:
            task = asyncio.create_task(entry.runtime_data._async_resync([device]))
            await entered.wait()
            await device.start()
            with pytest.raises(HomeAssistantError):
                await device.start()
            release.set()
            await task
            with pytest.raises(HomeAssistantError):
                await device.start()
            assert len(sent(ws_factory, "operation")) == 1
            read.side_effect = None
            read.return_value = deepcopy(idle)
            await entry.runtime_data._async_resync([device])
            await device.start()
            assert len(sent(ws_factory, "operation")) == 2
    finally:
        if task is not None and not task.done():
            task.cancel()
        await base.stop(hass, entry)


async def test_pending_start_expires_without_automatically_sending_anything(hass, aioclient_mock, ws_factory):
    entry, device = await prepare(hass, aioclient_mock, ws_factory)
    try:
        with patch.object(C, "WS_ACK_TIMEOUT", 600):
            await device.start()
            async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=START_PENDING_TIMEOUT - 1))
            await hass.async_block_till_done()
            with pytest.raises(HomeAssistantError):
                await device.start()
            async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=START_PENDING_TIMEOUT + 1))
            await hass.async_block_till_done()
            assert len(sent(ws_factory, "operation")) == 1
            await device.start()
            assert len(sent(ws_factory, "operation")) == 2
    finally:
        await base.stop(hass, entry)


async def test_unload_cancels_the_pending_start_timer(hass, aioclient_mock, ws_factory):
    entry, device = await prepare(hass, aioclient_mock, ws_factory)
    with patch.object(C, "WS_ACK_TIMEOUT", 600):
        await device.start()
        listener = Mock()
        device.add_listener(listener)
        await base.stop(hass, entry)
        listener.reset_mock()
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=START_PENDING_TIMEOUT + 1))
        await hass.async_block_till_done()
        listener.assert_not_called()
        assert device._cancel_start_pending is None
        assert len(sent(ws_factory, "operation")) == 1


@pytest.mark.parametrize("action,status", [("pause", "IN_PROGRESS"), ("resume", "PAUSED"), ("cancel", "IN_PROGRESS")])
@pytest.mark.parametrize("failure", ["ack", "write"])
async def test_safe_laundry_actions_keep_native_retry(hass, aioclient_mock, ws_factory, action, status, failure):
    entry, device = await prepare(hass, aioclient_mock, ws_factory)
    try:
        device.on_message({"event": "ProgramWasherBaseEvent", "data": {"program": {"title": "Mixed", "status": status}}})
        old_socket = ws_factory.current
        if failure == "write":
            old_socket.fail_send = True
        with patch.object(C, "WS_ACK_TIMEOUT", 0.05), patch.object(C, "WS_COMMAND_WAIT", 0):
            await getattr(device, action)()
            await ws_factory.wait_for(2)
            await wait_for(lambda: bool(ws_factory.current.sent))
            first_command = old_socket.sent[-1] if failure == "ack" else ws_factory.current.sent[-1]
            assert ws_factory.current.sent[-1]["command"] == device.actions[action]
            assert len(sent(ws_factory, "command")) == (2 if failure == "ack" else 1)
            assert not sent(ws_factory, "operation")
            ws_factory.current.feed({"event": "command_response", "trace": first_command["trace"], "errNo": 0})
            await asyncio.sleep(0.07)
    finally:
        await base.stop(hass, entry)


async def test_nonzero_start_ack_is_not_success_when_parameters_already_match(hass, aioclient_mock, ws_factory):
    entry, device = await prepare(hass, aioclient_mock, ws_factory)
    task = None
    try:
        idle = deepcopy(device.status_data)
        attributes = api.device_part(idle)["attributes"]
        program = next(p for p in device.programs if p["title"] == device.selected_program)
        for command in program["commands"]:
            attr = next((a for a in attributes if str(a.get("name")) == command["commandName"]), None)
            if attr is None:
                attributes.append({"name": command["commandName"], "currentValue": command["value"]})
            else:
                attr["currentValue"] = command["value"]
        aioclient_mock.get(status_url(device.device_id), json=idle)
        task = asyncio.create_task(device.start())
        await wait_for(lambda: bool(sent(ws_factory, "operation")))
        payload = sent(ws_factory, "operation")[-1]
        assert api.command_applied(payload, idle)
        ws_factory.current.feed({"event": "command_response", "trace": payload["trace"], "errNo": 7})
        with pytest.raises(HomeAssistantError) as error:
            await task
        assert error.value.translation_key == "command_rejected"
        assert not device._start_pending
        assert len(sent(ws_factory, "operation")) == 1
    finally:
        if task is not None and not task.done():
            task.cancel()
        await base.stop(hass, entry)


@pytest.mark.parametrize("mac", [base.HW90["device_id"], base.HD80["device_id"]])
async def test_start_not_written_can_be_retried_immediately(hass, aioclient_mock, ws_factory, mac):
    entry, device = await prepare(hass, aioclient_mock, ws_factory, mac)
    try:
        with patch.object(entry.runtime_data, "_async_wait_connected", AsyncMock(return_value=None)):
            with pytest.raises(HomeAssistantError) as error:
                await device.start()
        assert error.value.translation_key == "command_failed"
        assert not device._start_pending
        assert device._cancel_start_pending is None
        assert not entry.runtime_data._pending
        assert not entry.runtime_data._waiters
        assert not sent(ws_factory, "operation")
        with patch.object(C, "WS_ACK_TIMEOUT", 600), patch.object(C, "WS_COMMAND_WAIT", 0):
            await device.start()
        assert len(sent(ws_factory, "operation")) == 1
        assert device._start_pending
    finally:
        await base.stop(hass, entry)
