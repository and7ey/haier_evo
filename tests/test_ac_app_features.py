"""Air conditioner features of 1.x on the asyncio code.

The description of AS25S2SF2FA-B comes from the anonymised dump
tests/fixtures/evo_dump.json: unlike the synthetic fixtures it has the app's
screens (modes.itemsV2), which name louver positions and features.
"""
from __future__ import annotations

import copy

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.haier_evo import config as CFG  # noqa: N812
from custom_components.haier_evo import const as C  # noqa: N812

from .conftest import DEVICES_URL, LOGIN_URL, MAC_1, MAC_2, REFRESH_URL, load_fixture, status_url

UID = f"{MAC_1}_AS25S2SF2FA"


@pytest.fixture
async def s2sf_entry(hass, config_entry, aioclient_mock, ws_factory):
    """MAC_1 described as the AS25S2SF2FA-B of evo_dump.json, with the app's screens."""
    aioclient_mock.post(LOGIN_URL, json=load_fixture("login.json"))
    aioclient_mock.post(REFRESH_URL, json=load_fixture("login.json"))
    aioclient_mock.get(DEVICES_URL, json=load_fixture("devices_page.json"))
    aioclient_mock.get(status_url(MAC_1), json=load_fixture("status_as25s2sf2fa.json"))
    aioclient_mock.get(status_url(MAC_2), json=load_fixture("status_as70s2sf2fa.json"))
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    await ws_factory.wait_for(1)
    yield config_entry
    await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()


def eid(hass, domain: str, suffix: str = "") -> str | None:
    return er.async_get(hass).async_get_entity_id(domain, C.DOMAIN, f"{UID}{suffix}")


def device_of(entry):
    return next(d for d in entry.runtime_data.devices if d.device_id == MAC_1)


# --- louvers as the app names them ---


async def test_louver_positions_named_as_in_the_app(hass, s2sf_entry):
    state = hass.states.get(eid(hass, "climate"))
    modes = state.attributes["swing_modes"]
    for key in ("auto", "angle_0", "angle_30", "angle_45", "angle_60", "angle_90"):
        assert key in modes, key
    # positions the app does not offer (e.g. code 9, "only for a special model") are not options
    assert "special" not in modes
    assert all(not m.startswith("position_") for m in modes)
    assert "auto" in state.attributes["swing_horizontal_modes"]


async def test_hidden_current_louver_position_is_shown(hass, s2sf_entry, ws_factory):
    """check_louvers §7 of 1.7: set with the remote, a position the app does not offer is appended."""
    state = hass.states.get(eid(hass, "climate"))
    vertical, horizontal = state.attributes["swing_modes"], state.attributes["swing_horizontal_modes"]
    ws_factory.current.feed(
        {"event": "status", "macAddress": MAC_1, "payload": {"statuses": [{"properties": {"1": "9", "24": "2"}}]}}
    )
    await hass.async_block_till_done()
    state = hass.states.get(eid(hass, "climate"))
    assert state.attributes["swing_mode"] == "special"
    assert state.attributes["swing_modes"] == [*vertical, "special"]
    assert state.attributes["swing_horizontal_mode"] == "position_3"
    assert state.attributes["swing_horizontal_modes"] == [*horizontal, "position_3"]
    # back to an offered one: nothing extra
    ws_factory.current.feed(
        {"event": "status", "macAddress": MAC_1, "payload": {"statuses": [{"properties": {"1": "8", "24": "7"}}]}}
    )
    await hass.async_block_till_done()
    state = hass.states.get(eid(hass, "climate"))
    assert state.attributes["swing_modes"] == vertical
    assert state.attributes["swing_horizontal_modes"] == horizontal


def test_with_current_adds_only_what_is_missing():
    from custom_components.haier_evo.climate import HaierACEntity

    assert HaierACEntity._with_current(None, "x") is None
    assert HaierACEntity._with_current(["a"], None) == ["a"]
    assert HaierACEntity._with_current(["a"], "a") == ["a"]
    assert HaierACEntity._with_current(["a"], "b") == ["a", "b"]


async def test_named_louver_position_sends_its_code(hass, s2sf_entry, ws_factory):
    ws = ws_factory.current
    # the unit of the dump is off: louvers are locked then
    ws.feed({"event": "status", "macAddress": MAC_1, "payload": {"statuses": [{"properties": {"21": "1"}}]}})
    await hass.async_block_till_done()
    await hass.services.async_call(
        "climate", "set_swing_mode", {"entity_id": eid(hass, "climate"), "swing_mode": "angle_45"}, blocking=True
    )
    commands = ws.sent[-1]["commands"]
    assert {"commandName": "1", "value": "5"} in commands


def test_louver_layout_reads_the_screens():
    layout = CFG.louver_layout(load_fixture("status_as25s2sf2fa.json"))
    assert layout["1"][0] == ("8", "auto")
    assert ("5", "angle_45") in layout["1"]


def test_no_screens_no_layout():
    assert CFG.louver_layout(load_fixture("status_as25php3hra.json")) == {}
    assert CFG.app_screens(load_fixture("status_as25php3hra.json")) == {}


# --- features named and offered as in the app ---


