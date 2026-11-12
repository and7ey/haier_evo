"""Setup edge cases: unreadable descriptions, units gone from the account, commands at a reconnect,
the cache file, Retry-After, warnings per outage."""
from __future__ import annotations

import asyncio
import json
import logging
from unittest.mock import patch

import pytest
from aiohttp import ClientError
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_FINAL_WRITE
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.haier_evo import (
    _INCOMPLETE_SETUPS,
    api,
    async_remove_config_entry_device,
)
from custom_components.haier_evo import config as CFG
from custom_components.haier_evo import const as C
from custom_components.haier_evo.api import HaierAC
from custom_components.haier_evo.client import HaierRateLimitError

from .conftest import DEVICES_URL, LOGIN_URL, MAC_1, MAC_2, REFRESH_URL, load_fixture, status_url
from .test_client import make_client
from .test_resilience import cache_key, unload
from .test_websocket import status_message


def cloud(aioclient_mock, *, mac_2=None, list_mac_2: bool = True) -> None:
    """The cloud from scratch; `mac_2` changes the status of the second unit."""
    aioclient_mock.clear_requests()
    aioclient_mock.post(LOGIN_URL, json=load_fixture("login.json"))
    aioclient_mock.post(REFRESH_URL, json=load_fixture("login.json"))
    page = load_fixture("devices_page.json")
    if not list_mac_2:
        container = page["data"]["presentation"]["layout"]["scrollContainer"][1]
        state = json.loads(container["state"])
        state["items"] = [item for item in state["items"] if "53%3A02" not in item["action"]["link"]]
        assert len(state["items"]) == 1
        container["state"] = json.dumps(state)
    aioclient_mock.get(DEVICES_URL, json=page)
    aioclient_mock.get(status_url(MAC_1), json=load_fixture("status_as25php3hra.json"))
    status = load_fixture("status_as70s2sf2fa.json")
    if mac_2 is not None:
        mac_2(status)
    aioclient_mock.get(status_url(MAC_2), json=status)


def climates(hass) -> int:
    return sum(1 for entity in er.async_get(hass).entities.values() if entity.domain == "climate")


def cloud_down(aioclient_mock) -> None:
    aioclient_mock.clear_requests()
    aioclient_mock.post(LOGIN_URL, json=load_fixture("login.json"))
    aioclient_mock.post(REFRESH_URL, json=load_fixture("login.json"))
    aioclient_mock.get(DEVICES_URL, status=502, text="Bad gateway")
    aioclient_mock.get(status_url(MAC_1), status=502, text="Bad gateway")
    aioclient_mock.get(status_url(MAC_2), status=502, text="Bad gateway")


def logged(caplog, level: int, text: str) -> int:
    return sum(
        1 for record in caplog.records
        if record.levelno == level and text in record.getMessage()
    )


def saved(hass_storage, entry) -> dict:
    return hass_storage[cache_key(entry)]["data"]


async def _start(hass, entry) -> list[str]:
    """A start of Home Assistant that reads the cloud: the retry counter lives in memory.

    With saved descriptions a start builds the devices from them (1.4+) and
    does not look at the device list; these tests are about the starts that
    read it (the first one, a reload because the descriptions are out of date).
    """
    _INCOMPLETE_SETUPS.clear()
    api.RELOAD_FROM_CLOUD.add(entry.entry_id)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    states = [entry.state.value]
    while entry.state is not ConfigEntryState.LOADED and len(states) < 10:
        api.RELOAD_FROM_CLOUD.add(entry.entry_id)
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        states.append(entry.state.value)
    return states


# --- one unreadable description does not take the entry down ---


async def test_unreadable_device_is_skipped(hass, config_entry, mock_cloud, ws_factory, caplog):
    mock_cloud()
    real = HaierAC._load_config_from_attributes

    def load(self, data):
        if self.device_id == MAC_2:
            raise RuntimeError("not the format this code knows")
        real(self, data)

    with patch.object(HaierAC, "_load_config_from_attributes", load):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.LOADED
    assert [d.device_id for d in config_entry.runtime_data.devices] == [MAC_1]
    assert climates(hass) == 1
    assert logged(caplog, logging.WARNING, "its description is not readable") == 1
    await unload(hass, config_entry)


