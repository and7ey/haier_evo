"""Water heaters, washing machines and dryers, and the update from every 1.x release.

The accounts are anonymised dumps of real ones (the files of 1.7.2):
evo_dump.json (water heater ES80V-F7 and
three AS25S2SF2FA-B) and laundry_dump.json (washers HW60, HW90, dryer HD80).
"""
from __future__ import annotations

import copy

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.haier_evo import const as C  # noqa: N812
from custom_components.haier_evo.device import HaierCommandLocked
from custom_components.haier_evo.device_laundry import HaierLaundry, laundry_actions, laundry_programs
from custom_components.haier_evo.device_wh import HaierWH

from .conftest import DEVICES_URL, EMAIL, LOGIN_URL, REFRESH_URL, load_fixture, status_url

EVO = load_fixture("evo_dump.json")
LAUNDRY = load_fixture("laundry_dump.json")
WH_MAC = "5c:01:3b:00:00:01"
WH_UID = f"{WH_MAC}_ES80VF7"
HW90 = next(d for d in LAUNDRY["devices"] if "HW90" in d["device_name"])
HD80 = next(d for d in LAUNDRY["devices"] if "HD80" in d["device_name"])


def mock_account(aioclient_mock, dump: dict) -> None:
    aioclient_mock.clear_requests()
    aioclient_mock.post(LOGIN_URL, json=load_fixture("login.json"))
    aioclient_mock.post(REFRESH_URL, json=load_fixture("login.json"))
    aioclient_mock.get(DEVICES_URL, json={"data": copy.deepcopy(dump["backend_data"])})
    for device in dump["devices"]:
        aioclient_mock.get(status_url(device["device_id"]), json=copy.deepcopy(device["backend_data"]))


def make_entry(hass, options: dict | None = None) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=C.DOMAIN, title=f"{EMAIL} (ru)", unique_id=f"{EMAIL}_ru",
        data={"email": EMAIL, "password": "x" * 8, "region": "ru"}, options=options or {},
    )
    entry.add_to_hass(hass)
    return entry


async def start(hass, aioclient_mock, ws_factory, dump: dict, options: dict | None = None):
    mock_account(aioclient_mock, dump)
    entry = make_entry(hass, options)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    await ws_factory.wait_for(1)
    return entry


async def stop(hass, entry) -> None:
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


def eid(hass, platform: str, unique_id: str) -> str | None:
    return er.async_get(hass).async_get_entity_id(platform, C.DOMAIN, unique_id)


def device_by_mac(entry, mac: str):
    return next(d for d in entry.runtime_data.devices if d.device_id == mac)


# --- water heaters ---


async def test_water_heater(hass, aioclient_mock, ws_factory):
    entry = await start(hass, aioclient_mock, ws_factory, EVO)
    heater = device_by_mac(entry, WH_MAC)
    assert isinstance(heater, HaierWH)
    state = hass.states.get(eid(hass, "water_heater", WH_UID))
    assert state.attributes["operation_list"] == ["off", "eco", "performance"]
    assert state.attributes["current_temperature"] == heater.current_temperature
    assert state.attributes["temperature"] == heater.target_temperature
    for platform, suffix in (("sensor", "temperature"), ("switch", "sterilization"),
                             ("binary_sensor", "heating"), ("binary_sensor", "problem"),
                             ("binary_sensor", "connected"), ("binary_sensor", "firmware_update")):
        assert eid(hass, platform, f"{WH_UID}_{suffix}"), suffix
    await stop(hass, entry)


async def test_water_heater_commands(hass, aioclient_mock, ws_factory):
    entry = await start(hass, aioclient_mock, ws_factory, EVO)
    heater = device_by_mac(entry, WH_MAC)
    ws = ws_factory.current
    heater._apply_value(heater.config["status"], "1")
    await hass.services.async_call(
        "water_heater", "set_temperature", {"entity_id": eid(hass, "water_heater", WH_UID), "temperature": 61},
        blocking=True,
    )
    sent = ws.sent[-1]
    commands = sent.get("commands") or [sent.get("command")]
    assert {"commandName": heater.config["target_temperature"], "value": "61"} in commands
    assert heater.target_temperature == 61

    # a mode of a heater that is off turns it on in the same go
    heater._apply_value(heater.config["status"], "0")
    before = len(ws.sent)
    await hass.services.async_call(
        "water_heater", "set_operation_mode",
        {"entity_id": eid(hass, "water_heater", WH_UID), "operation_mode": "performance"}, blocking=True,
    )
    names = [
        c["commandName"] for message in ws.sent[before:]
        for c in (message.get("commands") or [message.get("command")])
    ]
    assert heater.config["status"] in names and heater.config["heating_mode"] in names
    assert hass.states.get(eid(hass, "water_heater", WH_UID)).state == "performance"
    await stop(hass, entry)


