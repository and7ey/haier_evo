"""Async recovery keeps later pushes, shares account limits and runs without a card."""
from __future__ import annotations

import asyncio
import time
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.haier_evo import coordinator as recovery
from custom_components.haier_evo import device as device_module
from custom_components.haier_evo import device_laundry as laundry_module
from custom_components.haier_evo.client import HaierConnectionError, HaierRateLimitError
from custom_components.haier_evo.const import DOMAIN
from custom_components.haier_evo.device import device_part
from custom_components.haier_evo.device_laundry import HaierLaundry

from .test_devices import HD80, HW90, LAUNDRY, device_by_mac, start, stop


@pytest.fixture
def clock(monkeypatch):
    clock = SimpleNamespace(now=time.monotonic(), sleeps=[])
    monotonic = SimpleNamespace(monotonic=lambda: clock.now)
    for module in (recovery, device_module, laundry_module):
        monkeypatch.setattr(module, "time", monotonic)

    async def sleep(seconds):
        clock.sleeps.append(seconds)
        clock.now += seconds
        await asyncio.sleep(0)

    monkeypatch.setattr(recovery, "asyncio", SimpleNamespace(sleep=sleep))
    return clock


def snapshot(device, *, minutes=17, phase="2", status="IN_PROGRESS", online=True, power="1"):
    data = deepcopy(device.status_data)
    data["program"] = {"title": "Mixed", "status": status}
    part = device_part(data)
    part["status"] = "ONLINE" if online else "OFFLINE"
    values = {device.codes.get("cyclePhase"): phase, device.codes.get("onOffStatus"): power,
              device.codes.get("remoteCtrValid"): "1", device.codes.get("remainingTimeHH"): "0",
              device.codes.get("remainingTimeMM"): str(minutes), device.codes.get("remainingDryTime"): str(minutes)}
    for attribute in part["attributes"]:
        code = str(attribute.get("name"))
        if code in values:
            attribute["currentValue"] = values[code]
    return data


def push(device, **values):
    device.on_message({
        "event": "status", "payload": {"statuses": [{"properties": {
            device.codes[meaning]: str(value) for meaning, value in values.items()
        }}]},
    })


@pytest.fixture
async def machines(hass, config_entry, clock):
    hub = SimpleNamespace(
        hass=hass, client=SimpleNamespace(async_get_device_status=AsyncMock()),
        cloud_available=True, laundry_control=True, _stopping=False, _reauth_started=False,
        on_refreshed=Mock(), _schedule_rebuild=Mock(), _start_reauth=Mock(), async_send=AsyncMock(),
    )
    devices = [HaierLaundry(haier=hub, device_mac=item["device_id"], backend_data=deepcopy(item["backend_data"]))
               for item in (HW90, HD80)]
    for device in devices:
        device.update_from_backend(snapshot(device))
    account = recovery.LaundryPollingState()
    coordinators = [recovery.HaierLaundryCoordinator(hass, config_entry, device, account) for device in devices]
    yield hub, devices, coordinators
    for coordinator in coordinators:
        await coordinator.async_shutdown()
    for device in devices:
        device.events.close()
    await hass.async_block_till_done()


async def test_rest_reply_cannot_undo_a_later_push(hass, machines, clock):
    hub, (washer, _), (coordinator, _) = machines
    entered, release = asyncio.Event(), asyncio.Event()
    stale = snapshot(washer, minutes=49)

    async def delayed_status(_mac):
        entered.set()
        await release.wait()
        return stale

    hub.client.async_get_device_status.side_effect = delayed_status
    clock.now += 46
    task = hass.async_create_task(coordinator.async_refresh())
    await entered.wait()
    clock.now += 1
    push(washer, cyclePhase="3", remainingTimeMM="8")
    release.set()
    await task
    assert washer.remaining_time == 8
    assert washer.phase_value == "3"
    hub.client.async_get_device_status.assert_awaited_once_with(washer.device_id)
    hub.on_refreshed.assert_not_called()


