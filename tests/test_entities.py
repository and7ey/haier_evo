"""Entities built from live-unit findings: alarms, categories, modifiers."""
from __future__ import annotations

import asyncio

import pytest
from aiohttp import ClientError
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.haier_evo import const as C

from .test_websocket import CLIMATE_UID, climate_id, status_message


def entity_id(hass: HomeAssistant, domain: str, suffix: str) -> str | None:
    return er.async_get(hass).async_get_entity_id(domain, C.DOMAIN, f"{CLIMATE_UID}_{suffix}")


async def test_outdoor_temperature_sensor_is_gone(hass, config_entry, mock_cloud, ws_factory):
    """Code 38 (0.4-0.5, 1.6.0) is removed from the registry, as 1.6.1 does."""
    registry = er.async_get(hass)
    old = registry.async_get_or_create(
        "sensor", C.DOMAIN, f"{CLIMATE_UID}_outdoor_temperature", config_entry=config_entry
    )
    mock_cloud()
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert registry.async_get(old.entity_id) is None
    # code 36 stays the room temperature; code 38 changes nothing
    assert hass.states.get(climate_id(hass)).attributes["current_temperature"] == 22.5
    ws_factory.current.feed(status_message({"38": "9"}))
    await hass.async_block_till_done()
    assert hass.states.get(climate_id(hass)).attributes["current_temperature"] == 22.5
    await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()


async def test_problem_sensor_follows_alarms(hass, setup_entry, ws_factory):
    problem = entity_id(hass, "binary_sensor", "problem")
    assert hass.states.get(problem).state == "off"
    # no category, as in 1.7: actions aimed at an area or a device still reach it
    assert er.async_get(hass).async_get(problem).entity_category is None
    ws = ws_factory.current
    ws.feed({
        "event": "status",
        "macAddress": setup_entry.runtime_data.devices[0].device_mac,
        "payload": {"statuses": [{"properties": {}, "alarms": ["indoorFanErr", "mysteryErr"]}]},
    })
    await hass.async_block_till_done()
    state = hass.states.get(problem)
    assert state.state == "on"
    assert state.attributes["alarms"] == ["indoorFanErr", "mysteryErr"]
    assert state.attributes["errors"] == [
        "[E14] Ошибка вентилятора внутреннего блока.",
        "Неизвестная ошибка (mysteryErr)",
    ]


async def test_duplicates_disabled_and_no_categories(hass, setup_entry):
    """Categories as in 1.7: a switch with one leaves "switch.turn_off" of an area (R9 of #106)."""
    registry = er.async_get(hass)
    quiet = registry.async_get(entity_id(hass, "switch", "quiet"))
    assert quiet.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    for entry in er.async_entries_for_config_entry(registry, setup_entry.entry_id):
        if entry.domain == "switch":
            assert entry.entity_category is None, entry.unique_id
    connected = registry.async_get(entity_id(hass, "binary_sensor", "connected"))
    assert connected.entity_category is EntityCategory.DIAGNOSTIC  # as in 1.7


async def test_special_swing_mode_hidden(hass, setup_entry):
    modes = hass.states.get(climate_id(hass)).attributes["swing_modes"]
    assert "auto" in modes
    assert "special" not in modes


async def test_temperature_of_an_off_unit_is_sent_on_turn_on(hass, setup_entry, ws_factory):
    """Alice may set the temperature before turning the unit on ("включи на 23")."""
    ws = ws_factory.current
    ws.feed(status_message({"21": "0"}))
    await hass.async_block_till_done()
    ws.sent.clear()
    await hass.services.async_call(
        "climate", "set_temperature", {"entity_id": climate_id(hass), "temperature": 23}, blocking=True
    )
    # the cloud locks the setpoint of an off unit: nothing goes out yet
    assert ws.sent == []
    assert hass.states.get(climate_id(hass)).attributes["temperature"] == 23
    await hass.services.async_call(
        "climate", "set_hvac_mode", {"entity_id": climate_id(hass), "hvac_mode": "cool"}, blocking=True
    )
    # one group, as 1.7 sends it: power on, the mode and the setpoint
    assert len(ws.sent) == 1
    assert {"commandName": "21", "value": "1"} in ws.sent[0]["commands"]
    assert {"commandName": "0", "value": "23"} in ws.sent[0]["commands"]
    assert hass.states.get(climate_id(hass)).attributes["temperature"] == 23


