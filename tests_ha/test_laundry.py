"""Washing machines and dryers in a real Home Assistant (tests/fixtures/laundry_dump.json:
the washer HW90-B14367U1 and the dryer HD80-A2367U1 of the app's device list). Read-only
by default; the control (beta) with the entry's option: the options flow, the program to
start and the buttons, and what goes to the cloud."""
from __future__ import annotations

import copy
import json

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from .conftest import DOMAIN, FIXTURES, FakeCloud, entity_id, haier, store_key, wait_for, wait_state

LAUNDRY = json.loads((FIXTURES / "laundry_dump.json").read_text(encoding="utf-8"))
HW90 = next(d for d in LAUNDRY["devices"] if "HW90" in d["device_name"])
HD80 = next(d for d in LAUNDRY["devices"] if "HD80" in d["device_name"])
WASHER, DRYER = HW90["device_id"], HD80["device_id"]
MODELS = {WASHER: "HW90B14367U", DRYER: "HD80A2367U1"}
ACTIONS = ("start", "pause", "resume", "cancel")


@pytest.fixture
def laundry(cloud: FakeCloud) -> FakeCloud:
    """The cloud of an account with a washer and a dryer."""
    cloud.home = copy.deepcopy(LAUNDRY["backend_data"])
    cloud.descriptions = {d["device_id"]: copy.deepcopy(d["backend_data"]) for d in (HW90, HD80)}
    return cloud


async def setup(hass: HomeAssistant, laundry: FakeCloud, make_entry, hass_storage, control: bool = False):
    entry = make_entry(options={"laundry_control": True} if control else None)
    hass_storage[store_key(entry)] = {"version": 1, "minor_version": 1, "key": store_key(entry), "data": laundry.tokens()}
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    await wait_for(lambda: haier(hass, entry)._connected.is_set(), "the websocket to open")
    return entry


def control_entities(hass: HomeAssistant, entry) -> list[tuple[str, str]]:
    registry = er.async_get(hass)
    return sorted((e.domain, e.unique_id) for e in er.async_entries_for_config_entry(registry, entry.entry_id)
                  if e.domain in ("button", "select"))


def button(hass: HomeAssistant, mac: str, action: str) -> str:
    return entity_id(hass, "button", f"{mac}_{MODELS[mac]}_laundry_{action}")


async def press(hass: HomeAssistant, mac: str, action: str) -> None:
    await hass.services.async_call("button", "press", {"entity_id": button(hass, mac, action)}, blocking=True)


async def ready(hass: HomeAssistant, laundry: FakeCloud, machine, status: str, remote: bool = True) -> None:
    """The machine as the cloud reports it: remote control, the program's state."""
    await laundry.push(machine.device_id, {machine.codes["remoteCtrValid"]: "1" if remote else "0"})
    event = "ProgramDryerBaseEvent" if machine.is_dryer else "ProgramWasherBaseEvent"
    await laundry.send({"event": event, "macAddress": machine.device_id,
                        "data": {"program": {"title": machine.program_title, "status": status}}})
    want = {"IN_PROGRESS": "running", "PAUSED": "paused", "FINISHED": "finished"}[status]
    await wait_for(lambda: machine.state == want and machine.remote_control is remote, f"the machine {want}")


async def test_read_only_by_default(hass: HomeAssistant, laundry: FakeCloud, make_entry, hass_storage) -> None:
    entry = await setup(hass, laundry, make_entry, hass_storage)
    assert control_entities(hass, entry) == []
    for mac in (WASHER, DRYER):
        assert hass.states.get(entity_id(hass, "sensor", f"{mac}_{MODELS[mac]}_state")) is not None
    assert laundry.commands() == []


