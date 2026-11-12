"""Cloud descriptions and an HA history projection; no raw push captures."""
from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import pytest
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.haier_evo.device_laundry import HaierLaundry, laundry_screen
from custom_components.haier_evo.laundry_events import EVENT_TYPE, EVENT_TYPES, LaundryEvents

from .conftest import load_fixture
from .test_devices import LAUNDRY, eid, start, stop


@pytest.fixture(params=[0, 1, 2], ids=["HW60", "HW90", "HD80"])
def machine(hass, request):
    row = load_fixture("laundry_dump.json")["devices"][request.param]
    hub = SimpleNamespace(hass=hass, cloud_available=True)
    device = HaierLaundry(haier=hub, device_mac=row["device_id"], backend_data=deepcopy(row["backend_data"]))
    device._apply_value(device.codes["onOffStatus"], "1")
    device._apply_value(device.codes["cyclePhase"], "0")
    device.program_title = "Synthetic programme"
    device.program_status = None
    device.laundry_events.observe()
    remove = device.add_listener(device.laundry_events.observe)
    yield device
    remove()
    device.laundry_events.close()


@pytest.fixture
def received(hass):
    events = []

    @callback
    def receive(event):
        events.append(event.data)

    remove = hass.bus.async_listen(EVENT_TYPE, receive)
    yield events
    remove()


def report(device, status, *, phase=None):
    device.program_status = status
    if phase is not None:
        device._apply_value(device.codes["cyclePhase"], str(phase))
    device.notify()


async def settle(hass, freezer):
    freezer.tick(timedelta(seconds=11))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()


def enter_care(machine, status="IN_PROGRESS"):
    report(machine, "IN_PROGRESS", phase=2)
    report(machine, status, phase="7" if machine.is_dryer else "16")


@pytest.mark.parametrize("status", ["IN_PROGRESS", "PAUSED"])
async def test_main_completion_is_reported_while_cloud_declared_care_continues(hass, machine, received, freezer, status):
    enter_care(machine, status)
    native_state = machine.state
    await settle(hass, freezer)
    assert [event["type"] for event in received] == ["program_finished", "care_started"]
    assert received[0]["completion_reason"] == "post_care"
    assert received[0]["care_name"] == (
        "Антисминание" if machine.is_dryer else "Fresh Air System" if "HW90" in machine.device_model else "Ночная стирка"
    )
    assert received[0]["laundry_ready"] is True
    assert received[0]["program"] == "Synthetic programme"
    assert machine.state == native_state
    report(machine, "FINISHED")
    await settle(hass, freezer)
    assert [event["type"] for event in received] == ["program_finished", "care_started"]


async def test_care_at_setup_does_not_replay_main_completion(hass, machine, received, freezer):
    machine.laundry_events.close()
    machine.program_status = "IN_PROGRESS"
    machine._apply_value(machine.codes["cyclePhase"], "7" if machine.is_dryer else "16")
    machine.laundry_events = LaundryEvents(machine)
    machine.laundry_events.observe()
    for _ in range(3):
        machine.laundry_events.observe()
        await settle(hass, freezer)
    assert received == []


async def test_brief_care_returning_to_main_program_is_not_completion(hass, machine, received, freezer):
    enter_care(machine)
    freezer.tick(timedelta(seconds=4))
    report(machine, "IN_PROGRESS", phase=2)
    await settle(hass, freezer)
    assert received == []
    assert machine.laundry_events.laundry_ready is False


async def test_care_confirmation_recovers_after_temporary_disconnect(hass, machine, received, freezer):
    enter_care(machine)
    freezer.tick(timedelta(seconds=4))
    machine._haier.cloud_available = False
    machine.notify()
    await settle(hass, freezer)
    assert received == []
    machine._haier.cloud_available = True
    machine.notify()
    await settle(hass, freezer)
    assert [event["type"] for event in received] == ["program_finished", "care_started"]


@pytest.mark.parametrize("status", ["IN_PROGRESS", "PAUSED"])
async def test_brief_off_and_return_to_care_does_not_report_cycle_ended(hass, machine, received, freezer, status):
    enter_care(machine)
    freezer.tick(timedelta(seconds=4))
    machine._apply_value(machine.codes["onOffStatus"], "0")
    machine.notify()
    freezer.tick(timedelta(seconds=4))
    machine._apply_value(machine.codes["onOffStatus"], "1")
    report(machine, status)
    await settle(hass, freezer)
    assert received == []
    assert machine.state in ("running", "paused")


