import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import time
import threading
from unittest.mock import Mock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed
from homeassistant.util import dt as dt_util
from requests import Response
from requests.exceptions import HTTPError, Timeout

from homeassistant.helpers.update_coordinator import UpdateFailed
from custom_components.haier_evo import api
from custom_components.haier_evo.coordinator import HaierLaundryCoordinator
from test_laundry import create, control_fixture, FakeCloud


def test_offline_then_online_needs_and_accepts_full_snapshot():
    cloud, device = create("WM", control_fixture())
    device.recovery = Mock()
    device.on_message({"event": "deviceStatusEvent", "payload": {"status": "OFFLINE"}})
    device.on_message({"event": "deviceStatusEvent", "payload": {"status": "ONLINE"}})
    assert not device.available
    device.recovery.request_refresh.assert_called_once()
    assert device.apply_snapshot(control_fixture(), device._state_revision)
    assert device.available and device.remote_control is True
    assert device.program_status == "idle"
    cloud.socket_app.send.assert_not_called()


def test_snapshot_cannot_overwrite_newer_push_or_accept_partial_fields():
    cloud, device = create("WM", control_fixture())
    revision = device._state_revision
    device.on_message({"event": "status", "payload": {"statuses": [{"properties": {"18": "2"}}]}})
    assert not device.apply_snapshot(control_fixture(), revision)
    assert device.values["18"] == "2"
    incomplete = deepcopy(control_fixture())
    incomplete["smartDeviceControl"]["attributes"] = []
    with pytest.raises(ValueError):
        device.apply_snapshot(incomplete, device._state_revision)
    assert device.values["18"] == "2"
    cloud.socket_app.send.assert_not_called()


def test_recovery_does_not_release_pending_start_on_same_idle_snapshot():
    cloud, device = create("WM", control_fixture())
    device.set_selected_program("Fabric - Cotton")
    device.send_laundry_action("start")
    device.mark_stale()
    device.apply_snapshot(control_fixture(), device._state_revision)
    assert device._start_pending and device.available_actions == []
    assert cloud.socket_app.send.call_count == 1


def test_snapshot_finishes_only_a_previously_confirmed_active_cycle():
    data = control_fixture()
    data["program"] = {"status": "FINISHED", "title": "Cotton"}
    cloud, device = create("WM", control_fixture())
    device.machine_entity_id = "haier_evo.synthetic"
    device._fire_laundry_event = Mock()
    device.apply_snapshot(data, device._state_revision)
    device._fire_laundry_event.assert_not_called()
    device._program_status = "IN_PROGRESS"
    device.apply_snapshot(data, device._state_revision)
    device._fire_laundry_event.assert_called_once_with("program_finished")
    device.apply_snapshot(data, device._state_revision)
    assert device._fire_laundry_event.call_count == 1


def test_phase_updates_keep_confirmed_running_program():
    cloud, device = create("WM", control_fixture())
    device.on_message({"event": "ProgramWasherBaseEvent",
                       "data": {"program": {"status": "IN_PROGRESS", "title": "Cotton"}}})
    device.on_message({"event": "status", "payload": {"statuses": [{"properties": {"18": "3"}}]}})
    assert device.program_status == "running" and device.program_name == "Cotton"


@pytest.mark.asyncio
async def test_poll_backoff_blocks_event_bursts_and_resets_after_recovery(hass):
    cloud = FakeCloud(control_fixture())
    cloud.hass = hass
    device = api.HaierDevice.create(cloud, "WM", "synthetic-device", device_title="Machine")
    cloud.read_device_snapshot = Mock(side_effect=Timeout())
    entry = MockConfigEntry(domain="haier_evo")
    entry.add_to_hass(hass)
    coordinator = HaierLaundryCoordinator(hass, entry, device)
    with pytest.raises(UpdateFailed) as first:
        await coordinator._async_update_data()
    assert first.value.retry_after == 60
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
    assert cloud.read_device_snapshot.call_count == 1
    coordinator._retry_until = 0
    cloud._laundry_next_request = 0
    cloud.read_device_snapshot.side_effect = None
    cloud.read_device_snapshot.return_value = control_fixture()
    await coordinator._async_update_data()
    assert coordinator._failures == 0
    assert coordinator.update_interval == timedelta(seconds=300)
    device._program_status = "IN_PROGRESS"
    assert coordinator.normal_interval == timedelta(seconds=60)
    await coordinator.async_shutdown()


@pytest.mark.asyncio
async def test_rate_limit_retry_after_is_shared_by_devices(hass):
    cloud = FakeCloud(control_fixture())
    cloud.hass = hass
    first = api.HaierDevice.create(cloud, "WM", "first", device_title="First")
    second = api.HaierDevice.create(cloud, "WM", "second", device_title="Second")
    response = Response()
    response.status_code = 429
    response.headers["Retry-After"] = "240"
    cloud.read_device_snapshot = Mock(side_effect=HTTPError(response=response))
    entry = MockConfigEntry(domain="haier_evo")
    entry.add_to_hass(hass)
    a = HaierLaundryCoordinator(hass, entry, first)
    b = HaierLaundryCoordinator(hass, entry, second)
    with pytest.raises(UpdateFailed) as error:
        await a._async_update_data()
    assert error.value.retry_after == 240
    with pytest.raises(UpdateFailed):
        await b._async_update_data()
    assert cloud.read_device_snapshot.call_count == 1
    await a.async_shutdown()
    await b.async_shutdown()