async def test_known_unit_turned_unreadable_is_not_missing(
    hass, config_entry, mock_cloud, ws_factory, aioclient_mock, caplog
):
    """The cloud does return it: no retries, no "gone from the account", no deleting."""
    mock_cloud()
    assert await _start(hass, config_entry) == ["loaded"]
    await unload(hass, config_entry)
    before = {e.unique_id for e in er.async_get(hass).entities.values()}

    # "info": null: without the model the unit would come up again as "<mac>_AC"
    cloud(aioclient_mock, mac_2=lambda d: d.update(info=None))
    assert await _start(hass, config_entry) == ["loaded"]  # no setup_retry
    haier = config_entry.runtime_data
    assert [d.device_id for d in haier.devices] == [MAC_1]
    assert haier.unreadable == {MAC_2}
    assert haier.missing == set()
    assert {e.unique_id for e in er.async_get(hass).entities.values()} == before
    assert logged(caplog, logging.WARNING, "its description is not readable") == 1
    assert "gone from the Evo app" not in caplog.text
    registry = dr.async_get(hass)
    unreadable = registry.async_get_device_by_identifier((C.DOMAIN, MAC_2), config_entry.entry_id)
    assert not await async_remove_config_entry_device(hass, config_entry, unreadable)
    await unload(hass, config_entry)


async def test_nothing_readable_fails_the_setup(hass, config_entry, mock_cloud, ws_factory):
    """No device at all is not an outage: no retry loop, no saved descriptions."""
    mock_cloud()
    with patch.object(CFG.Attribute, "__init__", side_effect=RuntimeError("unknown format")):
        await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_ERROR


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(constraint=None),
    lambda d: d.update(sensors=None),
    lambda d: d.update(settings=None, status=None),
    lambda d: d["attributes"][0].update(list=None, range=None),
    lambda d: d["attributes"].append(None),
], ids=["constraint", "sensors", "settings-status", "list-range", "junk-attribute"])
async def test_null_fields_are_read_as_absent(hass, config_entry, ws_factory, aioclient_mock, mutate):
    cloud(aioclient_mock, mac_2=mutate)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.LOADED
    assert len(config_entry.runtime_data.devices) == 2
    assert all(device.available for device in config_entry.runtime_data.devices)
    assert climates(hass) == 2
    await unload(hass, config_entry)


def test_odd_descriptions_do_not_raise():
    command = [{"commandName": "1", "value": "1"}]
    constraint = CFG.Constraint([
        {"pendingCondition": None, "additionalCommands": None},
        {"pendingCondition": {"commands": None}},
        {"pendingCondition": {"commands": [{"commandName": "1", "values": None}]}},
        {
            "pendingCondition": {"commands": [{"commandName": "1", "values": ["true"]}]},
            "additionalCommands": {"mergeType": "APPEND", "commands": None},
        },
    ])
    assert constraint.apply(command) == command
    assert CFG.Range({"data": None}).min_value is None
    assert CFG.Attribute({"name": "1", "range": "junk"}).range is None
    assert CFG.Attribute({"name": "1", "type": None}).type == ""
    for odd in (None, ["junk"], "junk", {"data": None}, {"data": "junk"}):
        assert CFG.Attribute({"name": "1", "list": odd}).list == []
    items = CFG.Attribute({"name": "1", "list": {"data": [None, "junk", {"data": "1", "name": "Активен"}]}}).list
    assert [item.name for item in items] == ["on"]


# --- a unit gone from the account is waited for once ---


