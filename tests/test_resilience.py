"""Ideas of 1.x: start without the cloud, incomplete device
lists, silent sessions, code 1007, errNo for applied values, MACs in the log,
no redirects."""
from __future__ import annotations

import asyncio
import copy
import json
import logging
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er

from custom_components.haier_evo import api
from custom_components.haier_evo import const as C
from custom_components.haier_evo.api import command_applied
from custom_components.haier_evo.client import HaierClient, HaierConnectionError
from custom_components.haier_evo.logger import mask_macs

from .conftest import (
    DEVICES_URL,
    LOGIN_URL,
    MAC_1,
    MAC_2,
    REFRESH_URL,
    load_fixture,
    status_url,
)
from .test_client import make_client

CLIMATE_UID = f"{MAC_1}_AS25PHP3HRA"


def climate_state(hass):
    entity_id = er.async_get(hass).async_get_entity_id("climate", C.DOMAIN, CLIMATE_UID)
    return hass.states.get(entity_id)


def cache_key(entry) -> str:
    return f"{C.DOMAIN}.{entry.entry_id}.devices"


def refresh_calls(aioclient_mock) -> int:
    return sum(1 for c in aioclient_mock.mock_calls if str(c[1]).endswith("/auth/refresh"))


def cloud(aioclient_mock, *, devices=200, mac_2_status=200) -> None:
    """The cloud from scratch: earlier answers (and recorded calls) are dropped."""
    aioclient_mock.clear_requests()
    aioclient_mock.post(LOGIN_URL, json=load_fixture("login.json"))
    aioclient_mock.post(REFRESH_URL, json=load_fixture("login.json"))
    if devices == 200:
        aioclient_mock.get(DEVICES_URL, json=load_fixture("devices_page.json"))
    else:
        # the whole cloud is down: the statuses do not answer either
        aioclient_mock.get(DEVICES_URL, status=devices, text="Bad gateway")
        aioclient_mock.get(status_url(MAC_1), status=devices, text="Bad gateway")
        aioclient_mock.get(status_url(MAC_2), status=devices, text="Bad gateway")
        return
    aioclient_mock.get(status_url(MAC_1), json=load_fixture("status_as25php3hra.json"))
    if mac_2_status == 200:
        aioclient_mock.get(status_url(MAC_2), json=load_fixture("status_as70s2sf2fa.json"))
    else:
        aioclient_mock.get(status_url(MAC_2), status=mac_2_status, text="")


async def unload(hass, entry) -> None:
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


# --- п.1: start from the saved descriptions ---


async def test_cloud_down_at_start_uses_saved_devices(
    hass, config_entry, mock_cloud, ws_factory, aioclient_mock, hass_storage
):
    """2026-11-04: after a power cut HA came up before the cloud did."""
    with patch.object(C, "CACHE_RETRY_FIRST", 0.05):
        mock_cloud()
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
        saved = hass_storage[cache_key(config_entry)]["data"]["devices"]
        assert {d["mac"] for d in saved} == {MAC_1, MAC_2}
        await unload(hass, config_entry)

        cloud(aioclient_mock, devices=502)
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
        assert config_entry.state is ConfigEntryState.LOADED
        assert config_entry.runtime_data.from_cache
        # the saved values are hours old: not shown as live (1.6)
        assert climate_state(hass).state == "unavailable"

        # the cloud answers again: the background read brings the state, no reload
        haier = config_entry.runtime_data
        cloud(aioclient_mock)
        async with asyncio.timeout(3):
            while haier.from_cache:
                await asyncio.sleep(0.02)
        await hass.async_block_till_done()
        assert config_entry.runtime_data is haier
        assert climate_state(hass).state == "cool"


async def test_start_does_not_wait_for_the_cloud(
    hass, config_entry, mock_cloud, ws_factory, aioclient_mock, hass_storage
):
    """1.4+ (#6): with saved descriptions the entities come up at once, the cloud is read in the background."""
    mock_cloud()
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    await unload(hass, config_entry)

    cloud(aioclient_mock)
    gate = asyncio.Event()
    real_method = HaierClient.async_get_device_status

    async def patched(self, mac):
        await gate.wait()
        return await real_method(self, mac)

    with patch.object(HaierClient, "async_get_device_status", patched):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        assert config_entry.state is ConfigEntryState.LOADED  # before the cloud answered
        assert not any("smartHome" in str(c[1]) for c in aioclient_mock.mock_calls)
        assert climate_state(hass).state == "unavailable"
        gate.set()
        haier = config_entry.runtime_data
        async with asyncio.timeout(3):
            while haier.from_cache:
                await asyncio.sleep(0.02)
        await hass.async_block_till_done()
    assert climate_state(hass).state == "cool"
    await unload(hass, config_entry)
    await unload(hass, config_entry)