async def test_unknown_heating_mode_is_a_translated_error(hass, aioclient_mock, ws_factory):
    entry = await start(hass, aioclient_mock, ws_factory, EVO)
    heater = device_by_mac(entry, WH_MAC)
    with pytest.raises(HaierCommandLocked) as err:
        await heater.set_heating_mode("junk")
    assert err.value.translation_key == "value_not_allowed"
    assert err.value.translation_placeholders["allowed"] == ", ".join(heater.get_heating_modes())
    await stop(hass, entry)


async def test_water_heater_locked_while_off(hass, aioclient_mock, ws_factory):
    """The cloud's rule: off, the setpoint cannot be changed (1.1)."""
    entry = await start(hass, aioclient_mock, ws_factory, EVO)
    heater = device_by_mac(entry, WH_MAC)
    heater._apply_value(heater.config["status"], "0")
    locked, _ = heater._modifier_locks()
    if heater.config["target_temperature"] not in locked:
        pytest.skip("the dump's rules do not lock the setpoint of an off heater")
    with pytest.raises(HomeAssistantError):
        await heater.set_temperature(50)
    await stop(hass, entry)


async def test_es100v_f7_of_the_diagnostics_of_101(hass):
    """ES100V-F7 (fixtures/wh_dump.json, and7ey/haier_evo#101): its profile, the codes of ES80V-F7."""
    from types import SimpleNamespace

    item = load_fixture("wh_dump.json")["devices"][0]
    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    hub = SimpleNamespace(hass=hass, cloud_available=True, laundry_control=False, async_send=send)
    heater = HaierWH(haier=hub, device_mac=item["device_id"], device_serial="x",
                     device_title=item["device_name"], backend_data=copy.deepcopy(item["backend_data"]))

    def commands() -> list[tuple[str, str]]:
        out = [(c["commandName"], c["value"]) for m in sent for c in (m.get("commands") or [m.get("command")])]
        sent.clear()
        return out

    assert (heater.device_model, heater.config.command_name) == ("ES100VF7", None)
    assert {n: heater.config[n] for n in ("current_temperature", "target_temperature", "status",
                                          "sterilization", "heating_mode", "heating_status")} == {
        "current_temperature": "0", "target_temperature": "1", "status": "35",
        "sterilization": "38", "heating_mode": "41", "heating_status": "43",
    }
    assert (heater.current_temperature, heater.target_temperature, heater.status, heater.heating_mode,
            heater.heating, heater.sterilization_on) == (72.0, 75.0, True, "eco", False, False)
    assert (heater.min_temperature, heater.max_temperature, heater.get_heating_modes()) == (
        35.0, 75.0, ["eco", "performance"])
    await heater.set_temperature(74)
    assert commands() == [("1", "74")]
    await heater.set_heating_mode("performance")
    assert commands() == [("41", "2")]
    await heater.set_sterilization_on(True)
    assert commands() == [("38", "1")]
    heater._apply_value("38", "1")
    with pytest.raises(HaierCommandLocked) as err:
        await heater.set_temperature(60)
    assert err.value.translation_key == "locked_by"
    await heater.switch_off()
    assert commands() == [("35", "0")]


# --- the update from every 1.x release keeps every entity ---


async def test_every_entity_of_1x_releases_is_kept(hass, aioclient_mock, ws_factory):
    """What each release created for the evo_dump account: here, or gone on purpose."""
    releases = load_fixture("entities_by_release.json")
    entry = await start(hass, aioclient_mock, ws_factory, EVO)
    registry = er.async_get(hass)
    current = {(e.domain, e.unique_id) for e in er.async_entries_for_config_entry(registry, entry.entry_id)}
    # retired on purpose: code 38 (1.6.0), "Звуковой сигнал" of AS25S2SF2FA
    # (not offered by the app), the debug switches of 0.3.x
    gone = ("_outdoor_temperature", "_sound", "http_switch")
    devices = {
        tuple(map(tuple, d.identifiers))[0] for d in dr.async_entries_for_config_entry(dr.async_get(hass),
                                                                                       entry.entry_id)
    }
    for name, release in releases.items():
        if name == "_":
            continue
        for platform, unique_id in release["entities"]:
            if any(unique_id.endswith(g) or g in unique_id for g in gone):
                continue
            assert (platform, unique_id) in current, f"{name}: {platform} {unique_id}"
        for identifiers in release["devices"]:
            assert tuple(map(tuple, identifiers))[0] in devices, f"{name}: device {identifiers}"
    await stop(hass, entry)


# --- washing machines and dryers ---


