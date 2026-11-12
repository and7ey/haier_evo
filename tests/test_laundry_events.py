"""Cycle notifications on Home Assistant's event bus, without sending appliance commands."""
from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.haier_evo.laundry_events import EVENT_TYPE, LaundryEvents


@pytest.fixture
async def machine(hass):
    device = SimpleNamespace(
        hass=hass, state="idle", available=True, program_title=None, remaining_time=0,
        in_postcare=False, postcare_name=None, laundry_errors=[],
        machine_entity_id="haier_evo.test_washer", device_name="Test washer",
        device_model="HW90-B14367U1", device_id="00:00:00:00:00:01", is_dryer=False,
        notify=lambda: None,
    )
    tracker = LaundryEvents(device)
    events = []
    unsubscribe = hass.bus.async_listen(EVENT_TYPE, lambda event: events.append(event.data))
    yield device, tracker, events
    tracker.close()
    unsubscribe()
    await hass.async_block_till_done()


async def observe(hass, machine, *, started=False, **changes):
    device, tracker, _ = machine
    for name, value in changes.items():
        setattr(device, name, value)
    tracker.observe(started=started)
    await hass.async_block_till_done()


async def advance(hass, seconds=11):
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()


@pytest.mark.parametrize("initial", ["idle", "finished", "cancelled", "off"])
async def test_initial_state_does_not_notify(hass, machine, initial):
    await observe(hass, machine, state=initial)
    await observe(hass, machine, state=initial)
    await advance(hass)
    assert machine[2] == []
    assert not machine[1].laundry_ready


async def test_running_cycle_finishes_once_and_retains_name(hass, machine):
    await observe(hass, machine, state="running", program_title="Смешанная", remaining_time=44)
    await observe(hass, machine, state="finished", program_title=None, remaining_time=0)
    assert machine[2] == []
    await advance(hass)
    assert machine[2] == [{
        "type": "program_finished", "entity_id": "haier_evo.test_washer",
        "name": "Test washer", "model": "HW90-B14367U1", "kind": "washer",
        "program": "Смешанная", "errors": [], "completion_reason": "program",
    }]
    assert machine[1].laundry_ready
    await observe(hass, machine, state="finished")
    await advance(hass)
    assert len(machine[2]) == 1


async def test_snapshot_finished_then_new_cycle_notifies_again(hass, machine):
    await observe(hass, machine, state="running", program_title="Mixed")
    await observe(hass, machine, state="finished")
    await advance(hass)
    await observe(hass, machine, state="running", started=True)
    assert not machine[1].laundry_ready
    await observe(hass, machine, state="finished")
    await advance(hass)
    assert [event["type"] for event in machine[2]] == ["program_finished", "program_finished"]


async def test_idle_boundary_rearms_without_program_event(hass, machine):
    await observe(hass, machine, state="running")
    await observe(hass, machine, state="finished")
    await advance(hass)
    await observe(hass, machine, state="idle")
    await observe(hass, machine, state="running")
    await observe(hass, machine, state="finished")
    await advance(hass)
    assert len(machine[2]) == 2


async def test_transient_finished_then_running_in_same_receive_thread(hass, machine):
    await observe(hass, machine, state="running", program_title="Mixed")
    device, tracker, events = machine

    def receive_pair():
        device.state = "finished"
        tracker.observe()
        device.state = "running"
        tracker.observe(started=True)

    await asyncio.to_thread(receive_pair)
    await hass.async_block_till_done()
    await advance(hass)
    assert events == []
    assert not tracker.laundry_ready


async def test_short_false_finish_cannot_rearm_completed_cycle(hass, machine):
    await observe(hass, machine, state="running")
    await observe(hass, machine, state="finished")
    await advance(hass)
    await observe(hass, machine, state="running")
    await observe(hass, machine, state="finished")
    await advance(hass)
    assert len(machine[2]) == 1


