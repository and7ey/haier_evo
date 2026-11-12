"""Recover missing laundry state without polling disconnected appliances faster."""
from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.haier_evo import device as device_module
from custom_components.haier_evo import device_laundry as laundry_module
from custom_components.haier_evo.api import Haier
from custom_components.haier_evo.client import HaierApiError, HaierAuthError, HaierConnectionError, HaierRateLimitError
from custom_components.haier_evo.device_laundry import HaierLaundry

from .test_devices import LAUNDRY


@pytest.fixture
def polling(hass):
    clock = SimpleNamespace(now=1000)
    client = SimpleNamespace(async_get_device_status=AsyncMock(return_value={}))
    hub = Haier(hass, client, Mock())
    hub._now = lambda: clock.now
    devices = []
    for number in range(3):
        device = Mock(spec=HaierLaundry)
        device.device_id = f"00:00:5e:00:53:{number:02}"
        device.device_name = f"Laundry {number}"
        device.connected = True
        device.synced = True
        device.state = "running"
        device.changed_at = clock.now - 181
        device.laundry_updated_at = clock.now - 181
        device.absolute_commands = False
        device.described_differently.return_value = False
        devices.append(device)
    hub.devices = devices
    return hub, devices, client, clock


@pytest.mark.parametrize("state,connected,synced,age", [
    ("off", False, True, 181), ("idle", False, True, 181),
    ("finished", False, True, 181), ("cancelled", False, True, 181),
    ("running", False, True, 181), ("running", True, False, 181),
    ("running", True, True, 179), ("paused", True, True, 179),
])
async def test_offline_unsynced_or_fresh_push_do_not_add_rest(polling, state, connected, synced, age):
    hub, devices, client, clock = polling
    hub.devices = devices[:1]
    device = devices[0]
    device.state, device.connected, device.synced = state, connected, synced
    device.changed_at = clock.now - age
    device.laundry_updated_at = clock.now - age
    await hub.async_refresh_laundry()
    client.async_get_device_status.assert_not_awaited()


@pytest.mark.parametrize("state", ["running", "paused"])
async def test_one_request_per_tick_rotates_among_stale_active_devices(polling, state):
    hub, devices, client, clock = polling
    for device in devices:
        device.state = state
    for device in devices:
        await hub.async_refresh_laundry()
        assert client.async_get_device_status.await_args.args == (device.device_id,)
        clock.now += 30
    assert client.async_get_device_status.await_count == 3
    assert all(device.update_from_backend.call_count == 1 for device in devices)


async def test_later_push_wins_over_inflight_rest(polling):
    hub, devices, client, clock = polling
    hub.devices = devices[:1]
    entered, release = asyncio.Event(), asyncio.Event()

    async def read(_device_id):
        entered.set()
        await release.wait()
        return {}

    client.async_get_device_status.side_effect = read
    task = asyncio.create_task(hub.async_refresh_laundry())
    await entered.wait()
    devices[0].changed_at = clock.now + 1
    release.set()
    await task
    devices[0].update_from_backend.assert_not_called()
    devices[0].notify.assert_not_called()


@pytest.mark.parametrize("error", [HaierConnectionError("offline"), HaierApiError("refused")])
async def test_failed_reads_back_off_without_error_log(polling, caplog, error):
    hub, _devices, client, clock = polling
    client.async_get_device_status.side_effect = error
    for delay in (60, 120, 240, 480, 900, 900):
        await hub.async_refresh_laundry()
        count = client.async_get_device_status.await_count
        assert hub._laundry_retry_at == clock.now + delay
        await hub.async_refresh_laundry()
        assert client.async_get_device_status.await_count == count
        clock.now += delay
    assert not [record for record in caplog.records if record.levelno >= 40]


async def test_rate_limit_blocks_other_machines_until_retry_after(polling):
    hub, _devices, client, clock = polling
    client.async_get_device_status.side_effect = HaierRateLimitError(600)
    await hub.async_refresh_laundry()
    clock.now += 599
    await hub.async_refresh_laundry()
    assert client.async_get_device_status.await_count == 1
    clock.now += 1
    client.async_get_device_status.side_effect = None
    await hub.async_refresh_laundry()
    assert client.async_get_device_status.await_count == 2
    assert hub._laundry_failures == 0


