"""Laundry extensions keep native async control and validate every programme."""
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.haier_evo.device_laundry import HaierLaundry, laundry_screen

from . import test_devices as base
from .test_laundry_recovery import snapshot


@pytest.fixture
def machine(hass):
    cloud = SimpleNamespace(hass=hass, cloud_available=True, laundry_control=True, async_send=AsyncMock())
    device = HaierLaundry(haier=cloud, device_mac=base.HW90["device_id"],
                          backend_data=deepcopy(base.HW90["backend_data"]))
    device._raw[device.codes["onOffStatus"]] = "1"
    device._raw[device.codes["remoteCtrValid"]] = "1"
    device._raw[device.codes["cyclePhase"]] = "0"
    device.program_status = "FINISHED"
    yield device, cloud
    device.events.close()


async def test_modal_entities_use_native_device_and_respect_read_only(hass, aioclient_mock, ws_factory):
    entry = await base.start(hass, aioclient_mock, ws_factory, base.LAUNDRY)
    try:
        sent = list(ws_factory.current.sent)
        for device in entry.runtime_data.devices:
            state = hass.states.get(device.machine_entity_id)
            assert state.entity_id.startswith("haier_evo.")
            assert state.attributes["friendly_name"] == device.device_name
            assert state.attributes["program_catalog"]
            assert state.attributes["available_actions"] == []
            for action in ("start", "pause", "resume", "cancel"):
                with pytest.raises(HomeAssistantError):
                    await hass.services.async_call("haier_evo", "execute_action", {
                        "entity_id": state.entity_id, "action": action,
                    }, blocking=True)
        assert ws_factory.current.sent == sent
    finally:
        await base.stop(hass, entry)


@pytest.mark.parametrize("index,count,groups", [(0, 28, 5), (1, 29, 5), (2, 20, 3)])
async def test_all_model_programmes_keep_cloud_constraints(hass, index, count, groups):
    data = base.LAUNDRY["devices"][index]
    cloud = SimpleNamespace(hass=hass, cloud_available=True, laundry_control=True, async_send=AsyncMock())
    device = HaierLaundry(haier=cloud, device_mac=data["device_id"], backend_data=deepcopy(data["backend_data"]))
    try:
        assert len(device.programs) == count
        assert len(device.program_catalog) == groups
        for programme in device.programs:
            device.set_selected_program(programme["title"])
            assert not device.program_settings.get("error"), (data["device_name"], programme["title"])
        cloud.async_send.assert_not_awaited()
    finally:
        device.events.close()


async def test_parameters_reach_native_async_start_and_pending_start_is_not_repeated(machine):
    device, cloud = machine
    device.set_selected_program("Смешанная")
    temperature = next(f for f in device.program_settings["fields"] if "Температур" in f["title"])
    value = next(o["value"] for o in temperature["options"] if o["value"] != temperature["value"])
    device.configure_program("Смешанная", {temperature["key"]: value})
    cloud.async_send.assert_not_awaited()
    await device.start()
    assert cloud.async_send.await_count == 1
    payload = cloud.async_send.call_args.args[0]
    assert {c["commandName"]: c["value"] for c in payload["commands"]}[temperature["key"]] == value
    with pytest.raises(HomeAssistantError):
        await device.start()
    assert cloud.async_send.await_count == 1
    device.on_message({"event": "ProgramWasherBaseEvent", "data": {"status": "PROGRAM_STARTED"}})
    assert device.state == "running" and not device._start_pending


async def test_active_unknown_and_out_of_range_edits_send_nothing(machine):
    device, cloud = machine
    device.set_selected_program("Смешанная")
    before = deepcopy(device.program_settings)
    for values in ({"invented": "1"}, {"50": "999"}):
        with pytest.raises(HomeAssistantError):
            device.configure_program("Смешанная", values)
    assert device.program_settings == before
    device.program_status = "IN_PROGRESS"
    device._raw[device.codes["cyclePhase"]] = "3"
    with pytest.raises(HomeAssistantError):
        device.configure_program("Смешанная", {"50": "30"})
    cloud.async_send.assert_not_awaited()


async def test_failed_start_releases_pending_without_resending(machine):
    device, cloud = machine
    device.set_selected_program("Смешанная")
    cloud.async_send.side_effect = HomeAssistantError("connection lost")
    with pytest.raises(HomeAssistantError):
        await device.start()
    assert not device._start_pending
    assert cloud.async_send.await_count == 1


async def test_idle_rest_does_not_allow_an_unconfirmed_start_to_be_sent_twice(machine):
    device, cloud = machine
    device.set_selected_program("Смешанная")
    idle = snapshot(device, status="FINISHED", phase="0", minutes=0)
    device.update_from_backend(idle)
    await device.start()
    device.update_from_backend(deepcopy(idle))
    assert device.available_actions == []
    with pytest.raises(HomeAssistantError) as error:
        await device.start()
    assert error.value.translation_key == "laundry_start_pending"
    assert cloud.async_send.await_count == 1

    device.on_message({"event": "ProgramWasherBaseEvent", "data": {"status": "PROGRAM_STARTED"}})
    assert device.state == "running" and not device._start_pending