async def test_deferred_temperature_survives_the_echo_of_turn_on(hass, setup_entry, ws_factory):
    """The echo of "on" may come before the answer to the command: it used to
    drop the setpoint, which then went out after the turn-on, or never."""
    ws = ws_factory.current
    ws.feed(status_message({"21": "0"}))
    await hass.async_block_till_done()
    await hass.services.async_call(
        "climate", "set_temperature", {"entity_id": climate_id(hass), "temperature": 23}, blocking=True
    )
    ws.sent.clear()
    call = hass.async_create_task(hass.services.async_call(
        "climate", "set_hvac_mode", {"entity_id": climate_id(hass), "hvac_mode": "cool"}, blocking=True
    ))
    async with asyncio.timeout(2):
        while not ws.sent:
            await asyncio.sleep(0.001)
    # the unit echoes power on while the service call still waits for the answer
    ws.feed(status_message({"21": "1"}))
    await call
    await hass.async_block_till_done()
    assert {"commandName": "0", "value": "23"} in ws.sent[0]["commands"]
    assert hass.states.get(climate_id(hass)).attributes["temperature"] == 23



async def test_failed_turn_on_keeps_the_deferred_temperature(hass, setup_entry, ws_factory):
    """No connection for the turn-on: the setpoint goes out with the next one."""
    ws = ws_factory.current
    ws.feed(status_message({"21": "0"}))
    await hass.async_block_till_done()
    await hass.services.async_call(
        "climate", "set_temperature", {"entity_id": climate_id(hass), "temperature": 23}, blocking=True
    )
    ws_factory.fail = ClientError("cloud down")
    ws.fail_send = True
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "climate", "set_hvac_mode", {"entity_id": climate_id(hass), "hvac_mode": "cool"}, blocking=True
        )
    ws_factory.fail = None
    new = await ws_factory.wait_for(len(ws_factory.sockets) + 1, timeout=3)
    await hass.async_block_till_done()
    await hass.services.async_call(
        "climate", "set_hvac_mode", {"entity_id": climate_id(hass), "hvac_mode": "cool"}, blocking=True
    )
    assert {"commandName": "0", "value": "23"} in new.sent[-1]["commands"]

async def test_stale_deferred_temperature_is_dropped(hass, setup_entry, ws_factory):
    """Turned on with the remote meanwhile: a later turn_on must not resend 23."""
    ws = ws_factory.current
    ws.feed(status_message({"21": "0"}))
    await hass.async_block_till_done()
    await hass.services.async_call(
        "climate", "set_temperature", {"entity_id": climate_id(hass), "temperature": 23}, blocking=True
    )
    ws.feed(status_message({"21": "1"}))
    await hass.async_block_till_done()
    ws.sent.clear()
    await hass.services.async_call("climate", "turn_on", {"entity_id": climate_id(hass)}, blocking=True)
    assert all(c["commandName"] != "0" for m in ws.sent for c in m["commands"])


async def test_turn_off_with_temperature_does_not_fail(hass, setup_entry, ws_factory):
    await hass.services.async_call(
        "climate", "set_temperature",
        {"entity_id": climate_id(hass), "temperature": 21, "hvac_mode": "off"}, blocking=True,
    )
    assert hass.states.get(climate_id(hass)).state == "off"


async def test_preset_shows_up_before_the_echo(hass, setup_entry, ws_factory):
    device = setup_entry.runtime_data.devices[0]
    await device.set_preset_mode("sleep")
    assert device.preset_mode == "sleep"
    await device.set_preset_mode("none")
    assert device.preset_mode == "none"


async def test_turn_on_running_unit_with_alarm(hass, setup_entry, ws_factory):
    """An alarm locks the mode, but turn_on without a mode change is not a change."""
    ws = ws_factory.current
    ws.feed({
        "event": "status",
        "macAddress": setup_entry.runtime_data.devices[0].device_mac,
        "payload": {"statuses": [{"properties": {}, "alarms": ["outdoorFanErr"]}]},
    })
    await hass.async_block_till_done()
    await hass.services.async_call("climate", "turn_on", {"entity_id": climate_id(hass)}, blocking=True)