async def test_removed_unit_is_waited_for_once(
    hass, config_entry, mock_cloud, ws_factory, aioclient_mock, hass_storage
):
    mock_cloud()
    assert await _start(hass, config_entry) == ["loaded"]
    await unload(hass, config_entry)

    cloud(aioclient_mock, list_mac_2=False)
    assert await _start(hass, config_entry) == [*["setup_retry"] * C.SETUP_INCOMPLETE_RETRIES, "loaded"]
    assert config_entry.runtime_data.missing == {MAC_2}
    # in the file already: a stop of Home Assistant unloads nothing
    assert saved(hass_storage, config_entry)["missing"] == [MAC_2]
    await unload(hass, config_entry)

    # the next start of Home Assistant does not wait for it again
    assert await _start(hass, config_entry) == ["loaded"]
    assert config_entry.runtime_data.missing == {MAC_2}
    await unload(hass, config_entry)

    # ...and it is waited for again once it has come back and gone
    cloud(aioclient_mock)
    assert await _start(hass, config_entry) == ["loaded"]
    assert config_entry.runtime_data.missing == set()
    await unload(hass, config_entry)
    cloud(aioclient_mock, list_mac_2=False)
    assert (await _start(hass, config_entry))[0] == "setup_retry"
    await unload(hass, config_entry)


async def test_given_up_unit_survives_a_start_from_the_saved_descriptions(
    hass, config_entry, mock_cloud, ws_factory, aioclient_mock
):
    mock_cloud()
    assert await _start(hass, config_entry) == ["loaded"]
    await unload(hass, config_entry)
    cloud(aioclient_mock, list_mac_2=False)
    assert (await _start(hass, config_entry))[-1] == "loaded"
    await unload(hass, config_entry)

    cloud_down(aioclient_mock)
    assert await _start(hass, config_entry) == ["loaded"]
    assert config_entry.runtime_data.from_cache
    assert config_entry.runtime_data.missing == {MAC_2}
    await unload(hass, config_entry)  # writes the file again

    cloud(aioclient_mock, list_mac_2=False)
    assert await _start(hass, config_entry) == ["loaded"]  # still not waited for
    await unload(hass, config_entry)


async def test_push_from_a_given_up_unit_forgets_it(
    hass, config_entry, mock_cloud, ws_factory, aioclient_mock, hass_storage, caplog
):
    """It is alive after all: the next start waits for it again."""
    mock_cloud()
    assert await _start(hass, config_entry) == ["loaded"]
    await unload(hass, config_entry)
    cloud(aioclient_mock, list_mac_2=False)
    assert (await _start(hass, config_entry))[-1] == "loaded"
    haier = config_entry.runtime_data
    assert haier.missing == {MAC_2}
    caplog.set_level(logging.INFO, logger="custom_components.haier_evo")

    ws_factory.current.feed({
        "event": "status", "macAddress": MAC_2.upper(), "payload": {"statuses": [{"properties": {}}]},
    })
    await asyncio.sleep(0.05)
    await hass.async_block_till_done()
    assert haier.missing == set()
    assert logged(caplog, logging.INFO, "is back in the cloud") == 1
    hass.bus.async_fire(EVENT_HOMEASSISTANT_FINAL_WRITE)
    await hass.async_block_till_done()
    assert saved(hass_storage, config_entry)["missing"] == []
    await unload(hass, config_entry)

    assert (await _start(hass, config_entry))[0] == "setup_retry"
    await unload(hass, config_entry)


async def test_gone_device_can_be_deleted(hass, config_entry, mock_cloud, ws_factory, aioclient_mock):
    mock_cloud()
    assert await _start(hass, config_entry) == ["loaded"]
    await unload(hass, config_entry)
    cloud(aioclient_mock, list_mac_2=False)
    await _start(hass, config_entry)

    assert config_entry.supports_remove_device
    registry = dr.async_get(hass)
    live = registry.async_get_device_by_identifier((C.DOMAIN, MAC_1), config_entry.entry_id)
    gone = registry.async_get_device_by_identifier((C.DOMAIN, MAC_2), config_entry.entry_id)
    assert not await async_remove_config_entry_device(hass, config_entry, live)
    assert await async_remove_config_entry_device(hass, config_entry, gone)

    registry.async_remove_device(gone.id)
    await unload(hass, config_entry)
    assert await _start(hass, config_entry) == ["loaded"]
    assert config_entry.runtime_data.missing == set()
    await unload(hass, config_entry)
    # not loaded: no telling which units are alive, nothing may be deleted
    assert not await async_remove_config_entry_device(hass, config_entry, live)