async def test_stable_idle_has_neutral_event_and_no_success_flag(hass, machine):
    await observe(hass, machine, state="running", program_title="Delicate")
    await observe(hass, machine, state="idle", program_title=None)
    await observe(hass, machine, state="idle")
    await advance(hass)
    assert [event["type"] for event in machine[2]] == ["cycle_ended"]
    assert machine[2][0]["program"] == "Delicate"
    assert machine[2][0]["completion_reason"] == "idle"
    assert not machine[1].laundry_ready
    await observe(hass, machine, state="idle")
    await advance(hass)
    assert len(machine[2]) == 1


async def test_explicit_finish_upgrades_pending_idle(hass, machine):
    await observe(hass, machine, state="running")
    await observe(hass, machine, state="idle")
    await observe(hass, machine, state="finished")
    await advance(hass)
    assert [event["type"] for event in machine[2]] == ["program_finished"]


@pytest.mark.parametrize("ending", ["cancelled", "off"])
async def test_cancel_or_power_off_is_not_completion(hass, machine, ending):
    await observe(hass, machine, state="running")
    await observe(hass, machine, state=ending)
    await advance(hass)
    assert machine[2] == []
    await observe(hass, machine, state="idle")
    await advance(hass)
    assert machine[2] == []


async def test_unavailable_cancels_confirmation(hass, machine):
    await observe(hass, machine, state="running")
    await observe(hass, machine, state="finished")
    await observe(hass, machine, available=False)
    await advance(hass)
    assert machine[2] == []


async def test_live_availability_checked_when_confirmation_fires(hass, machine):
    await observe(hass, machine, state="running")
    await observe(hass, machine, state="finished")
    machine[0].available = False
    await advance(hass)
    assert machine[2] == []


async def test_postcare_finishes_main_cycle_only_once(hass, machine):
    await observe(hass, machine, state="running", program_title="Mixed", remaining_time=1)
    await observe(hass, machine, state="care", in_postcare=True, postcare_name="Fresh Air System", remaining_time=120)
    await advance(hass)
    assert len(machine[2]) == 1
    assert machine[2][0]["completion_reason"] == "post_care"
    assert machine[2][0]["care_name"] == "Fresh Air System"
    assert machine[1].laundry_ready
    await observe(hass, machine, state="finished", in_postcare=False)
    await advance(hass)
    assert len(machine[2]) == 1


async def test_initial_postcare_is_ready_but_never_notifies(hass, machine):
    await observe(hass, machine, state="care", in_postcare=True, postcare_name="Anti-crease")
    await observe(hass, machine, state="finished", in_postcare=False)
    await advance(hass)
    assert machine[2] == []
    assert machine[1].laundry_ready


async def test_alarm_changes_have_decoding_and_no_repeated_push_spam(hass, machine):
    await observe(hass, machine)
    error = {"key": "doorAlarm", "code": "E2", "text": "[E2] Ошибка блокировки двери.", "description": "Закройте дверь."}
    await observe(hass, machine, laundry_errors=[error])
    await observe(hass, machine, laundry_errors=[dict(error)])
    assert len(machine[2]) == 1
    assert machine[2][0]["type"] == "error"
    assert machine[2][0]["errors"] == [error]
    await observe(hass, machine, laundry_errors=[])
    assert [event["type"] for event in machine[2]] == ["error", "error_cleared"]


async def test_current_errors_are_included_in_finish(hass, machine):
    await observe(hass, machine, state="running")
    await observe(hass, machine, state="finished")
    error = {"key": "doorAlarm", "code": "E2", "text": "Door lock error", "description": ""}
    await observe(hass, machine, laundry_errors=[error])
    await advance(hass)
    assert [event["type"] for event in machine[2]] == ["error", "program_finished"]
    assert machine[2][-1]["errors"] == [error]


async def test_unload_cancels_pending_and_queued_observations(hass, machine):
    await observe(hass, machine, state="running")
    await observe(hass, machine, state="finished")
    machine[1].observe()
    machine[1].close()
    await hass.async_block_till_done()
    await advance(hass)
    assert machine[2] == []