async def test_our_own_turn_off_locks_without_echo(hass, setup_entry, ws_factory):
    await hass.services.async_call("climate", "turn_off", {"entity_id": climate_id(hass)}, blocking=True)
    with pytest.raises(HomeAssistantError) as err:
        await setup_entry.runtime_data.devices[0].set_light_on(False)
    assert err.value.translation_key == "locked_off"


async def test_quiet_locked_in_auto_mode(hass, setup_entry, ws_factory):
    ws_factory.current.feed(status_message({"2": "0"}))
    await hass.async_block_till_done()
    device = setup_entry.runtime_data.devices[0]
    with pytest.raises(HomeAssistantError) as err:
        await device.set_quiet_on(True)
    assert err.value.translation_key == "locked_mode"
    with pytest.raises(HomeAssistantError):
        await device.set_preset_mode("sleep")
    # light is not restricted in auto
    await device.set_light_on(False)


async def test_fan_only_restricts_fan_values(hass, setup_entry, ws_factory):
    ws = ws_factory.current
    ws.feed(status_message({"2": "6"}))
    await hass.async_block_till_done()
    device = setup_entry.runtime_data.devices[0]
    with pytest.raises(HomeAssistantError) as err:
        await device.set_fan_mode("auto")
    assert err.value.translation_key == "value_not_allowed"
    ws.sent.clear()
    await device.set_fan_mode("high")
    assert ws.sent[0]["commands"] == [{"commandName": "4", "value": "1"}]


async def test_alarm_locks_controls(hass, setup_entry, ws_factory):
    ws = ws_factory.current
    ws.feed({
        "event": "status",
        "macAddress": setup_entry.runtime_data.devices[0].device_mac,
        "payload": {"statuses": [{"properties": {}, "alarms": ["outdoorFanErr"]}]},
    })
    await hass.async_block_till_done()
    with pytest.raises(HomeAssistantError) as err:
        await setup_entry.runtime_data.devices[0].set_temperature(22)
    assert err.value.translation_key == "locked_alarm"


async def test_alarms_from_rest_resync(hass, setup_entry):
    device = setup_entry.runtime_data.devices[0]
    device.update_from_backend({**device.status_data, "alarms": [{"name": "indoorFanErr"}]})
    assert device.alarms == ["indoorFanErr"]
    device.update_from_backend({**device.status_data, "alarms": []})
    assert device.alarms == []


async def test_retired_entities_removed_from_registry(hass, config_entry, mock_cloud, ws_factory):
    """An entity earlier versions created and this one does not offer is removed at setup."""
    from unittest.mock import patch

    from custom_components.haier_evo import api

    registry = er.async_get(hass)
    old = registry.async_get_or_create(
        "sensor", C.DOMAIN, f"{CLIMATE_UID}_gone_feature", config_entry=config_entry
    )
    hidden = registry.async_get_or_create(
        "switch", C.DOMAIN, f"{CLIMATE_UID}_hidden_feature", config_entry=config_entry
    )
    kept = registry.async_get_or_create(
        "sensor", C.DOMAIN, f"{CLIMATE_UID}_other", config_entry=config_entry
    )
    original = api.HaierAC.create_entities_switch

    def create_switches(self):
        entities = original(self)
        self.hidden_entities.append(("switch", f"{self.device_id}_{self.device_model}_hidden_feature"))
        return entities

    mock_cloud()
    with (
        patch.object(api.HaierAC, "RETIRED", {"sensor": ("gone_feature",)}),
        patch.object(api.HaierAC, "create_entities_switch", create_switches),
    ):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
    assert registry.async_get(old.entity_id) is None
    assert registry.async_get(hidden.entity_id) is None
    assert registry.async_get(kept.entity_id) is not None
    await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()


async def test_names_are_those_of_1_x(hass, setup_entry):
    """Full names stay as 1.7 made them: device name, then the entity's own."""
    registry = er.async_get(hass)
    problem = registry.async_get_entity_id("binary_sensor", C.DOMAIN, f"{CLIMATE_UID}_problem")
    room = registry.async_get_entity_id("sensor", C.DOMAIN, f"{CLIMATE_UID}_temperature")
    device_name = hass.states.get(climate_id(hass)).attributes["friendly_name"]
    assert hass.states.get(problem).attributes["friendly_name"] == f"{device_name} Неисправность"
    assert hass.states.get(room).attributes["friendly_name"] == f"{device_name} Температура в комнате"