async def test_laundry_read_only_by_default(hass, aioclient_mock, ws_factory):
    entry = await start(hass, aioclient_mock, ws_factory, LAUNDRY)
    for machine in (HW90, HD80):
        device = device_by_mac(entry, machine["device_id"])
        assert isinstance(device, HaierLaundry)
        prefix = f"{device.device_id}_{device.device_model}"
        state = hass.states.get(eid(hass, "sensor", f"{prefix}_state"))
        assert state.state == (device.state or "unknown")
        assert state.attributes["options"] == ["off", "idle", "running", "paused", "finished", "cancelled"]
        assert eid(hass, "sensor", f"{prefix}_program")
        assert eid(hass, "binary_sensor", f"{prefix}_problem")
        # no control without the option
        assert eid(hass, "select", f"{prefix}_laundry_program") is None
        assert eid(hass, "button", f"{prefix}_laundry_start") is None
    await stop(hass, entry)


async def test_laundry_codes_from_the_app_screens(hass, aioclient_mock, ws_factory):
    entry = await start(hass, aioclient_mock, ws_factory, LAUNDRY)
    washer = device_by_mac(entry, HW90["device_id"])
    for meaning in ("onOffStatus", "remoteCtrValid", "cyclePhase"):
        assert meaning in washer.codes, meaning
    assert {"remainingTimeHH", "remainingTimeMM"} <= set(washer.codes) or "remainingDryTime" in washer.codes
    dryer = device_by_mac(entry, HD80["device_id"])
    assert dryer.is_dryer
    await stop(hass, entry)


def test_programs_and_actions_from_the_descriptions():
    for machine in (HW90, HD80):
        programs = laundry_programs(machine["backend_data"])
        assert programs, machine["device_name"]
        assert all(p["commands"] and p["group"] for p in programs)
        assert len({p["title"] for p in programs}) == len(programs)
        actions = laundry_actions(machine["backend_data"])
        assert set(actions) <= {"pause", "resume", "cancel"}


async def test_laundry_control_with_the_option(hass, aioclient_mock, ws_factory):
    entry = await start(hass, aioclient_mock, ws_factory, LAUNDRY, options={"laundry_control": True})
    washer = device_by_mac(entry, HW90["device_id"])
    prefix = f"{washer.device_id}_{washer.device_model}"
    select = eid(hass, "select", f"{prefix}_laundry_program")
    start_button = eid(hass, "button", f"{prefix}_laundry_start")
    assert select and start_button
    program = washer.programs[-1]["title"]
    await hass.services.async_call("select", "select_option", {"entity_id": select, "option": program},
                                   blocking=True)
    assert washer.selected_program == program

    # remote control off at the machine: nothing goes out
    washer._apply_value(washer.codes["remoteCtrValid"], "0")
    ws = ws_factory.current
    before = len(ws.sent)
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call("button", "press", {"entity_id": start_button}, blocking=True)
    assert err.value.translation_key == "laundry_remote_off"
    assert len(ws.sent) == before

    # remote control on, the machine idle: the program's group command
    washer._apply_value(washer.codes["remoteCtrValid"], "1")
    washer._apply_value(washer.codes["onOffStatus"], "1")
    washer._apply_value(washer.codes["cyclePhase"], "0")
    washer.program_status = None
    await hass.services.async_call("button", "press", {"entity_id": start_button}, blocking=True)
    sent = ws.sent[-1]
    expected = next(p for p in washer.programs if p["title"] == program)
    assert sent["action"] == "operation"
    assert sent["commandName"] == expected["group"]
    assert sent["commands"] == expected["commands"]
    assert sent["trace"]
    await stop(hass, entry)


async def test_turning_the_control_off_removes_its_entities(hass, aioclient_mock, ws_factory):
    entry = await start(hass, aioclient_mock, ws_factory, LAUNDRY, options={"laundry_control": True})
    washer = device_by_mac(entry, HW90["device_id"])
    prefix = f"{washer.device_id}_{washer.device_model}"
    assert eid(hass, "button", f"{prefix}_laundry_start")
    await stop(hass, entry)
    hass.config_entries.async_update_entry(entry, options={"laundry_control": False})
    mock_account(aioclient_mock, LAUNDRY)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert eid(hass, "button", f"{prefix}_laundry_start") is None
    assert eid(hass, "select", f"{prefix}_laundry_program") is None
    await stop(hass, entry)


async def test_program_event_updates_the_state(hass, aioclient_mock, ws_factory):
    entry = await start(hass, aioclient_mock, ws_factory, LAUNDRY)
    washer = device_by_mac(entry, HW90["device_id"])
    washer._apply_value(washer.codes["onOffStatus"], "1")
    ws_factory.current.feed({
        "event": "ProgramWasherBaseEvent", "macAddress": washer.device_id,
        "data": {"program": {"title": "Хлопок", "status": "PROGRAM_FINISHED"}},
    })
    await hass.async_block_till_done()
    prefix = f"{washer.device_id}_{washer.device_model}"
    assert hass.states.get(eid(hass, "sensor", f"{prefix}_state")).state == "finished"
    assert hass.states.get(eid(hass, "sensor", f"{prefix}_program")).state == "Хлопок"
    await stop(hass, entry)