async def test_no_reload_while_the_cloud_lists_nothing(
    hass, config_entry, mock_cloud, ws_factory, aioclient_mock
):
    """An empty list (#105) must not reload the entry every few seconds."""
    with patch.object(C, "CACHE_RETRY_FIRST", 0.02), patch.object(C, "CACHE_RETRY_MAX", 0.02):
        mock_cloud()
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
        await unload(hass, config_entry)

        aioclient_mock.clear_requests()
        aioclient_mock.post(REFRESH_URL, json=load_fixture("login.json"))
        aioclient_mock.get(DEVICES_URL, json=load_fixture("devices_page_empty.json"))
        aioclient_mock.get(status_url(MAC_1), json=load_fixture("status_as25php3hra.json"))
        aioclient_mock.get(status_url(MAC_2), json=load_fixture("status_as70s2sf2fa.json"))
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
        haier = config_entry.runtime_data
        await asyncio.sleep(0.2)
        await hass.async_block_till_done()
        assert config_entry.runtime_data is haier  # not reloaded
    await unload(hass, config_entry)


async def test_unusable_saved_devices_are_dropped(hass, config_entry, aioclient_mock, hass_storage):
    hass_storage[cache_key(config_entry)] = {
        "version": C.CACHE_VERSION, "key": cache_key(config_entry), "data": {"devices": ["broken"]},
    }
    aioclient_mock.post(LOGIN_URL, status=502, text="Bad gateway")
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_RETRY
    assert cache_key(config_entry) not in hass_storage


async def test_wrong_password_after_a_start_from_saved_devices_asks_the_user(
    hass, config_entry, mock_cloud, ws_factory, aioclient_mock
):
    mock_cloud()
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    await unload(hass, config_entry)
    hass.data.pop(C.DOMAIN, None)
    aioclient_mock.clear_requests()
    aioclient_mock.post(REFRESH_URL, status=400, json={"error": {"validation": {"refreshToken": "bad"}}})
    aioclient_mock.post(LOGIN_URL, status=400, json={"error": {"validation": {"password": "bad"}}})
    aioclient_mock.get(DEVICES_URL, status=401, text="")
    aioclient_mock.get(status_url(MAC_1), status=401, text="")
    aioclient_mock.get(status_url(MAC_2), status=401, text="")
    with patch("custom_components.haier_evo.client.HaierClient.async_load_tokens"):
        await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
    # started from the saved descriptions (1.4+); the background read asks the user
    flows = hass.config_entries.flow.async_progress()
    assert [f["context"]["source"] for f in flows] == ["reauth"]
    assert climate_state(hass).state == "unavailable"
    await unload(hass, config_entry)


async def test_background_read_stops_after_the_reauth(
    hass, config_entry, mock_cloud, ws_factory, aioclient_mock, hass_storage, caplog
):
    """A dead refresh token found by the background read: one reauth, then it stops (1.7)."""
    mock_cloud()
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    await unload(hass, config_entry)
    assert "password" not in config_entry.data

    # the access token has expired, the refresh token is dead
    hass_storage[f"{C.DOMAIN}.{config_entry.entry_id}"]["data"]["tokenexpire"] = "2000-01-01T00:00:00+00:00"
    aioclient_mock.clear_requests()
    aioclient_mock.post(REFRESH_URL, status=400, json={"error": {"validation": {"refreshToken": "expired"}}})

    caplog.set_level(logging.ERROR)
    with patch.object(C, "CACHE_RETRY_FIRST", 0.02), patch.object(C, "CACHE_RETRY_MAX", 0.02):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await asyncio.sleep(0.5)
        await hass.async_block_till_done()
    await unload(hass, config_entry)
    flows = hass.config_entries.flow.async_progress()
    assert [f["context"]["source"] for f in flows] == ["reauth"]
    assert sum("re-authentication is required" in r.getMessage() for r in caplog.records) == 1
    assert refresh_calls(aioclient_mock) == 1


def _devices_page_of_mac_1() -> dict:
    """The device list before MAC_2 was added in the app."""
    page = copy.deepcopy(load_fixture("devices_page.json"))
    for item in page["data"]["presentation"]["layout"]["scrollContainer"]:
        if item["trackingData"]["component"]["componentName"] == "deviceList":
            state = json.loads(item["state"])
            state["items"] = state["items"][:1]
            item["state"] = json.dumps(state)
    return page["data"]