async def test_auth_failure_enters_native_reauth_once_without_traceback(polling, caplog):
    hub, _devices, client, _clock = polling
    client.async_get_device_status.side_effect = HaierAuthError("reauth")
    hub._start_reauth = Mock(side_effect=lambda: setattr(hub, "_reauth_started", True))
    await hub.async_refresh_laundry()
    await hub.async_refresh_laundry()
    hub._start_reauth.assert_called_once_with()
    assert client.async_get_device_status.await_count == 1
    assert not [record for record in caplog.records if record.levelno >= 40 or record.exc_info]


async def test_entry_owned_poll_cancels_on_stop_and_does_not_overlap(hass, polling):
    hub, devices, client, _clock = polling
    entry = MockConfigEntry(domain="haier_evo")
    entry.add_to_hass(hass)
    hub._entry = entry
    entered = asyncio.Event()

    async def read(_device_id):
        entered.set()
        await asyncio.Event().wait()

    client.async_get_device_status.side_effect = read
    hub._schedule_laundry_refresh()
    task = hub._laundry_refresh_task
    await entered.wait()
    hub._schedule_laundry_refresh()
    assert hub._laundry_refresh_task is task
    await hub.async_refresh_laundry()
    assert client.async_get_device_status.await_count == 1
    await hub.async_stop()
    assert task.cancelled()
    hub._schedule_laundry_refresh()
    assert hub._laundry_refresh_task is None
    assert all(not device.update_from_backend.called for device in devices)


@pytest.fixture(params=[0, 1, 2], ids=["HW60", "HW90", "HD80"])
def real_polling(hass, monkeypatch, request):
    clock = SimpleNamespace(now=1000.0)
    timer = SimpleNamespace(monotonic=lambda: clock.now)
    monkeypatch.setattr(device_module, "time", timer)
    monkeypatch.setattr(laundry_module, "time", timer)
    row = LAUNDRY["devices"][request.param]
    status = deepcopy(row["backend_data"])
    status["program"] = {"status": "IN_PROGRESS", "title": "Synthetic programme"}
    client = SimpleNamespace(async_get_device_status=AsyncMock(return_value=status))
    hub = Haier(hass, client, Mock())
    hub._now = lambda: clock.now
    device = HaierLaundry(haier=hub, device_mac=row["device_id"], backend_data=deepcopy(status))
    device._apply_value(device.codes["onOffStatus"], "1")
    device._apply_value(device.codes["cyclePhase"], "2")
    hub.devices = [device]
    assert device.state == "running"
    assert device.laundry_updated_at == clock.now
    return hub, device, client, clock


def programme_event(device, data):
    return {"event": "ProgramDryerBaseEvent" if device.is_dryer else "ProgramWasherBaseEvent", "data": data}


def property_event(code, value):
    return {"event": "status", "payload": {"statuses": [{"properties": {code: value}}]}}


@pytest.mark.parametrize("noise", [
    "empty_programme", "title_only", "invalid_programme", "empty_status", "remote", "online", "link", "alarms",
    "unknown_property", "invalid_phase",
])
async def test_unrelated_push_cannot_postpone_cycle_fallback(real_polling, noise):
    hub, device, client, clock = real_polling
    packets = {
        "empty_programme": programme_event(device, {}),
        "title_only": programme_event(device, {"program": {"title": "Synthetic programme"}}),
        "invalid_programme": programme_event(device, {"program": {"status": "UNKNOWN"}}),
        "empty_status": {"event": "status", "payload": {"statuses": []}},
        "remote": property_event(device.codes["remoteCtrValid"], "1"),
        "online": {"event": "deviceStatusEvent", "payload": {"status": "ONLINE"}},
        "link": {"event": "deviceConnectionStatusEvent", "payload": {"status": "GOOD"}},
        "alarms": {"event": "alarms", "payload": {"alarms": []}},
        "unknown_property": property_event("not-a-cycle-code", "2"),
        "invalid_phase": property_event(device.codes["cyclePhase"], "invalid"),
    }
    for timestamp in (1030.0, 1090.0, 1179.0):
        clock.now = timestamp
        device.on_message(packets[noise])
        await hub.async_refresh_laundry()
        client.async_get_device_status.assert_not_awaited()
        assert device.laundry_updated_at == 1000.0
    clock.now = 1180.0
    await hub.async_refresh_laundry()
    client.async_get_device_status.assert_awaited_once_with(device.device_id)


