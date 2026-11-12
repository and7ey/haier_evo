"""A refrigerator on the real description of a C4F640CWU1 (#52).

tests/fixtures/ref_dump.json is the anonymised dump of 1.7.2 (the same file);
the expectations are those of its tests/check_refrigerator.py.
"""
from __future__ import annotations

from homeassistant.helpers import entity_registry as er

from custom_components.haier_evo import const as C  # noqa: N812
from custom_components.haier_evo.api import HaierREF

from .conftest import load_fixture
from .test_devices import device_by_mac, start, stop

DUMP = load_fixture("ref_dump.json")
MAC = DUMP["devices"][0]["device_id"]
PREFIX = f"{MAC}_C4F640CWU1"


async def test_refrigerator_entities_and_values(hass, aioclient_mock, ws_factory):
    entry = await start(hass, aioclient_mock, ws_factory, DUMP)
    ref = device_by_mac(entry, MAC)
    assert isinstance(ref, HaierREF)
    assert (ref.device_model, ref.sw_version) == ("C4F640CWU1", "1.5.5")
    assert {
        "current_fridge_temperature": ref.current_fridge_temperature,
        "current_freezer_temperature": ref.current_freezer_temperature,
        "current_temperature": ref.current_temperature,
        "fridge_mode": ref.fridge_mode, "freezer_mode": ref.freezer_mode, "my_zone": ref.my_zone,
        "super_cooling": ref.super_cooling, "super_freeze": ref.super_freeze,
        "vacation_mode": ref.vacation_mode, "door_open": ref.door_open,
    } == {
        "current_fridge_temperature": 5.0, "current_freezer_temperature": -18.0, "current_temperature": 25.0,
        "fridge_mode": "+5", "freezer_mode": "-18", "my_zone": "5",
        "super_cooling": False, "super_freeze": False, "vacation_mode": False, "door_open": False,
    }
    assert ref.get_fridge_mode_options() == ["+2", "+3", "+4", "+5", "+6", "+7", "+8"]
    assert ref.get_freezer_mode_options() == [str(t) for t in range(-24, -15)]
    assert ref.get_my_zone_options() == ["-5", "0", "5"]
    registry = er.async_get(hass)
    ids = {
        (e.domain, e.unique_id.removeprefix(f"{PREFIX}_"))
        for e in er.async_entries_for_config_entry(registry, entry.entry_id)
    }
    for want in [
        ("switch", "super_cooling_switch"), ("switch", "super_freeze_switch"), ("switch", "vacation_mode_switch"),
        ("select", "fridge_mode_select"), ("select", "freezer_mode_select"), ("select", "my_zone"),
        ("sensor", "temperature"), ("sensor", "fridge_temperature"), ("sensor", "freezer_temperature"),
        ("sensor", "fridge_mode"), ("sensor", "freezer_mode"),
        ("binary_sensor", "super_cooling"), ("binary_sensor", "super_freeze"),
        ("binary_sensor", "vacation"), ("binary_sensor", "door_open"),
    ]:
        assert want in ids, want
    assert not any(domain in ("climate", "water_heater") for domain, _ in ids)
    await stop(hass, entry)


async def test_refrigerator_commands(hass, aioclient_mock, ws_factory):
    entry = await start(hass, aioclient_mock, ws_factory, DUMP)
    ref = device_by_mac(entry, MAC)
    ws = ws_factory.current
    calls = [
        (ref.set_fridge_mode, "+3", ("3", "4")),
        (ref.set_freezer_mode, "-20", ("4", "10")),
        (ref.set_my_zone, "0", ("5", "31")),
        (ref.set_super_cooling, True, ("6", "1")),
        (ref.set_super_freeze, False, ("7", "0")),
        (ref.set_vacation_mode, True, ("8", "1")),
    ]
    for method, value, (code, raw) in calls:
        before = len(ws.sent)
        await method(value)
        sent = ws.sent[before:]
        assert [(m["action"], m["command"]["commandName"], m["command"]["value"]) for m in sent] == [
            ("command", code, raw)
        ], method.__name__
    # shown at once, before the fridge echoes it
    assert ref.fridge_mode == "+3"
    # a value the fridge does not list: nothing goes out
    before = len(ws.sent)
    await ref.set_fridge_mode("+99")
    assert len(ws.sent) == before
    assert ref.fridge_mode == "+3"
    await stop(hass, entry)


async def test_refrigerator_push(hass, aioclient_mock, ws_factory):
    entry = await start(hass, aioclient_mock, ws_factory, DUMP)
    ref = device_by_mac(entry, MAC)
    ws_factory.current.feed({
        "event": "status", "macAddress": MAC,
        "payload": {"statuses": [{"properties": {"0": "3", "1": "-21", "2": "24", "6": "1", "9": "1"}}]},
    })
    await hass.async_block_till_done()
    assert (ref.current_fridge_temperature, ref.current_freezer_temperature, ref.current_temperature) == (3.0, -21.0, 24.0)
    assert ref.super_cooling is True
    assert ref.door_open is True
    door = er.async_get(hass).async_get_entity_id("binary_sensor", C.DOMAIN, f"{PREFIX}_door_open")
    assert hass.states.get(door).state == "on"
    await stop(hass, entry)


async def test_refrigerator_never_sends_a_group(hass, aioclient_mock, ws_factory):
    """As 1.x: one plain command, even when the description names a group command."""
    entry = await start(hass, aioclient_mock, ws_factory, DUMP)
    ref = device_by_mac(entry, MAC)
    ref.config.command_name = "5"
    ws = ws_factory.current
    before = len(ws.sent)
    await ref.set_super_cooling(True)
    assert [m["action"] for m in ws.sent[before:]] == ["command"]
    await stop(hass, entry)