@pytest.mark.parametrize("retry_after", [1, 137])
async def test_429_cooldown_is_shared_and_honours_retry_after(machines, clock, retry_after):
    hub, (washer, dryer), (first, second) = machines
    clock.now += 46
    hub.client.async_get_device_status.side_effect = HaierRateLimitError(retry_after)
    await first.async_refresh()
    assert not first.last_update_success
    assert hub.client.async_get_device_status.await_count == 1
    push(washer, remainingTimeMM="9")

    wait = max(60, retry_after)
    clock.now += wait - 1
    await second.async_refresh()
    assert hub.client.async_get_device_status.await_count == 1
    clock.now += 1
    hub.client.async_get_device_status.side_effect = None
    hub.client.async_get_device_status.return_value = snapshot(dryer, minutes=6)
    await second.async_refresh()
    assert hub.client.async_get_device_status.await_count == 2
    assert dryer.remaining_time == 6
    assert second.last_update_success


async def test_only_useful_fresh_push_suppresses_active_cycle_rest(machines, clock):
    hub, (washer, _), (coordinator, _) = machines
    clock.now += 46
    push(washer, remoteCtrValid="1")
    hub.client.async_get_device_status.return_value = snapshot(washer, minutes=40)
    await coordinator.async_refresh()
    assert hub.client.async_get_device_status.await_count == 1
    assert washer.remaining_time == 40

    clock.now += 16
    push(washer, remainingTimeMM="7")
    await coordinator.async_refresh()
    assert hub.client.async_get_device_status.await_count == 1
    assert washer.remaining_time == 7
    clock.now += 46
    hub.client.async_get_device_status.return_value = snapshot(washer, minutes=5)
    await coordinator.async_refresh()
    assert hub.client.async_get_device_status.await_count == 2
    assert washer.remaining_time == 5


async def test_pending_start_forces_rest_despite_recent_active_state(hass, machines):
    hub, (washer, _), _ = machines
    # Power is off but the cloud retained the previous running phase.
    washer.update_from_backend(snapshot(washer, power="0"))
    washer.set_selected_program(washer.programs[0]["title"])

    async def power_on(_message):
        # The power echo arrives before the program-start event.
        push(washer, onOffStatus="1")

    hub.async_send.side_effect = power_on
    hub.client.async_get_device_status.return_value = snapshot(washer, minutes=4)
    await washer.start()
    await hass.async_block_till_done()
    assert washer.state == "running"
    hub.async_send.assert_awaited_once()
    hub.client.async_get_device_status.assert_awaited_once_with(washer.device_id)
    assert washer.remaining_time == 4


async def test_shared_account_serializes_gets_fifteen_seconds_apart(machines, clock):
    hub, devices, coordinators = machines
    clock.now += 46
    starts = []

    async def status(mac):
        starts.append(clock.now)
        return snapshot(next(d for d in devices if d.device_id == mac), minutes=3)

    hub.client.async_get_device_status.side_effect = status
    await asyncio.gather(*(coordinator.async_refresh() for coordinator in coordinators))
    assert hub.client.async_get_device_status.await_count == 2
    assert starts[1] - starts[0] == pytest.approx(15, rel=0, abs=1e-9)
    assert [device.remaining_time for device in devices] == [3, 3]


@pytest.mark.parametrize("online,status,phase,seconds", [
    (True, "IN_PROGRESS", "2", 30),
    (False, "IN_PROGRESS", "2", 60),
    (True, None, "0", 120),
])
async def test_poll_interval_follows_the_refreshed_device_state(machines, clock, online, status, phase, seconds):
    hub, (washer, _), (coordinator, _) = machines
    data = snapshot(washer, minutes=3, online=online, status=status, phase=phase)
    washer.update_from_backend(data)
    clock.now += 46
    hub.client.async_get_device_status.return_value = data
    await coordinator.async_refresh()
    hub.client.async_get_device_status.assert_awaited_once_with(washer.device_id)
    assert washer.remaining_time == 3
    assert coordinator.update_interval == timedelta(seconds=seconds)