@pytest.mark.parametrize("boundary", ["off", "cancel", "fault", "new_program"])
async def test_boundary_during_disconnect_disarms_old_main_evidence(hass, machine, received, freezer, boundary):
    enter_care(machine)
    freezer.tick(timedelta(seconds=4))
    machine._haier.cloud_available = False
    if boundary == "off":
        machine._apply_value(machine.codes["onOffStatus"], "0")
    elif boundary == "cancel":
        machine.program_status = "CANCELLED"
    elif boundary == "fault":
        machine._set_alarms(["doorLockFail"])
    else:
        machine.program_title = "New programme"
    machine.notify()
    machine._apply_value(machine.codes["onOffStatus"], "1")
    machine.program_status = "IN_PROGRESS"
    machine._set_alarms([])
    machine._haier.cloud_available = True
    machine.notify()
    await settle(hass, freezer)
    assert received == []


async def test_care_pause_and_started_resume_do_not_repeat_completion(hass, machine, received, freezer):
    enter_care(machine)
    await settle(hass, freezer)
    report(machine, "PAUSED")
    machine.on_message({
        "event": "ProgramDryerBaseEvent" if machine.is_dryer else "ProgramWasherBaseEvent",
        "data": {"status": "PROGRAM_STARTED"},
    })
    await settle(hass, freezer)
    assert machine.state == "running"
    assert [event["type"] for event in received] == ["program_finished", "care_started"]
    assert machine.laundry_events.laundry_ready is True


async def test_care_phase_number_without_cloud_declaration_is_not_completion(hass, machine, received, freezer):
    screen = laundry_screen(machine.status_data)
    for parent in (screen, screen.get("phase")):
        if isinstance(parent, dict):
            parent.pop("freshAirSystem", None)
            parent.pop("antiCrease", None)
    enter_care(machine)
    await settle(hass, freezer)
    assert received == []


@pytest.mark.parametrize("interruption", ["off", "cancel", "fault", "disconnect"])
async def test_care_confirmation_requires_stable_available_fault_free_care(hass, machine, received, freezer, interruption):
    enter_care(machine)
    freezer.tick(timedelta(seconds=4))
    if interruption == "off":
        machine._apply_value(machine.codes["onOffStatus"], "0")
    elif interruption == "cancel":
        machine.program_status = "CANCELLED"
    elif interruption == "fault":
        machine._set_alarms(["doorLockFail"])
    else:
        machine._haier.cloud_available = False
    machine.notify()
    await settle(hass, freezer)
    assert all(event["type"] != "program_finished" for event in received)
    assert machine.laundry_events.laundry_ready is False


async def test_new_program_during_care_confirmation_does_not_complete_new_program(hass, machine, received, freezer):
    enter_care(machine)
    freezer.tick(timedelta(seconds=4))
    machine.program_title = "New programme"
    report(machine, "IN_PROGRESS")
    await settle(hass, freezer)
    assert received == []


async def test_two_main_to_care_cycles_report_once_each(hass, machine, received, freezer):
    enter_care(machine)
    await settle(hass, freezer)
    machine._apply_value(machine.codes["onOffStatus"], "0")
    machine.notify()
    machine._apply_value(machine.codes["onOffStatus"], "1")
    enter_care(machine)
    await settle(hass, freezer)
    assert [event["type"] for event in received].count("program_finished") == 2
    assert [event["type"] for event in received].count("care_started") == 2


@pytest.mark.parametrize("care_status", [None, "IN_PROGRESS", "PAUSED"])
async def test_explicit_finish_before_off_does_not_claim_care_is_still_running(hass, machine, received, freezer, care_status):
    report(machine, "IN_PROGRESS", phase=2)
    report(machine, "FINISHED", phase="7" if machine.is_dryer else "16")
    if care_status:
        report(machine, care_status)
    freezer.tick(timedelta(seconds=4))
    machine._apply_value(machine.codes["onOffStatus"], "0")
    report(machine, care_status)
    assert machine.state == "off"
    await settle(hass, freezer)
    assert [event["type"] for event in received] == ["program_finished"]
    assert received[0]["completion_reason"] == "program"
    assert "care_name" not in received[0]


