"""State edge cases: refused statuses, deferred setpoints, OFFLINE, the cache, batches of commands."""
from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.haier_evo import config as CFG
from custom_components.haier_evo import const as C
from custom_components.haier_evo.api import HaierAC, HaierREF, device_part

from .conftest import DEVICES_URL, MAC_1, MAC_2, REFRESH_URL, load_fixture, status_url
from .test_entities import entity_id
from .test_resilience import unload
from .test_websocket import climate_id, status_message

# --- every status refused is not a cloud that is back ---


async def test_no_reload_while_every_status_is_refused(
    hass, config_entry, mock_cloud, ws_factory, aioclient_mock
):
    with patch.object(C, "CACHE_RETRY_FIRST", 0.02), patch.object(C, "CACHE_RETRY_MAX", 0.02):
        mock_cloud()
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
        await unload(hass, config_entry)

        # smartHome answers, iot-platform refuses every unit
        aioclient_mock.clear_requests()
        aioclient_mock.post(REFRESH_URL, json=load_fixture("login.json"))
        aioclient_mock.get(DEVICES_URL, json=load_fixture("devices_page.json"))
        aioclient_mock.get(status_url(MAC_1), status=403, text="")
        aioclient_mock.get(status_url(MAC_2), status=403, text="")
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
        haier = config_entry.runtime_data
        await asyncio.sleep(0.2)
        await hass.async_block_till_done()
        assert config_entry.runtime_data is haier  # not reloaded
        assert not any(device.available for device in haier.devices)  # nothing read: not shown as live
    await unload(hass, config_entry)


# --- a repeat of the old setpoint keeps the deferred one ---


async def test_repeated_setpoint_keeps_the_deferred_temperature(hass, setup_entry, ws_factory):
    ws = ws_factory.current
    ws.feed(status_message({"21": "0"}))
    await hass.async_block_till_done()
    await hass.services.async_call(
        "climate", "set_temperature", {"entity_id": climate_id(hass), "temperature": 23}, blocking=True
    )
    # the unit repeats the setpoint it had (24, spelled the way REST does)
    ws.feed(status_message({"0": "24.00"}))
    await hass.async_block_till_done()
    assert hass.states.get(climate_id(hass)).attributes["temperature"] == 23
    ws.sent.clear()
    await hass.services.async_call(
        "climate", "set_hvac_mode", {"entity_id": climate_id(hass), "hvac_mode": "cool"}, blocking=True
    )
    assert {"commandName": "0", "value": "23"} in ws.sent[-1]["commands"]


async def test_new_setpoint_from_the_cloud_drops_the_deferred_one(hass, setup_entry, ws_factory):
    ws = ws_factory.current
    ws.feed(status_message({"21": "0"}))
    await hass.async_block_till_done()
    await hass.services.async_call(
        "climate", "set_temperature", {"entity_id": climate_id(hass), "temperature": 23}, blocking=True
    )
    ws.feed(status_message({"0": "26"}))  # set in the app meanwhile
    await hass.async_block_till_done()
    assert hass.states.get(climate_id(hass)).attributes["temperature"] == 26
    ws.sent.clear()
    await hass.services.async_call(
        "climate", "set_hvac_mode", {"entity_id": climate_id(hass), "hvac_mode": "cool"}, blocking=True
    )
    assert all(c["commandName"] != "0" for m in ws.sent for c in m["commands"])


# --- OFFLINE wins over a snapshot in flight and a trailing status ---


async def test_offline_survives_a_resync_in_flight(hass, setup_entry, ws_factory):
    haier = setup_entry.runtime_data
    device = haier.get_device_by_id(MAC_1)
    online = load_fixture("status_as25php3hra.json")
    real = haier.client.async_get_device_status

    async def slow_status(mac: str) -> dict:
        if mac == MAC_1:
            # OFFLINE arrives while the request is in flight; the answer says ONLINE
            ws_factory.current.feed(
                {"event": "deviceStatusEvent", "macAddress": MAC_1, "payload": {"status": "OFFLINE"}}
            )
            await asyncio.sleep(0.05)
            return copy.deepcopy(online)
        return await real(mac)

    with patch.object(haier.client, "async_get_device_status", slow_status):
        await haier._async_resync()
    await hass.async_block_till_done()
    assert not device.connected
    assert device.available  # OFFLINE is the connectivity sensor's, not availability (1.4)