def test_expired_authorization_does_not_retry_login_in_same_snapshot_request():
    client = api.Haier.__new__(api.Haier)
    client._lock = threading.Lock()
    client._device_id = "synthetic-client"
    client.token = "synthetic-token"
    client.tokenexpire = datetime.now(timezone.utc) + timedelta(hours=1)
    client.auth = Mock()
    response = Response()
    response.status_code = 401
    with patch("custom_components.haier_evo.api.requests.get", return_value=response) as get:
        with pytest.raises(HTTPError):
            client.read_device_snapshot("synthetic-device")
    assert get.call_count == 1
    assert client.tokenexpire <= datetime.now(timezone.utc)


def test_websocket_uses_a_bounded_pong_timeout():
    client = api.Haier.__new__(api.Haier)
    client._init_ws = Mock()
    client.socket_app = Mock()
    client.reset_limits()
    client.run_forever()
    kwargs = client.socket_app.run_forever.call_args.kwargs
    assert 0 < kwargs["ping_timeout"] < kwargs["ping_interval"]


@pytest.mark.asyncio
async def test_listener_uses_interval_and_repeated_push_does_not_extend_deadline(hass):
    cloud = FakeCloud(control_fixture())
    cloud.hass = hass
    cloud.read_device_snapshot = Mock(return_value=control_fixture())
    device = api.HaierDevice.create(cloud, "WM", "timer-device", device_title="Timer")
    entry = MockConfigEntry(domain="haier_evo")
    entry.add_to_hass(hass)
    coordinator = HaierLaundryCoordinator(hass, entry, device)
    remove = coordinator.async_add_listener(Mock())
    await hass.async_block_till_done()
    assert cloud.read_device_snapshot.call_count == 0
    first_deadline = coordinator._unsub_refresh.__self__.when()
    for _ in range(5):
        coordinator._accept_push()
    assert coordinator._unsub_refresh.__self__.when() == first_deadline
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=301))
    await hass.async_block_till_done()
    assert cloud.read_device_snapshot.call_count == 1
    remove()
    await coordinator.async_shutdown()
    coordinator.request_refresh()
    await hass.async_block_till_done()
    assert cloud.read_device_snapshot.call_count == 1


def test_unknown_to_clear_alarm_snapshot_does_not_report_recovery():
    cloud, device = create("WM", control_fixture())
    device.machine_entity_id = "haier_evo.synthetic"
    device._fire_laundry_event = Mock()
    data = control_fixture()
    data["smartDeviceControl"]["alarms"] = []
    device.apply_snapshot(data, device._state_revision)
    device._fire_laundry_event.assert_not_called()


def test_snapshot_recovers_missed_finish_after_a_socket_outage_once():
    cloud, device = create("WM", control_fixture())
    device.machine_entity_id = "haier_evo.synthetic"
    device._fire_laundry_event = Mock()
    device.on_message({"event": "ProgramWasherBaseEvent",
                       "data": {"program": {"status": "IN_PROGRESS", "title": "Cotton"}}})
    device.mark_stale()
    finished = control_fixture()
    finished["program"] = {"status": "FINISHED", "title": "Cotton"}
    device.apply_snapshot(finished, device._state_revision)
    device.apply_snapshot(finished, device._state_revision)
    device._fire_laundry_event.assert_called_once_with("program_finished")


def test_partial_snapshot_program_preserves_confirmed_running_cycle():
    cloud, device = create("WM", control_fixture())
    device.on_message({"event": "ProgramWasherBaseEvent", "data": {
        "program": {"status": "IN_PROGRESS", "title": "Mixed"}}})
    data = control_fixture()
    data["program"] = {"title": "Mixed"}
    device.apply_snapshot(data, device._state_revision)
    assert device.program_status == "running" and device.program_name == "Mixed"
    cloud.socket_app.send.assert_not_called()


@pytest.mark.asyncio
async def test_fresh_running_push_avoids_http_poll_and_silence_recovers(hass):
    cloud, device = create("WM", control_fixture())
    cloud.hass = hass
    cloud.read_device_snapshot = Mock(return_value=control_fixture())
    coordinator = HaierLaundryCoordinator(hass, MockConfigEntry(domain="haier_evo"), device)
    device.on_message({"event": "ProgramWasherBaseEvent", "data": {
        "program": {"status": "IN_PROGRESS", "title": "Mixed"}}})
    await coordinator._async_update_data()
    cloud.read_device_snapshot.assert_not_called()
    device._last_observed -= 181
    await coordinator._async_update_data()
    cloud.read_device_snapshot.assert_called_once()
    cloud.socket_app.send.assert_not_called()
    await coordinator.async_shutdown()
