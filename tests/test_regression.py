"""What the ACs of evo_dump.json send: the same commands as 1.7.2.

tests/fixtures/regression.json is the snapshot of 1.7.2 (the same file): for
every louver position its name and command, and the
commands of a typical sequence. Automations of its users name louver
positions ("angle_45") and expect the same commands to reach the unit.
"""
from __future__ import annotations

import pytest

from .conftest import load_fixture
from .test_devices import device_by_mac, start, stop

SNAPSHOT = load_fixture("regression.json")


def _commands(sent: list[dict]) -> list[list[dict]]:
    return [message.get("commands") or [message.get("command")] for message in sent]


def _turn_on(device) -> None:
    # as the snapshot does: louvers of an off unit are locked
    device._apply_value(device.config["status"], "1")
    device._apply_value(device.config["mode"], device.config.get_attr_by_name("mode").get_item_code("cool"))


@pytest.mark.parametrize("mac", sorted(SNAPSHOT))
async def test_louver_names_and_commands_as_in_1_7_2(hass, aioclient_mock, ws_factory, mac):
    entry = await start(hass, aioclient_mock, ws_factory, load_fixture("evo_dump.json"))
    device = device_by_mac(entry, mac)
    ws = ws_factory.current
    for key, (name, want) in sorted(SNAPSHOT[mac]["louvers"].items()):
        attr, value = key.split(":")
        item = next(i for i in device.config.get_attr_by_name(attr).list if str(i.value) == value)
        assert item.name == name, key
        if isinstance(want, str):
            continue  # 1.7.2 refused it too
        _turn_on(device)
        before = len(ws.sent)
        await getattr(device, f"set_{attr}")(name)
        assert _commands(ws.sent[before:]) == _commands(want), key
    await stop(hass, entry)


@pytest.mark.parametrize("mac", sorted(SNAPSHOT))
async def test_typical_sequence_as_in_1_7_2(hass, aioclient_mock, ws_factory, mac):
    entry = await start(hass, aioclient_mock, ws_factory, load_fixture("evo_dump.json"))
    device = device_by_mac(entry, mac)
    want = SNAPSHOT[mac]["sequence"]
    assert [device.get_hvac_modes(), device.get_fan_modes(), device.get_preset_modes()] == want["modes"]
    ws = ws_factory.current
    before = len(ws.sent)
    await device.switch_on("cool")
    await device.set_temperature(23)
    await device.set_fan_mode("low")
    await device.set_preset_mode("boost")
    await device.switch_off()
    assert _commands(ws.sent[before:]) == _commands(want["sent"])
    await stop(hass, entry)