@pytest.mark.parametrize("data", [{}, {"program": {"title": "Смешанная"}}])
async def test_partial_program_event_does_not_release_pending_start(machine, data):
    device, cloud = machine
    device.set_selected_program("Смешанная")
    await device.start()
    device.on_message({"event": "ProgramWasherBaseEvent", "data": data})
    with pytest.raises(HomeAssistantError) as error:
        await device.start()
    assert error.value.translation_key == "laundry_start_pending"
    assert cloud.async_send.await_count == 1


@pytest.mark.parametrize("data", [
    {"status": "PROGRAM_STARTED"},
    {"status": "PROGRAM_STARTED", "program": {"title": "Смешанная"}},
])
async def test_started_event_confirms_pending_start_even_with_a_partial_program(machine, data):
    device, _ = machine
    device.set_selected_program("Смешанная")
    await device.start()
    device.on_message({"event": "ProgramWasherBaseEvent", "data": data})
    assert device.state == "running" and not device._start_pending


@pytest.mark.parametrize("status", ["IN_PROGRESS", "PAUSED"])
async def test_active_rest_confirms_pending_start(machine, status):
    device, _ = machine
    device.set_selected_program("Смешанная")
    await device.start()
    device.update_from_backend(snapshot(device, status=status, phase="3"))
    assert not device._start_pending
    assert device.state in ("running", "paused")


@pytest.mark.parametrize("phases,expected", [(["0", "3"], "running"), (["3", "0"], "idle")])
async def test_last_batched_phase_wins(machine, phases, expected):
    device, _ = machine
    device.program_status = "IN_PROGRESS"
    device._raw[device.codes["cyclePhase"]] = "3"
    device.on_message({"event": "status", "payload": {"statuses": [
        {"properties": {device.codes["cyclePhase"]: phase}} for phase in phases
    ]}})
    assert device.state == expected


async def test_new_phase_clears_old_finished_status_when_start_event_was_lost(machine):
    device, _ = machine
    device.on_message({"event": "status", "payload": {"statuses": [
        {"properties": {device.codes["cyclePhase"]: "3"}}
    ]}})
    assert device.state == "running"
    assert device.program_status is None


async def test_care_keeps_pause_resume_and_cancel(machine):
    device, _ = machine
    care = laundry_screen(device.status_data)["freshAirSystem"]["freshAirSystemAttrValue"]
    device._raw[device.codes["cyclePhase"]] = str(care)
    device.program_status = "IN_PROGRESS"
    assert device.state == "care"
    assert device.available_actions == ["pause", "cancel"]
    device.program_status = "PAUSED"
    assert device.state == "paused"
    assert device.available_actions == ["resume", "cancel"]


async def test_standalone_start_clears_the_previous_idle_override(machine):
    device, _ = machine
    device.program_status = "IN_PROGRESS"
    device._phase_idle = True
    device.on_message({"event": "ProgramWasherBaseEvent", "data": {"status": "PROGRAM_STARTED"}})
    assert device.state == "running"


async def test_repeated_idle_rest_cannot_restore_the_old_running_state(machine):
    device, _ = machine
    device.update_from_backend(snapshot(device, status="IN_PROGRESS", phase="3"))
    device.on_message({"event": "status", "payload": {"statuses": [
        {"properties": {device.codes["cyclePhase"]: "0"}}
    ]}})
    assert device.state == "idle"
    device.update_from_backend(snapshot(device, status="IN_PROGRESS", phase="0"))
    assert device.state == "idle"
    device.update_from_backend(snapshot(device, status="IN_PROGRESS", phase="0"))
    assert device.state == "idle"


async def test_new_program_with_active_status_can_clear_idle_override(machine):
    device, _ = machine
    device.update_from_backend(snapshot(device, status="IN_PROGRESS", phase="3"))
    device.on_message({"event": "status", "payload": {"statuses": [
        {"properties": {device.codes["cyclePhase"]: "0"}}
    ]}})
    data = snapshot(device, status="IN_PROGRESS", phase="0")
    data["program"]["title"] = "New cycle"
    device.update_from_backend(data)
    assert device.state == "running"


async def test_new_cycle_clears_ready_in_the_real_entity_state(hass, aioclient_mock, ws_factory):
    entry = await base.start(hass, aioclient_mock, ws_factory, base.LAUNDRY)
    device = base.device_by_mac(entry, base.HW90["device_id"])
    try:
        device.on_message({"event": "status", "payload": {"statuses": [{"properties": {
            device.codes["onOffStatus"]: "1", device.codes["cyclePhase"]: "3",
        }}]}})
        device.on_message({"event": "ProgramWasherBaseEvent", "data": {
            "status": "PROGRAM_STARTED", "program": {"title": "Mixed", "status": "IN_PROGRESS"},
        }})
        await hass.async_block_till_done()
        device.on_message({"event": "ProgramWasherBaseEvent", "data": {
            "program": {"title": "Mixed", "status": "FINISHED"},
        }})
        await hass.async_block_till_done()
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=11))
        await hass.async_block_till_done()
        assert hass.states.get(device.machine_entity_id).attributes["laundry_ready"] is True

        device.on_message({"event": "ProgramWasherBaseEvent", "data": {
            "status": "PROGRAM_STARTED", "program": {"title": "Mixed", "status": "IN_PROGRESS"},
        }})
        await hass.async_block_till_done()
        state = hass.states.get(device.machine_entity_id)
        assert state.state == "running"
        assert state.attributes["laundry_ready"] is False
    finally:
        await base.stop(hass, entry)