@pytest.mark.parametrize("machine", [pytest.param(1, id="observed-HW90")], indirect=True)
async def test_observed_washer_history_projection_reports_main_before_power_off(hass, machine, received, freezer):
    history = load_fixture("laundry_care_history.json")
    laundry_screen(machine.status_data)["freshAirSystem"] = deepcopy(history["care"])
    elapsed = 0
    for row in history["observations"]:
        freezer.tick(timedelta(seconds=row["seconds"] - elapsed))
        machine._apply_value(machine.codes["onOffStatus"], "0" if row["native_state"] == "off" else "1")
        machine._apply_value(machine.codes["remainingTimeHH"], str(row["remaining_minutes"] // 60))
        machine._apply_value(machine.codes["remainingTimeMM"], str(row["remaining_minutes"] % 60))
        report(machine, {"running": "IN_PROGRESS", "paused": "PAUSED"}.get(row["native_state"]), phase=row["phase"])
        assert machine.state == row["native_state"]
        await settle(hass, freezer)
        elapsed = row["seconds"] + 11
        if row["phase"] == "16":
            assert received[0]["type"] == "program_finished"
            assert received[0]["completion_reason"] == "post_care"
            assert received[0]["care_name"] == "Fresh Air System"
            assert row["remaining_minutes"] == 1
    assert [event["type"] for event in received] == ["program_finished", "care_started", "care_finished"]


async def test_confirmed_finish_once_and_native_state_unchanged(hass, machine, received, freezer):
    report(machine, "IN_PROGRESS", phase=0)
    assert machine.state == "running"
    report(machine, "FINISHED", phase=14)
    assert machine.state == "finished"
    await hass.async_block_till_done()
    assert received == []
    await settle(hass, freezer)
    assert [event["type"] for event in received] == ["program_finished"]
    assert received[0]["laundry_ready"] is True
    for _ in range(3):
        report(machine, "FINISHED", phase=14)
    await settle(hass, freezer)
    assert [event["type"] for event in received] == ["program_finished"]


async def test_initial_finished_and_brief_finish_are_not_notifications(hass, machine, received, freezer):
    machine.laundry_events.close()
    machine.program_status = "FINISHED"
    machine.laundry_events = LaundryEvents(machine)
    machine.laundry_events.observe()
    await settle(hass, freezer)
    assert received == []
    machine.program_status = "IN_PROGRESS"
    machine.laundry_events.observe()
    machine.program_status = "FINISHED"
    machine.laundry_events.observe()
    machine.program_status = "IN_PROGRESS"
    machine.laundry_events.observe()
    await settle(hass, freezer)
    assert received == []


async def test_idle_or_off_is_neutral_not_success(hass, machine, received, freezer):
    report(machine, "IN_PROGRESS", phase=2)
    report(machine, None, phase=0)
    await settle(hass, freezer)
    assert [event["type"] for event in received] == ["cycle_ended"]
    assert received[0]["laundry_ready"] is False
    assert received[0]["completion_reason"] == "idle"


@pytest.mark.parametrize("transition", ["idle", "off", "empty_current_program", "empty_current_program_active_phase"])
async def test_finished_keeps_priority_when_cloud_clears_activity(hass, machine, received, freezer, transition):
    report(machine, "IN_PROGRESS", phase=0)
    report(machine, "FINISHED")
    freezer.tick(timedelta(seconds=4))
    if transition.startswith("empty_current_program"):
        if transition == "empty_current_program_active_phase":
            machine._apply_value(machine.codes["cyclePhase"], "2")
        machine.update_from_backend({"control": {"currentProgram": {}}})
        machine.notify()
    elif transition == "off":
        machine._apply_value(machine.codes["onOffStatus"], "0")
        report(machine, None)
    else:
        report(machine, None, phase=0)
    native_state = "running" if transition == "empty_current_program_active_phase" else "off" if transition == "off" else "idle"
    assert machine.state == native_state
    freezer.tick(timedelta(seconds=7))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    assert [event["type"] for event in received] == ["program_finished"]
    assert received[0]["laundry_ready"] is True
    assert received[0]["program"] == "Synthetic programme"
    for _ in range(3):
        machine.notify()
    await settle(hass, freezer)
    assert [event["type"] for event in received] == ["program_finished"]
    assert machine.state == native_state


@pytest.mark.parametrize("status", ["IN_PROGRESS", "PAUSED", "PROGRAM_STARTED"])
async def test_explicit_activity_after_empty_program_cancels_pending_finish(hass, machine, received, freezer, status):
    report(machine, "IN_PROGRESS", phase=2)
    report(machine, "FINISHED")
    machine.update_from_backend({"control": {"currentProgram": {}}})
    machine.notify()
    if status == "PROGRAM_STARTED":
        machine.on_message({
            "event": "ProgramDryerBaseEvent" if machine.is_dryer else "ProgramWasherBaseEvent",
            "data": {"status": status},
        })
    else:
        report(machine, status)
    await settle(hass, freezer)
    assert received == []
    assert machine.laundry_events.laundry_ready is False
    report(machine, "FINISHED")
    await settle(hass, freezer)
    assert [event["type"] for event in received] == ["program_finished"]


async def test_cancel_is_not_success_and_does_not_repeat(hass, machine, received, freezer):
    report(machine, "IN_PROGRESS", phase=2)
    report(machine, "FINISHED")
    machine.update_from_backend({"control": {"currentProgram": {}}})
    machine.notify()
    report(machine, "CANCELLED")
    report(machine, "CANCELLED")
    await settle(hass, freezer)
    assert [event["type"] for event in received] == ["program_cancelled"]
    assert received[0]["laundry_ready"] is False


async def test_lost_connection_cancels_pending_finish(hass, machine, received, freezer):
    report(machine, "IN_PROGRESS")
    report(machine, "FINISHED")
    machine._haier.cloud_available = False
    machine.notify()
    await settle(hass, freezer)
    assert received == []


async def test_close_cancels_pending_confirmation(hass, machine, received, freezer):
    report(machine, "IN_PROGRESS")
    report(machine, "FINISHED")
    machine.laundry_events.close()
    await settle(hass, freezer)
    assert received == []


async def test_error_keeps_code_text_and_guidance_and_clears_once(hass, machine, received):
    alarm = "doorLockFail"
    machine._set_alarms([alarm])
    machine.notify()
    machine.notify()
    await hass.async_block_till_done()
    assert [event["type"] for event in received] == ["error"]
    error = received[0]["errors"][0]
    assert error["key"] == alarm
    assert error["text"] == machine.alarm_texts[0]
    assert error["code"] == "E2"
    assert error["description"]
    machine._set_alarms([])
    machine.notify()
    await hass.async_block_till_done()
    assert [event["type"] for event in received] == ["error", "error_cleared"]


async def test_fault_cannot_be_reported_as_success(hass, machine, received, freezer):
    report(machine, "IN_PROGRESS", phase=2)
    report(machine, "FINISHED")
    machine.update_from_backend({"control": {"currentProgram": {}}})
    machine.notify()
    machine._set_alarms(["doorLockFail"])
    machine.notify()
    await settle(hass, freezer)
    machine._set_alarms([])
    machine.notify()
    await settle(hass, freezer)
    assert [event["type"] for event in received] == ["error", "error_cleared"]
    assert machine.laundry_events.laundry_ready is False
    report(machine, "IN_PROGRESS")
    report(machine, "FINISHED")
    await settle(hass, freezer)
    assert received[-1]["type"] == "program_finished"


async def test_explicit_new_start_rearms_after_finish(hass, machine, received, freezer):
    report(machine, "IN_PROGRESS")
    report(machine, "FINISHED")
    await settle(hass, freezer)
    machine.on_message({
        "event": "ProgramDryerBaseEvent" if machine.is_dryer else "ProgramWasherBaseEvent",
        "data": {"status": "PROGRAM_STARTED"},
    })
    assert machine.laundry_events.laundry_ready is False
    report(machine, "FINISHED")
    await settle(hass, freezer)
    assert [event["type"] for event in received] == ["program_finished", "program_finished"]


async def test_changed_program_rearms_without_start_or_idle(hass, machine, received, freezer):
    report(machine, "IN_PROGRESS")
    report(machine, "FINISHED")
    await settle(hass, freezer)
    machine.program_title = "Second synthetic programme"
    report(machine, "IN_PROGRESS")
    assert machine.laundry_events.laundry_ready is False
    report(machine, "FINISHED")
    await settle(hass, freezer)
    assert [event["type"] for event in received] == ["program_finished", "program_finished"]
    assert [event["program"] for event in received] == ["Synthetic programme", "Second synthetic programme"]


@pytest.mark.parametrize("care", [False, True], ids=["finished", "care"])
async def test_same_program_bounce_does_not_rearm_after_finish(hass, machine, received, freezer, care):
    phase = ("7" if machine.is_dryer else "16") if care else "0"
    report(machine, "IN_PROGRESS", phase=phase)
    report(machine, "FINISHED")
    await settle(hass, freezer)
    expected = ["program_finished", "care_started"] if care else ["program_finished"]
    freezer.tick(timedelta(hours=12))
    report(machine, "IN_PROGRESS")
    assert machine.state == "running"
    assert machine.laundry_events.laundry_ready is True
    report(machine, "FINISHED")
    await settle(hass, freezer)
    assert [event["type"] for event in received] == expected


async def test_cloud_declared_care_does_not_override_finished(hass, machine, received, freezer):
    phase = "7" if machine.is_dryer else "16"
    report(machine, "IN_PROGRESS", phase=phase)
    await settle(hass, freezer)
    assert received == []
    assert machine.state == "running"
    report(machine, "FINISHED", phase=phase)
    await settle(hass, freezer)
    assert machine.state == "finished"
    assert [event["type"] for event in received] == ["program_finished", "care_started"]
    assert received[-1]["care_name"]
    report(machine, "FINISHED", phase=phase)
    await settle(hass, freezer)
    report(machine, "FINISHED", phase=0)
    await hass.async_block_till_done()
    assert [event["type"] for event in received] == ["program_finished", "care_started", "care_finished"]
    report(machine, "FINISHED", phase=phase)
    await settle(hass, freezer)
    assert [event["type"] for event in received] == ["program_finished", "care_started", "care_finished"]


async def test_explicit_cancel_status_does_not_change_native_sensor_state(hass, machine, received, freezer):
    report(machine, "IN_PROGRESS")
    machine.on_message({"event": "ProgramWasherBaseEvent", "data": {"status": "PROGRAM_CANCELLED"}})
    await settle(hass, freezer)
    assert machine.state == "running"  # the native 2.0 state only reads its existing fields
    assert [event["type"] for event in received] == ["program_cancelled"]


async def test_off_after_completion_ends_care_even_when_phase_is_stale(hass, machine, received, freezer):
    phase = "7" if machine.is_dryer else "16"
    report(machine, "IN_PROGRESS", phase=phase)
    report(machine, "FINISHED")
    await settle(hass, freezer)
    machine._apply_value(machine.codes["onOffStatus"], "0")
    machine.notify()
    await hass.async_block_till_done()
    assert machine.state == "off"
    assert [event["type"] for event in received] == ["program_finished", "care_started", "care_finished"]


async def test_standard_event_entity_and_disabled_entity_keep_bus_producer(hass, aioclient_mock, ws_factory, received, freezer):
    entry = await start(hass, aioclient_mock, ws_factory, LAUNDRY)
    device = entry.runtime_data.devices[1]
    uid = f"{device.device_id}_{device.device_model}_laundry"
    event_id = eid(hass, "event", uid)
    assert event_id
    assert hass.states.get(event_id).attributes["event_types"] == EVENT_TYPES
    device._apply_value(device.codes["onOffStatus"], "1")
    report(device, "IN_PROGRESS")
    report(device, "FINISHED")
    await settle(hass, freezer)
    assert hass.states.get(event_id).attributes["event_type"] == "program_finished"
    sensor_id = eid(hass, "sensor", f"{device.device_id}_{device.device_model}_state")
    assert hass.states.get(sensor_id).state == "finished"
    await stop(hass, entry)
    er.async_get(hass).async_update_entity(event_id, disabled_by=er.RegistryEntryDisabler.USER)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await ws_factory.wait_for(2)
    device = entry.runtime_data.devices[1]
    device._apply_value(device.codes["onOffStatus"], "1")
    received.clear()
    report(device, "IN_PROGRESS")
    report(device, "FINISHED")
    await settle(hass, freezer)
    registry_entry = er.async_get(hass).async_get(event_id)
    assert registry_entry is not None
    assert registry_entry.disabled_by is er.RegistryEntryDisabler.USER
    disabled_state = hass.states.get(event_id)
    assert disabled_state is None or disabled_state.attributes.get("event_type") != "program_finished"
    assert [event["type"] for event in received] == ["program_finished"]
    assert received[0]["entity_id"] == event_id
    assert received[0]["device_id"]
    report(device, None, phase=0)
    report(device, "IN_PROGRESS")
    report(device, "FINISHED")
    await stop(hass, entry)
    received.clear()
    await settle(hass, freezer)
    assert received == []