async def test_switch_names_come_from_the_app(hass, s2sf_entry):
    screens = CFG.app_screens(load_fixture("status_as25s2sf2fa.json"))
    light = er.async_get(hass).async_get(eid(hass, "switch", "_light"))
    assert light.original_name == screens["12"]


async def test_feature_the_app_does_not_offer_is_removed(hass, config_entry, aioclient_mock, ws_factory):
    """"Звуковой сигнал" (14): AS25S2SF2FA refuses it, the app has no screen for it."""
    registry = er.async_get(hass)
    old = registry.async_get_or_create("switch", C.DOMAIN, f"{UID}_sound", config_entry=config_entry)
    aioclient_mock.post(LOGIN_URL, json=load_fixture("login.json"))
    aioclient_mock.get(DEVICES_URL, json=load_fixture("devices_page.json"))
    aioclient_mock.get(status_url(MAC_1), json=load_fixture("status_as25s2sf2fa.json"))
    aioclient_mock.get(status_url(MAC_2), json=load_fixture("status_as70s2sf2fa.json"))
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert registry.async_get(old.entity_id) is None
    assert eid(hass, "switch", "_light") is not None
    await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()


async def test_without_screens_everything_is_offered(hass, setup_entry):
    registry = er.async_get(hass)
    uid = f"{MAC_1}_AS25PHP3HRA"
    assert registry.async_get_entity_id("switch", C.DOMAIN, f"{uid}_light") is not None
    light = registry.async_get(registry.async_get_entity_id("switch", C.DOMAIN, f"{uid}_light"))
    # no app screens: the cloud's flat-list name, as 1.7 named it (R10 of #106)
    assert light.original_name == "Подсветка блока"


# --- new sensors ---


async def test_room_temperature_sensor(hass, setup_entry):
    sensor = er.async_get(hass).async_get_entity_id("sensor", C.DOMAIN, f"{MAC_1}_AS25PHP3HRA_temperature")
    assert hass.states.get(sensor).state == "22.5"


async def test_filter_reminder_is_no_fault(hass, s2sf_entry, ws_factory):
    ws_factory.current.feed({
        "event": "status", "macAddress": MAC_1,
        "payload": {"statuses": [{"properties": {}, "alarms": ["filterCleaningAlarm", "alarmCancel"]}]},
    })
    await hass.async_block_till_done()
    assert hass.states.get(eid(hass, "binary_sensor", "_filter")).state == "on"
    problem = hass.states.get(eid(hass, "binary_sensor", "_problem"))
    assert problem.state == "off"
    assert problem.attributes["alarms"] == ["filterCleaningAlarm"]  # alarmCancel is no alarm


async def test_connectivity_sensor_shows_offline(hass, s2sf_entry, ws_factory):
    connected = eid(hass, "binary_sensor", "_connected")
    assert hass.states.get(connected).state == "on"
    ws = ws_factory.current
    ws.feed({"event": "deviceConnectionStatusEvent", "macAddress": MAC_1, "payload": {"status": "GOOD"}})
    ws.feed({"event": "deviceStatusEvent", "macAddress": MAC_1, "payload": {"status": "OFFLINE"}})
    await hass.async_block_till_done()
    state = hass.states.get(connected)
    assert state.state == "off"
    assert state.attributes["link"] == "GOOD"
    # the rest of the unit stays available: OFFLINE flaps for working units (1.4)
    assert hass.states.get(eid(hass, "climate")).state != "unavailable"


async def test_firmware_update_sensor(hass, s2sf_entry):
    device = device_of(s2sf_entry)
    sensor = eid(hass, "binary_sensor", "_firmware_update")
    state = hass.states.get(sensor)
    assert state.state == "off"
    assert state.attributes["installed_version"] == "1.6.0"
    assert state.attributes["auto_update"] is True
    status = copy.deepcopy(device.status_data)
    status["settings"]["firmware"]["updateRequired"] = True
    device.update_from_backend(status)
    device.notify()
    await hass.async_block_till_done()
    assert hass.states.get(sensor).state == "on"


# --- locks say why ---


async def test_lock_in_a_mode_names_the_mode(hass, setup_entry, ws_factory):
    from .test_websocket import status_message

    ws_factory.current.feed(status_message({"2": "0"}))
    await hass.async_block_till_done()
    device = setup_entry.runtime_data.devices[0]
    with pytest.raises(HomeAssistantError) as err:
        await device.set_quiet_on(True)
    assert err.value.translation_key == "locked_mode"
    assert err.value.translation_placeholders["mode"] == device.cloud_value_name("2", "0")
    assert err.value.translation_placeholders["mode"] != "0"


async def test_lock_by_another_feature_names_it(hass, setup_entry, ws_factory):
    device = setup_entry.runtime_data.devices[0]
    device._status_data = copy.deepcopy(device.status_data)
    device._status_data["modifiers"].append({
        "priority": 0,
        "trigger": {"operator": "OR", "conditions": {"condition": [{"mode": "20", "values": ["1"]}]}},
        "actions": [{"name": "8", "rewriteFields": "W", "writable": False}],
    })
    device._raw["20"] = "1"
    with pytest.raises(HomeAssistantError) as err:
        await device.set_light_on(False)
    assert err.value.translation_key == "locked_by"
    assert err.value.translation_placeholders["cause"] == device.app_title("20", "20")
