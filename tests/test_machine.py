from unittest.mock import Mock
from datetime import timedelta
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from homeassistant.exceptions import HomeAssistantError

from custom_components.haier_evo import api
from custom_components.haier_evo.machine import async_setup_machines
from test_laundry import FakeCloud, control_fixture


@pytest.mark.asyncio
async def test_machine_registration_selection_actions_and_unload(hass, enable_custom_integrations):
    entry = MockConfigEntry(domain="haier_evo", data={})
    entry.add_to_hass(hass)
    cloud = FakeCloud(control_fixture())
    cloud.hass = hass
    device = api.HaierDevice.create(cloud, "WM", "synthetic-device", device_title="Machine")
    cloud.devices = [device]
    hass.data.setdefault("haier_evo", {})[entry.entry_id] = cloud
    platform = await async_setup_machines(hass, entry)
    await hass.async_block_till_done()
    assert len(platform.entities) == 1
    entity_id = next(iter(platform.entities))
    assert entity_id.startswith("haier_evo.")
    state = hass.states.get(entity_id)
    assert state.state == "idle"
    assert platform.platform_data.platform_translations["component.haier_evo.entity.haier_evo.laundry_machine.state.idle"] == "Idle"
    assert state.attributes["friendly_name"] == "Machine"
    assert state.attributes["available_actions"] == []
    cloud.socket_app.send.assert_not_called()

    device.set_selected_program("Fabric - Cotton")
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).attributes["available_actions"] == ["start"]
    cloud.socket_app.send.assert_not_called()
    await hass.services.async_call("haier_evo", "execute_action",
                                  {"entity_id": entity_id, "action": "start"}, blocking=True)
    await hass.async_block_till_done()
    assert cloud.socket_app.send.call_count == 1
    assert hass.states.get(entity_id).attributes["available_actions"] == []
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call("haier_evo", "execute_action",
                                      {"entity_id": entity_id, "action": "start"}, blocking=True)
    assert cloud.socket_app.send.call_count == 1

    await platform.async_destroy()
    assert not device._write_ha_state_callbacks
    assert not platform.entities
    assert hass.states.get(entity_id).state == "unavailable"


@pytest.mark.asyncio
async def test_machine_setup_failure_cleans_platform(hass):
    from unittest.mock import AsyncMock, patch
    entry = Mock()
    with patch("custom_components.haier_evo.machine.EntityPlatform") as factory:
        platform = factory.return_value
        platform.async_setup_entry = AsyncMock(return_value=False)
        platform.async_destroy = AsyncMock()
        with pytest.raises(RuntimeError):
            await async_setup_machines(hass, entry)
        platform.async_destroy.assert_awaited_once()


@pytest.mark.asyncio
async def test_observed_washer_finish_and_alarm_events_are_deduplicated(hass, enable_custom_integrations):
    entry = MockConfigEntry(domain="haier_evo", data={})
    entry.add_to_hass(hass)
    data = control_fixture()
    data["smartDeviceControl"]["alarms"] = []
    data["smartDeviceControl"]["errors"] = {"alarmToResult": {
        "doorLockFail": {"text": "[E2] Ошибка блокировки двери",
                         "description": "Закройте дверь и повторите запуск."}}}
    cloud = FakeCloud(data)
    cloud.hass = hass
    device = api.HaierDevice.create(cloud, "WM", "synthetic-device", device_title="Machine")
    cloud.devices = [device]
    hass.data.setdefault("haier_evo", {})[entry.entry_id] = cloud
    events = []
    hass.bus.async_listen("haier_evo_laundry_event", lambda event: events.append(event.data))
    platform = await async_setup_machines(hass, entry)
    await hass.async_block_till_done()
    assert not events

    alarm = {"event": "alarms", "payload": {"alarms": ["doorLockFail"]}}
    device.on_message(alarm)
    device.on_message(alarm)
    await hass.async_block_till_done()
    assert len(events) == 1
    assert events[0]["type"] == "error"
    assert events[0]["errors"][0]["code"] == "E2"
    assert events[0]["entity_id"] in platform.entities

    finish = {"event": "ProgramWasherBaseEvent", "trace": "synthetic-finish",
              "data": {"status": "PROGRAM_FINISHED", "program": {
                  "status": "FINISHED", "title": "Смешанная"}}}
    device.on_message(finish)
    device.on_message(finish)
    await hass.async_block_till_done()
    assert len(events) == 1
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=11))
    await hass.async_block_till_done()
    assert len(events) == 2
    assert events[1]["type"] == "program_finished"
    assert events[1]["program"] == "Смешанная"
    assert events[1]["errors"][0]["code"] == "E2"

    device.on_message({"event": "alarms", "payload": {"alarms": []}})
    await hass.async_block_till_done()
    assert events[-1]["type"] == "error_cleared"
    assert device.error_details == []
    device.on_message({"event": "alarms", "payload": {}})
    await hass.async_block_till_done()
    assert device.error_details is None
    assert len(events) == 3
    cloud.socket_app.send.assert_not_called()
    await platform.async_destroy()