async def test_network_backoff_grows_caps_and_resets_after_success(machines, clock):
    hub, (washer, _), (coordinator, _) = machines
    hub.client.async_get_device_status.side_effect = HaierConnectionError("cloud offline")
    clock.now += 46
    for calls, seconds in enumerate((60, 120, 240, 480, 900), 1):
        await coordinator.async_refresh()
        assert hub.client.async_get_device_status.await_count == calls
        assert not coordinator.last_update_success
        assert coordinator.update_interval == timedelta(seconds=seconds)
        clock.now += seconds - 1
        await coordinator.async_refresh()
        assert hub.client.async_get_device_status.await_count == calls
        clock.now += 1
    hub.client.async_get_device_status.side_effect = None
    hub.client.async_get_device_status.return_value = snapshot(washer, minutes=2)
    await coordinator.async_refresh()
    assert hub.client.async_get_device_status.await_count == 6
    assert coordinator.last_update_success
    assert washer.remaining_time == 2
    assert coordinator.update_interval == timedelta(seconds=30)


async def test_shutdown_discards_inflight_reply_and_rejects_new_refresh(hass, machines, clock):
    hub, (washer, _), (coordinator, _) = machines
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed_status(_mac):
        entered.set()
        await release.wait()
        return snapshot(washer, minutes=1)

    hub.client.async_get_device_status.side_effect = delayed_status
    clock.now += 46
    task = hass.async_create_task(coordinator.async_refresh())
    await entered.wait()
    await coordinator.async_shutdown()
    release.set()
    await task
    assert washer.remaining_time == 17
    coordinator.request_refresh()
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hub.client.async_get_device_status.await_count == 1


async def test_polling_recovers_offline_device_without_opening_card_and_stops_on_unload(
    hass, aioclient_mock, ws_factory, monkeypatch, clock
):
    entry = await start(hass, aioclient_mock, ws_factory, LAUNDRY)
    washer = device_by_mac(entry, HW90["device_id"])
    responses = {device.device_id: snapshot(device) for device in entry.runtime_data.devices}
    responses[washer.device_id] = snapshot(washer, minutes=35, online=False)
    get_status = AsyncMock(side_effect=lambda mac: deepcopy(responses[mac]))
    monkeypatch.setattr(entry.runtime_data.client, "async_get_device_status", get_status)
    washer.on_message({"event": "deviceStatusEvent", "payload": {"status": "OFFLINE"}})
    await hass.async_block_till_done()
    assert get_status.await_count == 1
    assert not washer.connected
    assert washer.recovery.update_interval == timedelta(seconds=60)

    responses[washer.device_id] = snapshot(washer, minutes=8, online=True)
    clock.now += 61
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
    await hass.async_block_till_done()
    assert sum(call.args[0] == washer.device_id for call in get_status.await_args_list) == 2
    assert washer.connected and washer.remaining_time == 8
    remaining_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{washer.device_id}_{washer.device_model}_remaining_time"
    )
    assert hass.states.get(remaining_id).state == "8"
    await stop(hass, entry)
    get_status.reset_mock()
    clock.now += 300
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=300))
    await hass.async_block_till_done()
    get_status.assert_not_awaited()
    assert washer.recovery is None


@pytest.mark.parametrize("scheduled", [False, True], ids=["forced", "scheduled"])
async def test_unload_cancels_an_active_poll(
    hass, aioclient_mock, ws_factory, monkeypatch, clock, scheduled
):
    entry = await start(hass, aioclient_mock, ws_factory, LAUNDRY)
    washer = device_by_mac(entry, HW90["device_id"])
    entered, cancelled = asyncio.Event(), asyncio.Event()
    responses = {device.device_id: snapshot(device) for device in entry.runtime_data.devices}

    async def status(mac):
        if mac != washer.device_id:
            return responses[mac]
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    get_status = AsyncMock(side_effect=status)
    monkeypatch.setattr(entry.runtime_data.client, "async_get_device_status", get_status)
    if scheduled:
        clock.now += 121
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=121))
    else:
        clock.now += 46
        washer.recovery.request_refresh()
    async with asyncio.timeout(2):
        await entered.wait()
        await stop(hass, entry)
    assert cancelled.is_set()
    assert washer.recovery is None
    get_status.reset_mock()
    clock.now += 300
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=300))
    await hass.async_block_till_done()
    get_status.assert_not_awaited()