async def test_failed_reload_from_the_cloud_waits_before_reading_it_again(
    hass, config_entry, mock_cloud, ws_factory, aioclient_mock
):
    """A unit added in the app reloads the entry from the cloud. A cloud that
    fails that start and answers the check right after it must not reload the
    entry again and again: the fallback to the saved descriptions waits
    CACHE_RETRY_FIRST s before reading the cloud."""
    mock_cloud()
    one_unit = _devices_page_of_mac_1()
    real_page = HaierClient.async_get_devices_page
    real_status = HaierClient.async_get_device_status
    real_setup = api.Haier.async_setup
    state = {"added": False, "in_setup": False, "setups": 0}

    async def page(self):
        return await real_page(self) if state["added"] else copy.deepcopy(one_unit)

    async def status(self, mac):
        if state["in_setup"]:
            raise HaierConnectionError("the start from the cloud fails")
        return await real_status(self, mac)

    async def setup(self):
        state["setups"] += 1
        state["in_setup"] = state["added"]
        try:
            return await real_setup(self)
        finally:
            state["in_setup"] = False

    with (
        patch.object(HaierClient, "async_get_devices_page", page),
        patch.object(HaierClient, "async_get_device_status", status),
        patch.object(api.Haier, "async_setup", setup),
        patch.object(C, "CACHE_RETRY_FIRST", 5),
    ):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
        assert len(config_entry.runtime_data.devices) == 1
        state["added"], state["setups"] = True, 0
        await config_entry.runtime_data._async_check_device_list()
        await asyncio.sleep(1)
        reloads = state["setups"]
        state["added"] = False  # ends a loop, if there is one, before the unload
        await asyncio.sleep(0.5)
        await hass.async_block_till_done()
        assert config_entry.state is ConfigEntryState.LOADED
    await unload(hass, config_entry)
    # one reload from the cloud, then the saved descriptions and a pause
    assert reloads == 1


# --- п.6: fewer devices than the registry knows ---


async def test_incomplete_device_list_retries_then_starts(
    hass, config_entry, mock_cloud, ws_factory, aioclient_mock, caplog
):
    mock_cloud()
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    await unload(hass, config_entry)

    cloud(aioclient_mock, mac_2_status=403)
    # starts that read the cloud (with saved descriptions a start uses them, 1.4+)
    api.RELOAD_FROM_CLOUD.add(config_entry.entry_id)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_RETRY
    for _ in range(C.SETUP_INCOMPLETE_RETRIES - 1):
        api.RELOAD_FROM_CLOUD.add(config_entry.entry_id)
        await hass.config_entries.async_reload(config_entry.entry_id)
        await hass.async_block_till_done()
        assert config_entry.state is ConfigEntryState.SETUP_RETRY
    api.RELOAD_FROM_CLOUD.add(config_entry.entry_id)
    await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.LOADED
    assert len(config_entry.runtime_data.devices) == 1
    assert "still does not return" in caplog.text
    ours = [r.getMessage() for r in caplog.records if r.name.startswith("custom_components.haier_evo")]
    assert not [m for m in ours if MAC_2 in m]  # masked
    await unload(hass, config_entry)


# --- п.4: silent sessions and code 1007 ---


async def test_silent_session_is_replaced(hass, setup_entry, ws_factory, aioclient_mock):
    with patch.object(C, "WS_SILENT_SESSION_TIMEOUT", 0.05):
        ws_factory.current.server_close()
        second = await ws_factory.wait_for(2)
        refreshes = refresh_calls(aioclient_mock)
        await ws_factory.wait_for(3)  # nothing came over the second one
        assert second.closed
        await ws_factory.wait_for(4, timeout=3)
        # the second silent session in a row goes with a renewed token
        assert refresh_calls(aioclient_mock) > refreshes
        ws_factory.current.feed({"event": "info", "macAddress": MAC_1, "payload": {}})
        await asyncio.sleep(0.3)
        assert len(ws_factory.sockets) == 4
        assert setup_entry.runtime_data._silent_sessions == 0


async def test_session_with_messages_is_kept(hass, setup_entry, ws_factory):
    with patch.object(C, "WS_SILENT_SESSION_TIMEOUT", 0.05):
        ws_factory.current.server_close()
        second = await ws_factory.wait_for(2)
        second.feed({"event": "info", "macAddress": MAC_1, "payload": {}})
        await asyncio.sleep(0.2)
        assert not second.closed
        assert len(ws_factory.sockets) == 2


