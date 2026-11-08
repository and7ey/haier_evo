"""Home Assistant services to websocket commands and cloud events to states,
through the real service layer and state machine."""
from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.haier_evo import const as C

from .conftest import (
    AC_OFF, AC_ON, BOILER, DOMAIN, REGRESSION, FakeCloud, entity_id, haier, wait_for, wait_state,
)

MODEL = "AS25S2SF2FA"


def climate_id(hass: HomeAssistant, mac: str) -> str:
    return entity_id(hass, "climate", f"{mac}_{MODEL}")


async def climate(hass: HomeAssistant, service: str, mac: str, **data) -> None:
    await hass.services.async_call("climate", service, {"entity_id": climate_id(hass, mac), **data}, blocking=True)


@pytest.fixture
async def ready(hass: HomeAssistant, setup_entry, cloud: FakeCloud):
    """Set up, the websocket open, nothing sent yet."""
    await wait_for(lambda: haier(hass, setup_entry)._connected.is_set(), "the websocket to open")
    assert cloud.commands() == []
    return setup_entry


@pytest.mark.parametrize("mac", sorted(REGRESSION))
async def test_typical_sequence_sends_what_the_snapshot_records(
    hass: HomeAssistant, ready, cloud: FakeCloud, mac: str
) -> None:
    """tests/check_regression.py records what a typical sequence sends, calling
    the device directly; here the same sequence goes through Home Assistant's
    climate services, with their own validation and argument types."""
    expected = REGRESSION[mac]["sequence"]
    await climate(hass, "set_hvac_mode", mac, hvac_mode="cool")
    await climate(hass, "set_temperature", mac, temperature=23)
    await climate(hass, "set_fan_mode", mac, fan_mode="low")
    await climate(hass, "set_preset_mode", mac, preset_mode="boost")
    await climate(hass, "turn_off", mac)
    assert cloud.commands(mac) == expected["sent"]

    state = await wait_state(
        hass, climate_id(hass, mac), "off", temperature=expected["state"]["target_temperature"],
        fan_mode=expected["state"]["fan_mode"], preset_mode=expected["state"]["preset_mode"],
    )
    assert state.attributes["hvac_modes"] == expected["modes"][0]
    assert state.attributes["fan_modes"] == expected["modes"][1]
    assert state.attributes["preset_modes"] == expected["modes"][2]


async def test_status_pushed_by_the_cloud_shows_up(hass: HomeAssistant, ready, cloud: FakeCloud) -> None:
    # power on (21), cooling (2 = 1), setpoint (0), as the cloud pushes them
    await cloud.push(AC_OFF, {"21": "1", "2": "1", "0": "26"})
    await wait_for(lambda: hass.states.get(climate_id(hass, AC_OFF)).state == "cool", "the pushed mode")
    assert hass.states.get(climate_id(hass, AC_OFF)).attributes["temperature"] == 26


async def test_device_offline_and_back(hass: HomeAssistant, ready, cloud: FakeCloud) -> None:
    """The cloud's ONLINE/OFFLINE flaps: it shows in the connectivity sensor,
    the controls stay available (they go unavailable only without the
    websocket, see test_setup.py)."""
    link = entity_id(hass, "binary_sensor", f"{AC_OFF}_{MODEL}_connected")
    assert hass.states.get(link).state == "on"
    await cloud.push(AC_OFF, event="deviceStatusEvent", payload={"status": "OFFLINE"})
    await wait_for(lambda: hass.states.get(link).state == "off", "the device to show offline")
    assert hass.states.get(climate_id(hass, AC_OFF)).state == "off"
    await cloud.push(AC_OFF, event="deviceStatusEvent", payload={"status": "ONLINE"})
    await wait_for(lambda: hass.states.get(link).state == "on", "the device to come back")


async def test_command_the_app_would_not_allow_is_refused(hass: HomeAssistant, ready, cloud: FakeCloud) -> None:
    """The fan of a unit that is off: greyed out in the app, an error here."""
    with pytest.raises(HomeAssistantError) as err:
        await climate(hass, "set_fan_mode", AC_OFF, fan_mode="high")
    assert (err.value.translation_domain, err.value.translation_key) == (DOMAIN, "locked_off")
    assert "Кондиционер 2" in str(err.value)
    assert cloud.commands(AC_OFF) == []