async def test_status_packet_does_not_bring_an_offline_unit_back(hass, setup_entry, ws_factory):
    ws = ws_factory.current
    ws.feed({"event": "deviceStatusEvent", "macAddress": MAC_1, "payload": {"status": "OFFLINE"}})
    ws.feed(status_message({"36": "21"}))
    await hass.async_block_till_done()
    assert not setup_entry.runtime_data.get_device_by_id(MAC_1).connected


# --- no indoorTemperature in sensors ---


def _ac(hass: HomeAssistant, data: dict) -> HaierAC:
    haier = SimpleNamespace(hass=hass, cloud_available=True)
    return HaierAC(haier=haier, device_mac=MAC_1, backend_data=data)


async def test_room_temperature_without_the_sensor_name(hass):
    data = load_fixture("status_as25php3hra.json")
    data.pop("sensors")
    device = await hass.async_add_executor_job(_ac, hass, data)
    assert device.current_temperature == 22.5  # code 36, not 38 (11 °C)
    device._set_attribute_value("38", "9")
    assert device.current_temperature == 22.5


async def test_single_room_temperature_without_the_sensor_name(hass):
    data = load_fixture("status_as25php3hra.json")
    data.pop("sensors")
    data["attributes"] = [a for a in data["attributes"] if a.get("name") != "38"]
    device = await hass.async_add_executor_job(_ac, hass, data)
    assert device.current_temperature == 22.5


# --- the cache keeps what came over the socket ---


async def test_cache_holds_pushed_values(hass, setup_entry, ws_factory):
    haier = setup_entry.runtime_data
    ws = ws_factory.current
    ws.feed(status_message({"0": "19", "36": "20.5"}))
    ws.feed({
        "event": "status",
        "macAddress": MAC_1,
        "payload": {"statuses": [{"properties": {}, "alarms": ["indoorFanErr"]}]},
    })
    await hass.async_block_till_done()
    saved = next(d for d in haier.cache_dict()["devices"] if d["mac"] == MAC_1)["status"]
    values = {a["name"]: a.get("currentValue") for a in saved["attributes"]}
    assert values["0"] == "19"
    assert values["36"] == "20.5"
    assert saved["alarms"] == ["indoorFanErr"]
    ws.feed({"event": "deviceStatusEvent", "macAddress": MAC_1, "payload": {"status": "OFFLINE"}})
    await hass.async_block_till_done()
    saved = next(d for d in haier.cache_dict()["devices"] if d["mac"] == MAC_1)["status"]
    assert saved["status"] == "OFFLINE"
    assert entity_id(hass, "sensor", "temperature")  # entities untouched


# --- fridge and washer send the whole batch ---


async def test_fridge_sends_every_command_of_a_batch():
    device = object.__new__(HaierREF)
    batch = [{"commandName": "1", "value": "1"}, {"commandName": "2", "value": "0"}]
    sent: list = []

    async def send(commands: list[dict]) -> None:
        sent.append(commands)

    device.get_commands = lambda name, value: batch
    device._send_commands = send
    await device.set_super_cooling(True)
    assert sent == [batch]


# --- Temperature.mappings is per instance ---


def test_temperature_mappings_are_per_instance():
    first = CFG.Temperature({"data": "3", "name": "3℃"})
    second = CFG.Temperature({"data": "5", "name": "5℃"})
    assert "3℃" not in CFG.Temperature.mappings
    assert "3℃" not in second.mappings
    assert first.name == "3"
    assert second.name == "5"


# --- the model of the description, as 1.7 read it (R11 of #106) ---


@pytest.mark.parametrize(("model", "expected"), [(None, "AC"), ("", ""), ("AS25-PHP3/HRA", "AS25PHP3HRA")])
def test_model_of_the_description_is_read_as_in_1_7(hass, model, expected):
    """info.setdefault("model", "AC") of 1.7: only a missing key is "AC", unique_ids stay."""
    data = copy.deepcopy(load_fixture("status_as25php3hra.json"))
    info = device_part(data).setdefault("info", {})
    if model is None:
        info.pop("model", None)
    else:
        info["model"] = model
    assert _ac(hass, data).device_model == expected