async def test_close_1007_renews_the_token(hass, setup_entry, ws_factory, aioclient_mock):
    """The gateway takes the upgrade, then closes with 1007 "Auth token not valid!"."""
    ws = ws_factory.current
    before = refresh_calls(aioclient_mock)
    setup_entry.runtime_data.client._token_issued -= C.TOKEN_FRESH  # not one issued seconds ago
    ws.closed = True
    ws.close_code = C.WS_CLOSE_TOKEN_REFUSED
    ws._queue.put_nowait(None)
    await ws_factory.wait_for(2)
    assert refresh_calls(aioclient_mock) == before + 1


# --- п.5: errNo for a value the unit already has ---


async def test_nonzero_errno_for_an_applied_value_is_no_rejection(
    hass, setup_entry, ws_factory, caplog
):
    ws = ws_factory.current
    state = climate_state(hass)
    await hass.services.async_call(
        "climate", "set_temperature", {"entity_id": state.entity_id, "temperature": 24}, blocking=True
    )
    ws.feed({"event": "command_response", "macAddress": MAC_1, "trace": ws.sent[-1]["trace"], "errNo": -1})
    await asyncio.sleep(0.05)
    await hass.async_block_till_done()
    assert "rejected a command" not in caplog.text


@pytest.mark.parametrize(
    ("payload", "applied"),
    [
        ({"commands": [{"commandName": "0", "value": "24"}]}, True),
        ({"commands": [{"commandName": "0", "value": "24.00"}]}, True),
        ({"commands": [{"commandName": "0", "value": "24"}, {"commandName": "2", "value": "4"}]}, False),
        ({"command": {"commandName": "0", "value": "25"}}, False),
        ({"commands": [{"commandName": "999", "value": "1"}]}, False),
        ({"commands": []}, False),
    ],
)
def test_command_applied(payload, applied):
    status = load_fixture("status_as25php3hra.json")  # "0" = 24, "2" = 1
    assert command_applied(payload, status) is applied


# --- п.2, п.3: MACs in the log, redirects ---


def test_mask_macs():
    assert mask_macs(f"unit {MAC_1} and {MAC_2.upper()}") == "unit 00:00:5e:**:**:01 and 00:00:5E:**:**:02"
    assert mask_macs("00-00-5e-00-53-01") == "00-00-5e-**-**-01"
    assert mask_macs("macAddress=00%3A00%3A5E%3A00%3A53%3A01") == "macAddress=00%3A00%3A5E%3A**%3A**%3A01"
    assert mask_macs("12:30:45 and 3f2504e0-4f89-11d3-9a0c-0305e82c3301") == (
        "12:30:45 and 3f2504e0-4f89-11d3-9a0c-0305e82c3301"
    )


async def test_debug_log_has_no_macs(hass, setup_entry, ws_factory, caplog):
    caplog.set_level(logging.DEBUG, logger="custom_components.haier_evo")
    ws_factory.current.feed({"event": "status", "macAddress": MAC_1.upper(), "payload": {"statuses": []}})
    await hass.async_block_till_done()
    ours = [r.getMessage() for r in caplog.records if r.name.startswith("custom_components.haier_evo")]
    assert any("Received WSS message" in m for m in ours)
    assert not [m for m in ours if MAC_1 in m.lower()]


async def test_redirects_are_not_followed(hass, aioclient_mock):
    aioclient_mock.post(LOGIN_URL, json=load_fixture("login.json"))
    aioclient_mock.get(DEVICES_URL, status=302, headers={"Location": "https://example.com/"})
    client = make_client(hass, store=False)
    with pytest.raises(HaierConnectionError, match="redirect"):
        await client.async_get_devices_page()
    assert not [c for c in aioclient_mock.mock_calls if "example.com" in str(c[1])]


async def test_sign_in_redirect_is_no_wrong_password(hass, aioclient_mock):
    aioclient_mock.post(LOGIN_URL, status=307, headers={"Location": "https://example.com/"})
    client = make_client(hass, store=False)
    with pytest.raises(HaierConnectionError):
        await client.async_login()
    assert not client._auth_failed


async def test_saved_devices_go_with_the_entry(hass, config_entry, mock_cloud, ws_factory, hass_storage):
    """A delayed write pending at removal must not bring the file back."""
    mock_cloud()
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    await config_entry.runtime_data._async_resync()  # schedules a delayed write
    assert await hass.config_entries.async_remove(config_entry.entry_id)
    await hass.async_block_till_done()
    assert cache_key(config_entry) not in hass_storage