async def test_setpoint_of_a_unit_that_is_off_goes_with_power_on(
    hass: HomeAssistant, ready, cloud: FakeCloud
) -> None:
    await climate(hass, "set_temperature", AC_OFF, temperature=21)
    assert cloud.commands(AC_OFF) == []
    await wait_state(hass, climate_id(hass, AC_OFF), "off", temperature=21)

    await climate(hass, "set_hvac_mode", AC_OFF, hvac_mode="cool")
    [command] = cloud.commands(AC_OFF)
    values = {c["commandName"]: c["value"] for c in command["commands"]}
    assert values["21"] == "1" and values["2"] == "1" and float(values["0"]) == 21


async def test_command_rejected_by_the_cloud(hass: HomeAssistant, ready, cloud: FakeCloud) -> None:
    cloud.err_no = 5
    with pytest.raises(HomeAssistantError) as err:
        await climate(hass, "set_fan_mode", AC_ON, fan_mode="high")
    assert (err.value.translation_domain, err.value.translation_key) == (DOMAIN, "command_rejected")
    assert hass.states.get(climate_id(hass, AC_ON)).attributes["fan_mode"] == "low"


async def test_unconfirmed_command_is_sent_again_over_a_new_session(
    hass: HomeAssistant, ready, cloud: FakeCloud, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(C, "WS_ACK_TIMEOUT", 0.5)
    cloud.answer_commands = False
    call = hass.async_create_task(climate(hass, "set_fan_mode", AC_ON, fan_mode="high"))
    await wait_for(lambda: len(cloud.commands(AC_ON)) == 1, "the first try")
    cloud.answer_commands = True
    await call
    first, second = cloud.commands(AC_ON)
    assert first == second
    assert len(cloud.ws_tokens) >= 2, "the second try goes over a new session"
    await wait_state(hass, climate_id(hass, AC_ON), fan_mode="high")


async def test_without_the_cloud_a_command_fails_loudly(
    hass: HomeAssistant, ready, cloud: FakeCloud, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(C, "WS_SEND_WAIT", 1)
    cloud.ws_refused = True
    await cloud.drop_sessions()
    await wait_for(lambda: not haier(hass, ready)._connected.is_set(), "the session to drop")
    with pytest.raises(HomeAssistantError) as err:
        await climate(hass, "set_fan_mode", AC_ON, fan_mode="high")
    assert (err.value.translation_domain, err.value.translation_key) == (DOMAIN, "not_connected")
    assert cloud.commands(AC_ON) == []


async def test_switch(hass: HomeAssistant, ready, cloud: FakeCloud) -> None:
    light = entity_id(hass, "switch", f"{AC_ON}_{MODEL}_light")
    assert hass.states.get(light).state == "off"
    await hass.services.async_call("switch", "turn_on", {"entity_id": light}, blocking=True)
    [command] = cloud.commands(AC_ON)
    assert {"commandName": "12", "value": "1"} in command["commands"]
    await wait_state(hass, light, "on")


async def test_water_heater(hass: HomeAssistant, ready, cloud: FakeCloud) -> None:
    heater = entity_id(hass, "water_heater", f"{BOILER}_ES80VF7")
    await hass.services.async_call("water_heater", "set_temperature",
                                   {"entity_id": heater, "temperature": 60}, blocking=True)
    await hass.services.async_call("water_heater", "set_operation_mode",
                                   {"entity_id": heater, "operation_mode": "eco"}, blocking=True)
    await hass.services.async_call("water_heater", "turn_off", {"entity_id": heater}, blocking=True)
    sent = [(m["command"]["commandName"], m["command"]["value"]) for m in cloud.commands(BOILER)]
    assert sent == [("1", "60"), ("41", "1"), ("35", "0")]
    await wait_state(hass, heater, "off", temperature=60)

    # while it is off the temperature cannot be changed, as in the app
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call("water_heater", "set_temperature",
                                       {"entity_id": heater, "temperature": 50}, blocking=True)
    assert err.value.translation_key == "locked_off"
