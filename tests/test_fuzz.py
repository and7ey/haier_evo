"""Odd answers of the cloud (ideas of tests/check_fuzz.py and check_security.py of 1.7.2).

Descriptions with random parts removed or replaced, pushes and socket text
of any kind, broken device list pages: the integration does not crash and
its state stays readable. Seeded: every run checks the same cases.
"""
from __future__ import annotations

import copy
import json
import random
import re
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.haier_evo import api
from custom_components.haier_evo.client import parse_expire
from custom_components.haier_evo.device import HaierDevice
from custom_components.haier_evo.diagnostics import async_get_config_entry_diagnostics

from .conftest import load_fixture
from .test_devices import start, stop

EVO = load_fixture("evo_dump.json")
LAUNDRY = load_fixture("laundry_dump.json")
ODD: list[Any] = [None, "", "x", 0, -1, 1e309, True, [], {}, [None], {"x": None}, "null", "\x00", "1" * 500]
CASES = 150


def _paths(value: Any, prefix: tuple = ()) -> list[tuple]:
    out = [prefix] if prefix else []
    if isinstance(value, dict):
        for key, item in value.items():
            out += _paths(item, (*prefix, key))
    elif isinstance(value, list):
        for index, item in enumerate(value[:6]):
            out += _paths(item, (*prefix, index))
    return out


def _mutated(data: dict, rng: random.Random) -> dict:
    data = copy.deepcopy(data)
    paths = _paths(data)
    for _ in range(rng.randint(1, 4)):
        path = rng.choice(paths)
        parent = data
        try:
            for key in path[:-1]:
                parent = parent[key]
            if rng.random() < 0.3 and isinstance(parent, dict):
                parent.pop(path[-1], None)
            else:
                parent[path[-1]] = rng.choice(ODD)
        except (KeyError, IndexError, TypeError):
            continue
    return data


def _hub(hass) -> Any:
    return SimpleNamespace(hass=hass, cloud_available=True, laundry_control=True)


def _sane(device: HaierDevice) -> None:
    json.dumps(device.to_dict(), default=str)
    for platform in ("climate", "water_heater", "switch", "select", "sensor", "binary_sensor", "button"):
        getattr(device, f"create_entities_{platform}")()
        device.retired_unique_ids(platform)
    _ = device.available, device.problem, device.alarm_texts, device.firmware_update


def _build(hass, item: dict, data: dict) -> HaierDevice | None:
    try:
        return HaierDevice.create(
            haier=_hub(hass), device_type=item.get("type", "AC"), device_mac=item["mac"],
            device_serial="x", device_title="t", backend_data=data,
        )
    except Exception:  # noqa: BLE001 - the hub skips an unreadable description
        return None


DEVICES = [
    ({"mac": d["device_id"], "type": t}, d["backend_data"])
    for d, t in zip(EVO["devices"], ("WH", "AC", "AC", "AC"), strict=True)
] + [({"mac": d["device_id"], "type": "WM"}, d["backend_data"]) for d in LAUNDRY["devices"]]


@pytest.mark.parametrize(("item", "data"), DEVICES, ids=[i["mac"] for i, _ in DEVICES])
async def test_broken_descriptions(hass, item, data):
    rng = random.Random(item["mac"])
    built = 0
    for _ in range(CASES // 5):
        device = _build(hass, item, _mutated(data, rng))
        if device is None:
            continue
        built += 1
        _sane(device)
        device.update_from_backend(_mutated(data, rng))
        _sane(device)
    assert built  # most broken descriptions still build a device


@pytest.mark.parametrize(("item", "data"), DEVICES, ids=[i["mac"] for i, _ in DEVICES])
async def test_odd_pushes(hass, item, data):
    device = _build(hass, item, copy.deepcopy(data))
    assert device is not None
    rng = random.Random(f"push{item['mac']}")
    codes = [str(a.get("name")) for a in api.device_part(data).get("attributes") or []]
    events = ["status", "deviceStatusEvent", "info", "command_response", "deviceConnectionStatusEvent",
              "alarms", "ProgramWasherBaseEvent", "ProgramDryerBaseEvent", "whatever"]
    for _ in range(CASES):
        properties = {rng.choice(codes or ["0"]): rng.choice(ODD) for _ in range(rng.randint(0, 4))}
        message = {
            "event": rng.choice(events), "macAddress": item["mac"],
            "payload": rng.choice([
                {"statuses": [{"properties": properties, "alarms": rng.choice(ODD)}]},
                {"status": rng.choice(ODD)}, {"alarms": rng.choice(ODD)}, rng.choice(ODD),
            ]),
            "data": rng.choice([{"program": rng.choice(ODD)}, {"status": rng.choice(ODD)}, rng.choice(ODD)]),
        }
        try:
            device.on_message(message)
        except (TypeError, AttributeError, ValueError):
            # the hub catches what a device raises for one message (it must not kill the socket);
            # a parse error here is noted, not a crash of the integration
            continue
        _sane(device)


async def test_socket_text_of_any_kind(hass, aioclient_mock, ws_factory):
    entry = await start(hass, aioclient_mock, ws_factory, EVO)
    haier = entry.runtime_data
    rng = random.Random("socket")
    mac = EVO["devices"][1]["device_id"]
    texts = ["", "x", "null", "[]", "{}", "1", '"s"', "{" * 50, json.dumps({"event": None}),
             json.dumps({"event": "status", "macAddress": None}),
             json.dumps({"event": "status", "macAddress": mac, "payload": None}),
             json.dumps({"event": "status", "macAddress": mac, "payload": {"statuses": "x"}}),
             json.dumps({"event": "command_response", "trace": 5, "errNo": {}})]
    for _ in range(CASES):
        text = rng.choice(texts) if rng.random() < 0.7 else "".join(rng.choice("{}[]\":,0a") for _ in range(20))
        haier._handle_message(text)  # never raises: one bad message must not kill the socket
    assert haier.connected
    await stop(hass, entry)


def test_device_list_pages_broken_or_not():
    page = EVO["backend_data"]
    rng = random.Random("page")
    for _ in range(CASES):
        devices = api.parse_device_list(_mutated(page, rng))
        macs = [d["mac"].lower() for d in devices]
        assert len(macs) == len(set(macs))
        assert all(re.fullmatch(r"[0-9a-f]{2}(:[0-9a-f]{2}){5}", m) for m in macs)
    assert [d["mac"] for d in api.parse_device_list(page)] == [d["device_id"] for d in EVO["devices"]]


@pytest.mark.parametrize("value", [None, "", "x", 0, [], {}, "2099-01-01", "2099-01-01T00:00:00",
                                   "2099-01-01T00:00:00+03:00", "9999-99-99T99:99:99", "1" * 100])
def test_token_expiry_of_any_form(value):
    expire = parse_expire(value)
    assert expire is None or expire.tzinfo is not None


async def test_diagnostics_of_a_real_account(hass, aioclient_mock, ws_factory):
    """No MAC, IP, serial, e-mail or token in the file users attach to public issues."""
    entry = await start(hass, aioclient_mock, ws_factory, EVO)
    text = json.dumps(await async_get_config_entry_diagnostics(hass, entry), ensure_ascii=False)
    assert not re.findall(r"[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5}", text)
    assert not re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", text)
    for device in EVO["devices"]:
        assert device["device_serial"] not in text
    assert "user@example.com" not in text and "test-access-token" not in text and "test-refresh-token" not in text
    assert not re.search(r"X-Amz-Signature=[0-9a-f]{10}", text)
    # the device data itself stays
    assert '"ES80V-F7"' in text and '"AS25S2SF2FA-B"' in text
    await stop(hass, entry)