@pytest.mark.parametrize("field", ["phase", "remaining"])
@pytest.mark.parametrize("value", ["0", "2"])
async def test_useful_cycle_properties_reset_quiet_period(real_polling, field, value):
    hub, device, client, clock = real_polling
    meaning = "cyclePhase" if field == "phase" else next(
        name for name in ("remainingDryTime", "remainingTimeMM", "remainingTimeHH") if name in device.codes
    )
    clock.now = 1040.0
    device.on_message(property_event(device.codes[meaning], value))
    assert device.laundry_updated_at == 1040.0
    clock.now = 1180.0
    await hub.async_refresh_laundry()
    client.async_get_device_status.assert_not_awaited()
    clock.now = 1220.0
    await hub.async_refresh_laundry()
    client.async_get_device_status.assert_awaited_once_with(device.device_id)


@pytest.mark.parametrize("value", [None, True, False, "NaN", "inf", "-inf", "invalid", [], {}])
def test_invalid_cycle_properties_are_not_fresh_feedback(real_polling, value):
    _hub, device, _client, clock = real_polling
    clock.now = 1040.0
    device.on_message(property_event(device.codes["cyclePhase"], value))
    assert device.laundry_updated_at == 1000.0


@pytest.mark.parametrize("status", ["IN_PROGRESS", "PROGRAM_PAUSED", "FINISHED", "CANCELLED", "PROGRAM_STARTED"])
def test_native_programme_status_counts_as_cycle_feedback(real_polling, status):
    _hub, device, _client, clock = real_polling
    clock.now = 1040.0
    device.on_message(programme_event(device, {"program": {"status": status}}))
    assert device.laundry_updated_at == 1040.0


@pytest.mark.parametrize("status", ["PROGRAM_STARTED", "PROGRAM_FINISHED"])
def test_existing_direct_programme_status_counts_as_cycle_feedback(real_polling, status):
    _hub, device, _client, clock = real_polling
    clock.now = 1040.0
    device.on_message(programme_event(device, {"status": status}))
    assert device.laundry_updated_at == 1040.0


async def test_successful_rest_refresh_resets_cycle_quiet_period(real_polling):
    hub, device, client, clock = real_polling
    clock.now = 1180.0
    await hub.async_refresh_laundry()
    assert device.laundry_updated_at == 1180.0
    clock.now = 1210.0
    await hub.async_refresh_laundry()
    assert client.async_get_device_status.await_count == 1
    clock.now = 1360.0
    await hub.async_refresh_laundry()
    assert client.async_get_device_status.await_count == 2


@pytest.mark.parametrize("source", ["phase", "remaining", "programme", "unrelated"])
def test_rest_freshness_uses_only_returned_cycle_data(real_polling, source):
    _hub, device, _client, clock = real_polling
    data = {"smartDeviceControl": {"attributes": []}}
    if source == "programme":
        data["program"] = {"status": "PAUSED"}
    else:
        meaning = "cyclePhase" if source == "phase" else "remoteCtrValid" if source == "unrelated" else next(
            name for name in ("remainingDryTime", "remainingTimeMM", "remainingTimeHH") if name in device.codes
        )
        data["smartDeviceControl"]["attributes"] = [{"name": device.codes[meaning], "currentValue": "0"}]
    clock.now = 1040.0
    device.update_from_backend(data)
    assert device.laundry_updated_at == (1000.0 if source == "unrelated" else 1040.0)


