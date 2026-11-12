"""Legacy text diagnostics keep their ID, error codes and cloud explanations."""
from copy import deepcopy

import pytest
from homeassistant.const import EntityCategory
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.haier_evo.const import DOMAIN

from .test_devices import HW90, LAUNDRY, make_entry, mock_account, start, stop

FAULT_UID = f"{HW90['device_id']}_HW90B14367U_fault"


@pytest.mark.parametrize("fault", ["none", "known", "unknown", "long"])
async def test_legacy_error_sensor_survives_upgrade(hass, aioclient_mock, ws_factory, fault):
    dump = deepcopy(LAUNDRY)
    washer = next(d for d in dump["devices"] if d["device_id"] == HW90["device_id"])
    control = washer["backend_data"]["smartDeviceControl"]
    text = "[E2] Ошибка блокировки двери" if fault != "long" else "[E2] " + "Ошибка " * 50
    if fault != "none":
        control["alarms"] = ["mysteryAlarm" if fault == "unknown" else "doorAlarm"]
        control["errors"] = {"alarmToResult": {"doorAlarm": {
            "text": text, "description": "Проверьте, закрыта ли дверца",
        }}}
    mock_account(aioclient_mock, dump)
    entry = make_entry(hass)
    registry = er.async_get(hass)
    old = registry.async_get_or_create(
        "sensor", DOMAIN, FAULT_UID, config_entry=entry, suggested_object_id="existing_washer_error"
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await ws_factory.wait_for(1)
    sensor = hass.states.get(old.entity_id)
    assert sensor is not None and sensor.state != "unavailable"
    assert registry.async_get(old.entity_id).id == old.id
    assert registry.async_get(old.entity_id).entity_category is EntityCategory.DIAGNOSTIC
    if fault == "none":
        assert sensor.state == "Нет активных ошибок"
        assert sensor.attributes["errors"] == []
    elif fault == "unknown":
        assert "mysteryAlarm" in sensor.state
        assert sensor.attributes["errors"][0]["code"] == "mysteryAlarm"
    else:
        assert sensor.state == text[:255]
        assert sensor.attributes["errors"][0]["text"] == text
        assert sensor.attributes["errors"][0]["code"] == "E2"
        assert sensor.attributes["errors"][0]["description"] == "Проверьте, закрыта ли дверца"
    assert ws_factory.current.sent == []

    ws_factory.current.feed({
        "event": "alarms", "macAddress": HW90["device_id"], "payload": {"alarms": []},
    })
    await hass.async_block_till_done()
    assert hass.states.get(old.entity_id).state == "Нет активных ошибок"
    assert hass.states.get(old.entity_id).attributes["errors"] == []
    await stop(hass, entry)


async def test_native_installation_gets_no_legacy_error_sensor(hass, aioclient_mock, ws_factory):
    entry = await start(hass, aioclient_mock, ws_factory, LAUNDRY)
    assert not any(e.unique_id.endswith("_fault") for e in er.async_entries_for_config_entry(
        er.async_get(hass), entry.entry_id
    ))
    assert ws_factory.current.sent == []
    await stop(hass, entry)


async def test_legacy_diagnostic_owned_by_another_entry_is_untouched(hass, aioclient_mock, ws_factory):
    other = MockConfigEntry(domain=DOMAIN, data={"email": "other@example.com", "region": "ru"})
    other.add_to_hass(hass)
    registry = er.async_get(hass)
    old = registry.async_get_or_create("sensor", DOMAIN, FAULT_UID, config_entry=other)
    entry = await start(hass, aioclient_mock, ws_factory, LAUNDRY)
    assert registry.async_get(old.entity_id) == old
    assert hass.states.get(old.entity_id) is None
    await stop(hass, entry)