def test_fault_sensor_preserves_unknown_codes_and_manufacturer_details():
    data = control_fixture()
    data["smartDeviceControl"].update({"alarms": ["doorLockFail"],
        "errors": {"alarmToResult": {"doorLockFail": {
            "text": "[E2] Ошибка блокировки двери", "description": "Проверьте дверь."}}}})
    cloud = FakeCloud(data)
    device = api.HaierDevice.create(cloud, "WM", "synthetic-device", device_title="Machine")
    sensor = next(item for item in device.create_entities_sensor()
                  if item.translation_key == "laundry_fault")
    sensor.hass = cloud.hass
    assert sensor.entity_category == "diagnostic"
    assert sensor.native_value == "[E2] Ошибка блокировки двери"
    assert sensor.extra_state_attributes["errors"][0]["description"] == "Проверьте дверь."
    device.on_message({"event": "alarms", "payload": {"alarms": ["newAlarm"]}})
    assert sensor.native_value == "newAlarm"
    device.on_message({"event": "alarms", "payload": {"alarms": []}})
    assert sensor.native_value == "Нет активных ошибок"
    device.on_message({"event": "deviceStatusEvent", "payload": {"status": "OFFLINE"}})
    assert sensor.native_value is None and not sensor.available


@pytest.mark.asyncio
async def test_configuring_program_is_local_validated_and_reset_on_new_selection(hass, enable_custom_integrations):
    from test_laundry import dynamic_delay_fixture
    entry = MockConfigEntry(domain="haier_evo", data={})
    entry.add_to_hass(hass)
    data = dynamic_delay_fixture()
    data["smartDeviceControl"]["businessAttributes"][0]["commandParameters"]["attrNameList"][0].update({
        "valueType": "3", "valueRange": {"list": {"type": "LIST", "data": [{"data": "1"}, {"data": "2"}]}}})
    data["enrichAttributes"] = {"attrs": {"20": {"title": "Temperature"}}}
    data["smartDeviceControl"]["attributes"].append({"name": "20", "writable": True,
        "list": {"data": [{"data": "1"}, {"data": "2"}]}})
    cloud = FakeCloud(data)
    cloud.hass = hass
    device = api.HaierDevice.create(cloud, "WM", "synthetic-device", device_title="Machine")
    cloud.devices = [device]
    hass.data.setdefault("haier_evo", {})[entry.entry_id] = cloud
    platform = await async_setup_machines(hass, entry)
    await hass.async_block_till_done()
    entity_id = next(iter(platform.entities))
    device.set_selected_program("Fabric - Cotton")
    await hass.services.async_call("haier_evo", "configure_program", {
        "entity_id": entity_id, "program": "Fabric - Cotton", "parameters": {"20": "2"}}, blocking=True)
    await hass.async_block_till_done()
    settings = hass.states.get(entity_id).attributes["program_settings"]
    assert settings["program"] == "Fabric - Cotton"
    assert settings["values"]["20"] == "2"
    cloud.socket_app.send.assert_not_called()
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call("haier_evo", "configure_program", {
            "entity_id": entity_id, "program": "Fabric - Cotton", "parameters": {"20": "99"}}, blocking=True)
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call("haier_evo", "configure_program", {
            "entity_id": entity_id, "program": "Other", "parameters": {"20": "1"}}, blocking=True)
    device.set_selected_program(None)
    assert not device.program_settings
    device.set_selected_program("Fabric - Cotton")
    assert device.program_settings["values"]["20"] == "1"
    await hass.services.async_call("haier_evo", "execute_action", {
        "entity_id": entity_id, "action": "start"}, blocking=True)
    import json
    commands = json.loads(cloud.socket_app.send.call_args.args[0])["commands"]
    assert next(c["value"] for c in commands if c["commandName"] == "20") == "1"
    await platform.async_destroy()


@pytest.mark.asyncio
async def test_stage_finished_followed_by_running_does_not_publish_cycle_finished(hass, enable_custom_integrations):
    entry = MockConfigEntry(domain="haier_evo", data={})
    entry.add_to_hass(hass)
    cloud = FakeCloud(control_fixture())
    cloud.hass = hass
    device = api.HaierDevice.create(cloud, "WM", "synthetic-device", device_title="Machine")
    cloud.devices = [device]
    hass.data.setdefault("haier_evo", {})[entry.entry_id] = cloud
    events = []
    hass.bus.async_listen("haier_evo_laundry_event", lambda event: events.append(event.data))
    platform = await async_setup_machines(hass, entry)
    await hass.async_block_till_done()
    for status in ("IN_PROGRESS", "FINISHED", "IN_PROGRESS"):
        device.on_message({"event": "ProgramWasherBaseEvent", "data": {
            "program": {"status": status, "title": "Mixed"}}})
        await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=11))
    await hass.async_block_till_done()
    assert not events and device.program_status == "running"
    device.on_message({"event": "ProgramWasherBaseEvent", "data": {
        "program": {"status": "FINISHED", "title": "Mixed"}}})
    await hass.async_block_till_done()
    await platform.async_destroy()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=22))
    await hass.async_block_till_done()
    assert not events and device._finish_unsub is None
    cloud.socket_app.send.assert_not_called()