async def test_cached_unsynced_state_does_not_enable_active_poll(real_polling):
    hub, device, client, clock = real_polling
    device.synced = False
    clock.now = 1180.0
    await hub.async_refresh_laundry()
    client.async_get_device_status.assert_not_awaited()


async def test_minute_cycle_updates_do_not_trigger_fallback(real_polling):
    hub, device, client, clock = real_polling
    meaning = next(name for name in ("remainingDryTime", "remainingTimeMM", "remainingTimeHH") if name in device.codes)
    for elapsed in range(15, 601, 15):
        clock.now = 1000.0 + elapsed
        if elapsed % 60 == 0:
            device.on_message(property_event(device.codes[meaning], str(30 - elapsed // 60)))
        if elapsed % 30 == 15:
            await hub.async_refresh_laundry()
    client.async_get_device_status.assert_not_awaited()


@pytest.mark.parametrize("state", ["running", "paused"])
async def test_success_without_cycle_data_cannot_poll_more_than_every_three_minutes(polling, state):
    hub, devices, client, clock = polling
    hub.devices = devices[:1]
    devices[0].state = state
    await hub.async_refresh_laundry()
    for elapsed in (30, 60, 90, 120, 150, 179):
        clock.now = 1000 + elapsed
        await hub.async_refresh_laundry()
        assert client.async_get_device_status.await_count == 1
    clock.now = 1180
    await hub.async_refresh_laundry()
    assert client.async_get_device_status.await_count == 2


async def test_poll_interval_and_global_failure_backoff_both_apply(polling):
    hub, devices, client, clock = polling
    hub.devices = devices[:1]
    client.async_get_device_status.side_effect = HaierConnectionError("offline")
    await hub.async_refresh_laundry()
    assert hub._laundry_retry_at == 1060
    clock.now = 1060
    await hub.async_refresh_laundry()
    assert client.async_get_device_status.await_count == 1
    clock.now = 1180
    await hub.async_refresh_laundry()
    assert client.async_get_device_status.await_count == 2
    assert hub._laundry_retry_at == 1300


async def test_rest_passes_request_start_time_to_native_device(polling):
    hub, devices, _client, clock = polling
    hub.devices = devices[:1]
    await hub.async_refresh_laundry()
    devices[0].update_from_backend.assert_called_once_with({}, requested_at=clock.now)


@pytest.mark.parametrize("cached_state", ["off", "idle", "finished", "cancelled"])
async def test_online_inactive_machine_recovers_missed_start(real_polling, cached_state):
    hub, device, client, clock = real_polling
    response = client.async_get_device_status.return_value
    for attr in device_module.device_part(response)["attributes"]:
        if str(attr["name"]) == device.codes["onOffStatus"]:
            attr["currentValue"] = "1"
    device._apply_value(device.codes["onOffStatus"], "0" if cached_state == "off" else "1")
    device._apply_value(device.codes["cyclePhase"], "0")
    device.program_status = {"finished": "FINISHED", "cancelled": "CANCELLED"}.get(cached_state)
    assert device.state == cached_state
    clock.now = 1180.0
    await hub.async_refresh_laundry()
    client.async_get_device_status.assert_awaited_once_with(device.device_id)
    assert device.state == "running"


def wake_packet(device, signal):
    packets = {
        "online": {"event": "deviceStatusEvent", "payload": {"status": "ONLINE"}},
        "link": {"event": "deviceConnectionStatusEvent", "payload": {"status": "GOOD"}},
        "power": property_event(device.codes["onOffStatus"], "1"),
        "programme": programme_event(device, {"status": "PROGRAM_STARTED"}),
        "programme_empty_nested": programme_event(device, {"program": None, "status": "PROGRAM_STARTED"}),
    }
    return json.dumps({**packets[signal], "macAddress": device.device_id})


async def test_cycle_updates_without_power_cannot_keep_online_machine_off(real_polling):
    hub, device, client, clock = real_polling
    response = client.async_get_device_status.return_value
    for attr in device_module.device_part(response)["attributes"]:
        if str(attr["name"]) == device.codes["onOffStatus"]:
            attr["currentValue"] = "1"
    device._apply_value(device.codes["onOffStatus"], "0")
    device.program_status = None
    hub._laundry_polled_at[device.device_id] = clock.now
    for elapsed in (60, 120, 180):
        clock.now = 1000.0 + elapsed
        device.on_message(property_event(device.codes["cyclePhase"], "5"))
        await hub.async_refresh_laundry()
        if elapsed < 180:
            client.async_get_device_status.assert_not_awaited()
    client.async_get_device_status.assert_awaited_once_with(device.device_id)
    assert device.state == "running"


async def test_first_standby_budget_is_not_restarted_by_partial_push(real_polling):
    hub, device, client, clock = real_polling
    device._apply_value(device.codes["onOffStatus"], "0")
    device.program_status = None
    for elapsed in range(30, 211, 30):
        clock.now = 1000.0 + elapsed
        hub._handle_message(json.dumps({**property_event(device.codes["cyclePhase"], "5"),
                                       "macAddress": device.device_id}))
        await hub.async_refresh_laundry()
        if elapsed < 210:
            client.async_get_device_status.assert_not_awaited()
    client.async_get_device_status.assert_awaited_once_with(device.device_id)


@pytest.mark.parametrize("signal", ["online", "link", "power", "programme", "programme_empty_nested"])
async def test_wakeup_reads_before_stale_timeout(real_polling, signal):
    hub, device, client, clock = real_polling
    device._apply_value(device.codes["onOffStatus"], "0")
    device.link_status = "LOST"
    if signal in ("online", "link"):
        device.available = "OFFLINE"
    clock.now = 1010.0
    hub._handle_message(wake_packet(device, signal))
    await hub.async_refresh_laundry()
    client.async_get_device_status.assert_awaited_once_with(device.device_id)


async def test_repeated_online_and_good_coalesce_without_repoll(real_polling):
    hub, device, client, clock = real_polling
    device.available = "OFFLINE"
    device.link_status = "LOST"
    clock.now = 1010.0
    for _ in range(100):
        hub._handle_message(wake_packet(device, "online"))
        hub._handle_message(wake_packet(device, "link"))
    await hub.async_refresh_laundry()
    for _ in range(100):
        hub._handle_message(wake_packet(device, "online"))
        hub._handle_message(wake_packet(device, "link"))
        await hub.async_refresh_laundry()
    client.async_get_device_status.assert_awaited_once_with(device.device_id)


@pytest.mark.parametrize("delay", [30.0, 600.0])
async def test_wakeup_waits_for_cooldown_and_retry_after(real_polling, delay):
    hub, device, client, clock = real_polling
    hub._laundry_polled_at[device.device_id] = 1000.0
    hub._laundry_retry_at = 1000.0 + delay if delay > 30 else 0.0
    device.available = "OFFLINE"
    clock.now = 1010.0
    hub._handle_message(wake_packet(device, "online"))
    await hub.async_refresh_laundry()
    client.async_get_device_status.assert_not_awaited()
    clock.now = 1000.0 + delay - 0.1
    await hub.async_refresh_laundry()
    client.async_get_device_status.assert_not_awaited()
    clock.now = 1000.0 + delay
    await hub.async_refresh_laundry()
    client.async_get_device_status.assert_awaited_once_with(device.device_id)


async def test_offline_cancels_pending_wakeup(real_polling):
    hub, device, client, clock = real_polling
    device.available = "OFFLINE"
    device.link_status = "LOST"
    clock.now = 1010.0
    hub._handle_message(wake_packet(device, "link"))
    hub._handle_message(json.dumps({"event": "deviceStatusEvent", "macAddress": device.device_id,
                                  "payload": {"status": "OFFLINE"}}))
    await hub.async_refresh_laundry()
    client.async_get_device_status.assert_not_awaited()


async def test_failed_good_wakeup_retries_after_rate_limit_without_online_packet(real_polling):
    hub, device, client, clock = real_polling
    device.available = "OFFLINE"
    device.link_status = "LOST"
    clock.now = 1010.0
    hub._handle_message(wake_packet(device, "link"))
    client.async_get_device_status.side_effect = HaierRateLimitError(600)
    await hub.async_refresh_laundry()
    assert device.device_id in hub._laundry_wakeups
    clock.now = 1609.9
    await hub.async_refresh_laundry()
    assert client.async_get_device_status.await_count == 1
    client.async_get_device_status.side_effect = None
    clock.now = 1610.0
    await hub.async_refresh_laundry()
    assert client.async_get_device_status.await_count == 2
    assert not hub._laundry_wakeups


async def test_offline_during_failed_read_does_not_restore_wakeup(real_polling):
    hub, device, client, clock = real_polling
    device.available = "OFFLINE"
    device.link_status = "LOST"
    clock.now = 1010.0
    hub._handle_message(wake_packet(device, "link"))

    async def fail_after_offline(_device_id):
        clock.now = 1011.0
        hub._handle_message(json.dumps({"event": "deviceStatusEvent", "macAddress": device.device_id,
                                      "payload": {"status": "OFFLINE"}}))
        raise HaierRateLimitError(600)

    client.async_get_device_status.side_effect = fail_after_offline
    await hub.async_refresh_laundry()
    assert not hub._laundry_wakeups
    clock.now = 1700.0
    await hub.async_refresh_laundry()
    assert client.async_get_device_status.await_count == 1


async def test_wakeup_during_read_survives_and_late_rest_cannot_undo_push(real_polling):
    hub, device, client, clock = real_polling
    device.available = "OFFLINE"
    device._apply_value(device.codes["onOffStatus"], "0")
    entered, release = asyncio.Event(), asyncio.Event()
    response = client.async_get_device_status.return_value

    async def read(_device_id):
        entered.set()
        await release.wait()
        return response

    client.async_get_device_status.side_effect = read
    clock.now = 1010.0
    hub._handle_message(wake_packet(device, "online"))
    task = asyncio.create_task(hub.async_refresh_laundry())
    await asyncio.wait_for(entered.wait(), 1)
    clock.now = 1011.0
    hub._handle_message(wake_packet(device, "power"))
    release.set()
    await task
    assert device.power is True
    assert device.device_id in hub._laundry_wakeups
    client.async_get_device_status.side_effect = None
    clock.now = 1040.0
    await hub.async_refresh_laundry()
    assert client.async_get_device_status.await_count == 2
    assert not hub._laundry_wakeups


async def test_wakeup_runs_in_entry_owned_background_task(real_polling, hass):
    hub, device, client, clock = real_polling
    entry = MockConfigEntry(domain="haier_evo")
    entry.add_to_hass(hass)
    hub._entry = entry
    device.available = "OFFLINE"
    clock.now = 1010.0
    hub._handle_message(wake_packet(device, "online"))
    task = hub._laundry_refresh_task
    assert task is not None
    await task
    client.async_get_device_status.assert_awaited_once_with(device.device_id)
    await hub.async_stop()


@pytest.mark.parametrize("packet", [
    {"event": "deviceConnectionStatusEvent", "payload": {"status": "BAD"}},
    {"event": "deviceConnectionStatusEvent", "payload": {"status": "LOST"}},
    {"event": "deviceStatusEvent", "payload": {}},
    {"event": "ProgramWasherBaseEvent", "data": {"status": "PROGRAM_FINISHED"}},
    {"event": "ProgramDryerBaseEvent", "data": {}},
    {"event": "ProgramWasherBaseEvent", "data": {"program": {"status": "UNKNOWN"}}},
    {"event": "ProgramDryerBaseEvent", "data": {"program": {"title": "Old title"}}},
])
@pytest.mark.parametrize("connected", [False, True])
async def test_unrelated_packets_do_not_request_early_read(real_polling, packet, connected):
    hub, device, client, clock = real_polling
    device.available = connected
    device._apply_value(device.codes["onOffStatus"], "0")
    clock.now = 1010.0
    hub._handle_message(json.dumps({**packet, "macAddress": device.device_id}))
    await hub.async_refresh_laundry()
    client.async_get_device_status.assert_not_awaited()