# --- a command at the moment the socket closes waits for the reconnect ---


async def test_command_right_as_the_socket_closes(hass, setup_entry, ws_factory):
    device = setup_entry.runtime_data.get_device_by_id(MAC_1)
    first = ws_factory.current
    first.server_close()
    # no await in between: the session has not seen the close yet
    assert setup_entry.runtime_data._connected.is_set()
    await device.set_temperature(25)
    second = await ws_factory.wait_for(2)
    assert first.sent == []
    assert second.sent[-1]["commands"] == [{"commandName": "0", "value": "25"}]


# --- what came over the socket reaches the file ---


def saved_setpoint(hass_storage, entry) -> str:
    devices = hass_storage[cache_key(entry)]["data"]["devices"]
    status = next(d for d in devices if d["mac"] == MAC_1)["status"]
    return next(a["currentValue"] for a in status["attributes"] if a["name"] == "0")


async def test_pushed_values_reach_the_file_on_stop(hass, setup_entry, ws_factory, hass_storage):
    assert saved_setpoint(hass_storage, setup_entry) == "24"
    ws_factory.current.feed(status_message({"0": "19"}))
    await asyncio.sleep(0.05)
    await hass.async_block_till_done()
    assert saved_setpoint(hass_storage, setup_entry) == "24"  # delayed, not at once
    # a stop of Home Assistant unloads nothing: the Store flushes what is scheduled
    hass.bus.async_fire(EVENT_HOMEASSISTANT_FINAL_WRITE)
    await hass.async_block_till_done()
    assert saved_setpoint(hass_storage, setup_entry) == "19"

    def saved_status() -> str:
        devices = saved(hass_storage, setup_entry)["devices"]
        return next(d for d in devices if d["mac"] == MAC_1)["status"]["status"]

    assert saved_status() == "ONLINE"
    ws_factory.current.feed({"event": "deviceStatusEvent", "macAddress": MAC_1, "payload": {"status": "OFFLINE"}})
    await asyncio.sleep(0.05)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_FINAL_WRITE)
    await hass.async_block_till_done()
    assert saved_status() == "OFFLINE"


async def test_pushes_do_not_put_the_write_off(hass, setup_entry, ws_factory):
    """Store.async_delay_save restarts its timer on every call."""
    store = setup_entry.runtime_data.cache_store
    with patch.object(store, "async_delay_save", wraps=store.async_delay_save) as delay_save:
        for value in ("19", "20", "21"):
            ws_factory.current.feed(status_message({"0": value}))
        await asyncio.sleep(0.05)
        await hass.async_block_till_done()
        assert delay_save.call_count == 1
        # once written, the next push schedules a write again
        hass.bus.async_fire(EVENT_HOMEASSISTANT_FINAL_WRITE)
        await hass.async_block_till_done()
        ws_factory.current.feed(status_message({"0": "22"}))
        await asyncio.sleep(0.05)
        await hass.async_block_till_done()
        assert delay_save.call_count == 2


# --- a plain turn-on leaves an unknown mode alone ---


async def test_turn_on_with_an_unknown_mode_sends_power_only(hass, setup_entry, ws_factory):
    device = setup_entry.runtime_data.get_device_by_id(MAC_1)
    ws = ws_factory.current
    ws.feed(status_message({"21": "0", "2": "7"}))  # a mode the profile does not list
    await asyncio.sleep(0.05)
    await hass.async_block_till_done()
    assert device.mode is None
    await device.switch_on()
    assert ws.sent[-1]["commands"] == [{"commandName": "21", "value": "1"}]
    assert device.mode is None
    assert device.status == 1
    # an explicit mode still goes out with the power
    await device.switch_on("heat")
    assert ws.sent[-1]["commands"] == [
        {"commandName": "21", "value": "1"}, {"commandName": "2", "value": "4"},
    ]
    assert device.mode == "heat"


# --- Retry-After is capped ---