async def test_options_flow_turns_the_control_on(hass: HomeAssistant, laundry: FakeCloud, make_entry, hass_storage) -> None:
    entry = await setup(hass, laundry, make_entry, hass_storage)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert (result["type"], result["step_id"]) == (FlowResultType.FORM, "init")
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"laundry_control": True})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert dict(entry.options) == {"laundry_control": True}
    want = sorted([("select", f"{mac}_{MODELS[mac]}_laundry_program") for mac in MODELS]
                  + [("button", f"{mac}_{MODELS[mac]}_laundry_{a}") for mac in MODELS for a in ACTIONS])
    await wait_for(lambda: entry.state is ConfigEntryState.LOADED and control_entities(hass, entry) == want,
                   "the integration reloaded with the control")
    assert laundry.commands() == [], "turning the control on sends nothing"

    # and off again: the entities leave the registry, no orphans stay
    result = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(result["flow_id"], {"laundry_control": False})
    await hass.async_block_till_done()
    await wait_for(lambda: entry.state is ConfigEntryState.LOADED and control_entities(hass, entry) == [],
                   "the control entities removed")


async def test_start_sends_the_chosen_program(hass: HomeAssistant, laundry: FakeCloud, make_entry, hass_storage) -> None:
    entry = await setup(hass, laundry, make_entry, hass_storage, control=True)
    washer = haier(hass, entry).get_device_by_id(WASHER)
    await ready(hass, laundry, washer, "FINISHED")
    select = entity_id(hass, "select", f"{WASHER}_{MODELS[WASHER]}_laundry_program")
    assert hass.states.get(select).attributes["options"] == [p["title"] for p in washer.programs]
    await hass.services.async_call("select", "select_option", {"entity_id": select, "option": "Деликатная"}, blocking=True)
    await wait_state(hass, select, "Деликатная")
    assert laundry.commands(WASHER) == [], "choosing a program sends nothing"

    await press(hass, WASHER, "start")
    chosen = next(p for p in washer.programs if p["title"] == "Деликатная")
    assert laundry.commands(WASHER) == [
        {"action": "operation", "macAddress": WASHER, "commandName": "8", "commands": chosen["commands"]}]


async def test_remote_control_off_on_the_machine(hass: HomeAssistant, laundry: FakeCloud, make_entry, hass_storage) -> None:
    entry = await setup(hass, laundry, make_entry, hass_storage, control=True)
    washer = haier(hass, entry).get_device_by_id(WASHER)
    await ready(hass, laundry, washer, "FINISHED", remote=False)
    with pytest.raises(HomeAssistantError) as err:
        await press(hass, WASHER, "start")
    assert (err.value.translation_domain, err.value.translation_key) == (DOMAIN, "laundry_remote_off")
    assert "Washing Machine HW90-B14367U1" in str(err.value)
    assert laundry.commands(WASHER) == []


async def test_pause_resume_cancel(hass: HomeAssistant, laundry: FakeCloud, make_entry, hass_storage) -> None:
    entry = await setup(hass, laundry, make_entry, hass_storage, control=True)
    washer = haier(hass, entry).get_device_by_id(WASHER)
    await ready(hass, laundry, washer, "IN_PROGRESS")
    with pytest.raises(HomeAssistantError) as err:
        await press(hass, WASHER, "start")
    assert err.value.translation_key == "laundry_cannot_start"
    await press(hass, WASHER, "pause")
    await ready(hass, laundry, washer, "PAUSED")
    await press(hass, WASHER, "resume")
    await press(hass, WASHER, "cancel")
    assert [m["command"] for m in laundry.commands(WASHER)] == [
        {"commandName": "19", "value": "2"}, {"commandName": "19", "value": "1"}, {"commandName": "144", "value": "1"}]

    dryer = haier(hass, entry).get_device_by_id(DRYER)
    await ready(hass, laundry, dryer, "IN_PROGRESS")
    await press(hass, DRYER, "pause")
    assert laundry.commands(DRYER) == [
        {"action": "command", "macAddress": DRYER, "command": {"commandName": "5", "value": "2"}}]