@pytest.mark.parametrize(("header", "expected"), [
    ("864000", C.LOGIN_LIMIT_MAX),
    ("inf", C.LOGIN_LIMIT_429),
    ("nan", C.LOGIN_LIMIT_429),
    ("-5", 1),
    ("soon", C.LOGIN_LIMIT_429),
    ("120", 120),
])
async def test_retry_after_is_capped(hass, aioclient_mock, header, expected):
    aioclient_mock.post(LOGIN_URL, status=429, headers={"Retry-After": header}, text="")
    client = make_client(hass, store=False)
    with pytest.raises(HaierRateLimitError) as err:
        await client.async_login()
    assert err.value.retry_after == expected
    loop_time = asyncio.get_running_loop().time()
    assert client._login_blocked_until <= loop_time + C.LOGIN_LIMIT_MAX


# --- one warning per outage ---


async def test_one_warning_per_outage(hass, setup_entry, ws_factory, caplog):
    def count(level: int, text: str) -> int:
        return logged(caplog, level, text)

    caplog.set_level(logging.DEBUG, logger="custom_components.haier_evo")
    ws_factory.fail = ClientError("cloud down")
    ws_factory.current.server_close()
    async with asyncio.timeout(2):
        while count(logging.DEBUG, "WebSocket connect failed") < 3:
            await asyncio.sleep(0.01)
    assert count(logging.WARNING, "WebSocket connect failed") == 1
    assert count(logging.INFO, "connection is back") == 0

    # the outage goes on for another reason: that one is reported once, too
    ws_factory.fail = TimeoutError()
    async with asyncio.timeout(2):
        while count(logging.DEBUG, "WebSocket connect failed: TimeoutError") < 3:
            await asyncio.sleep(0.01)
    assert count(logging.WARNING, "WebSocket connect failed: TimeoutError") == 1
    assert count(logging.WARNING, "WebSocket connect failed") == 2

    ws_factory.fail = None
    await ws_factory.wait_for(2)
    await hass.async_block_till_done()
    assert count(logging.INFO, "connection is back") == 1

    # the next outage is reported again
    ws_factory.fail = ClientError("cloud down")
    ws_factory.current.server_close()
    async with asyncio.timeout(2):
        while count(logging.WARNING, "WebSocket connect failed") < 3:
            await asyncio.sleep(0.01)
    ws_factory.fail = None


# --- values compared, item lists, the preset "none" ---


def test_true_on_and_one_are_one_value():
    """The cloud answers errNo for a value already set: "on" there and "1" here are the same."""
    from custom_components.haier_evo.device import _same_value

    assert _same_value("on", "1") and _same_value("TRUE", 1) and _same_value("off", "false")
    assert _same_value("24.00", "24") and not _same_value("on", "0")
    attr = CFG.Attribute({"name": "1", "list": {"data": [{"data": "1", "name": "Активен"}]}})
    assert attr.get_item_name("on") == attr.get_item_name("true") == attr.get_item_name("1") == "on"


def test_item_lists_are_made_once_per_list():
    attr = CFG.Attribute({"name": "1", "list": {"data": [{"data": "1", "name": "Активен"}]}})
    first = attr.list
    assert attr.list[0] is first[0]
    first.clear()  # a caller's copy
    assert len(attr.list) == 1
    attr.list = [{"data": "0", "name": "Неактивен"}, {"data": "1", "name": "Активен"}]
    assert [i.value for i in attr.list] == ["0", "1"]
    attr["list"]["data"].append({"data": "2", "name": "Авто"})
    assert len(attr.list) == 3


def test_preset_none_carries_the_additional_commands():
    from types import SimpleNamespace

    attr = CFG.Attribute({"name": "7", "description": "Тихий"})
    attr.name = "preset_mode_sleep"
    constraint = CFG.Constraint([{
        "pendingCondition": {"commands": [{"commandName": "7", "values": ["0"]}]},
        "additionalCommands": {"mergeType": "APPEND", "commands": [{"name": "5", "value": "1"}]},
    }])
    device = SimpleNamespace(
        config=SimpleNamespace(get_command_by_name=lambda name: None, attrs=[attr]), constraint=constraint,
    )
    assert HaierAC.get_preset_mode_none(device) == [
        {"commandName": "7", "value": "0"}, {"commandName": "5", "value": "1"},
    ]
