"""Reliability and safety of 1.x, on the asyncio code.

A bounded WebSocket connect, a retried TCP connect, honest unavailability
after a start from the saved descriptions, refusals of the cloud in the UI, maintenance alarms that lock nothing, a periodic look at the
device list, value-pattern scrubbing of diagnostics, safe profile names.
"""
from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import os
import shutil
from unittest.mock import patch

import pytest
from aiohttp import ClientConnectorError, ClientSession
from aiohttp.client_reqrep import ConnectionKey
from homeassistant.exceptions import HomeAssistantError

from custom_components.haier_evo import api
from custom_components.haier_evo import config as CFG  # noqa: N812
from custom_components.haier_evo import const as C  # noqa: N812
from custom_components.haier_evo.diagnostics import async_get_config_entry_diagnostics

from .conftest import DEVICES_URL, LOGIN_URL, MAC_1, MAC_2, load_fixture, status_url
from .test_client import _answers, make_client
from .test_resilience import cache_key, climate_state, cloud
from .test_websocket import climate_id, status_message


async def _next_sent(ws, count: int, timeout: float = 2) -> dict:
    async with asyncio.timeout(timeout):
        while len(ws.sent) < count:
            await asyncio.sleep(0.01)
    return ws.sent[count - 1]


# --- WebSocket connect is bounded ---


async def test_stuck_websocket_connect_is_given_up(hass, setup_entry, ws_factory):
    """A handshake that never finishes used to hold the connection for up to 300 s."""
    calls = []

    async def stuck(session, url, **kwargs):
        calls.append(url)
        if len(calls) == 1:
            await asyncio.sleep(3600)
        return await ws_factory(session, url, **kwargs)

    with patch.object(C, "WS_CONNECT_TIMEOUT", 0.05), patch.object(ClientSession, "ws_connect", stuck):
        ws_factory.current.server_close()
        await ws_factory.wait_for(2, timeout=3)
    assert len(calls) >= 2


# --- a TCP connect that failed is tried once more ---


def _connector_error() -> ClientConnectorError:
    key = ConnectionKey("iot-platform.evo.haieronline.ru", 443, True, None, None, None, None)
    return ClientConnectorError(key, OSError(110, "Connection timed out"))


async def test_get_retries_a_failed_tcp_connect(hass, aioclient_mock):
    aioclient_mock.post(LOGIN_URL, json=load_fixture("login.json"))
    _answers(DEVICES_URL, aioclient_mock, {"exc": _connector_error()}, {"json": load_fixture("devices_page.json")})
    client = make_client(hass, store=False)
    await client.async_login()
    assert "presentation" in await client.async_get_devices_page()


# --- after a start from the saved descriptions: unavailable until live ---


async def test_saved_values_are_not_shown_as_live(hass, config_entry, mock_cloud, ws_factory, aioclient_mock,
                                                  hass_storage):
    mock_cloud()
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert cache_key(config_entry) in hass_storage

    with patch.object(C, "CACHE_RETRY_FIRST", 3600):
        cloud(aioclient_mock, devices=502)
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
        assert config_entry.runtime_data.from_cache
        assert climate_state(hass).state == "unavailable"
        # the cloud pushes the state of the unit over the socket: live again
        ws = await ws_factory.wait_for(2)
        ws.feed(status_message({"2": "1", "21": "1"}))
        await hass.async_block_till_done()
        assert climate_state(hass).state == "cool"
        assert config_entry.runtime_data.devices[0].to_dict()["synced"] is True
    await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()


# --- refusals of the cloud reach the UI ---


async def _call_and_answer(hass, ws, service: str, data: dict, *answers: int | None):
    """Start a service call and answer its commands, one errNo per command sent."""
    base = len(ws.sent)
    task = hass.async_create_task(
        hass.services.async_call("climate", service, {"entity_id": climate_id(hass), **data}, blocking=True)
    )
    for number, err in enumerate(answers, start=1):
        message = await _next_sent(ws, base + number)
        if err is not None:
            ws.feed({"event": "command_response", "macAddress": MAC_1, "trace": message["trace"], "errNo": err})
    return task


async def test_refused_command_is_an_error_in_the_ui(hass, setup_entry, ws_factory, caplog):
    ws = ws_factory.current
    with patch.object(C, "WS_COMMAND_WAIT", 2):
        task = await _call_and_answer(hass, ws, "set_temperature", {"temperature": 29}, 5)
        with pytest.raises(HomeAssistantError) as err:
            await task
    assert err.value.translation_key == "command_rejected"
    assert err.value.translation_placeholders["code"] == "5"
    # the real state is read back: still 24, not the refused 29
    assert hass.states.get(climate_id(hass)).attributes["temperature"] == 24
    assert "rejected a command, errNo=5" in caplog.text


async def test_errno_for_a_value_the_unit_has_is_no_error(hass, setup_entry, ws_factory):
    ws = ws_factory.current
    with patch.object(C, "WS_COMMAND_WAIT", 2):
        task = await _call_and_answer(hass, ws, "set_temperature", {"temperature": 24}, -1)
        await task


async def test_confirmed_command_returns_at_once(hass, setup_entry, ws_factory):
    haier = setup_entry.runtime_data
    ws = ws_factory.current
    with patch.object(C, "WS_COMMAND_WAIT", 5):
        task = await _call_and_answer(hass, ws, "set_temperature", {"temperature": 22}, 0)
        async with asyncio.timeout(1):
            await task
    assert haier._waiters == {}


async def test_power_on_refused_for_a_running_unit_sends_the_mode_alone(hass, setup_entry, ws_factory):
    """errNo 22 for "power on" of a unit that runs takes the mode down with it (1.6.2)."""
    ws = ws_factory.current
    with patch.object(C, "WS_COMMAND_WAIT", 2):
        task = await _call_and_answer(hass, ws, "set_hvac_mode", {"hvac_mode": "heat"}, 22, 0)
        await task
    first, second = ws.sent[-2:]
    assert {c["commandName"] for c in first["commands"]} == {"21", "2"}
    assert [c["commandName"] for c in second["commands"]] == ["2"]


# --- a reminder to clean the filter locks nothing ---


async def test_filter_reminder_does_not_lock(hass, setup_entry, ws_factory):
    device = setup_entry.runtime_data.devices[0]
    device._status_data = copy.deepcopy(device.status_data)
    device._status_data["modifiers"].append({
        "priority": 1,
        "trigger": {"operator": "OR", "alarms": ["filterCleaningAlarm"]},
        "actions": [{"name": "0", "rewriteFields": "W", "writable": False}],
    })
    ws_factory.current.feed({
        "event": "status", "macAddress": MAC_1,
        "payload": {"statuses": [{"properties": {}, "alarms": ["filterCleaningAlarm"]}]},
    })
    await hass.async_block_till_done()
    await device.set_temperature(22)
    assert ws_factory.current.sent[-1]["commands"] == [{"commandName": "0", "value": "22"}]


# --- the device list and the descriptions are compared with the cloud ---


async def test_new_device_in_the_app_reloads(hass, config_entry, mock_cloud, ws_factory, aioclient_mock):
    mock_cloud()
    aioclient_mock.clear_requests()
    cloud(aioclient_mock, mac_2_status=403)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    haier = config_entry.runtime_data
    assert [d.device_id for d in haier.devices] == [MAC_1]
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        # still refused: skipped by the setup as well, no reload
        await haier._async_check_device_list()
        reload.assert_not_called()
        cloud(aioclient_mock)
        await haier._async_check_device_list()
        reload.assert_called_once_with(config_entry.entry_id)
    await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()


def _page_without_mac_2() -> dict:
    page = load_fixture("devices_page.json")
    container = page["data"]["presentation"]["layout"]["scrollContainer"][1]
    state = json.loads(container["state"])
    state["items"] = state["items"][:1]
    container["state"] = json.dumps(state)
    return page


async def test_device_removed_in_the_app_reloads(hass, setup_entry, aioclient_mock, hass_storage):
    """Gone from the list and refused (1.7): reloaded, and not waited for at the next start."""
    haier = setup_entry.runtime_data
    aioclient_mock.clear_requests()
    aioclient_mock.get(DEVICES_URL, json=_page_without_mac_2())
    aioclient_mock.get(status_url(MAC_1), json=load_fixture("status_as25php3hra.json"))
    aioclient_mock.get(status_url(MAC_2), status=403, text="")
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        # found by the periodic state check, not only by the six-hourly list check
        await haier._async_resync()
    reload.assert_called_once_with(setup_entry.entry_id)
    assert haier.missing == {MAC_2.lower()}
    saved = hass_storage[cache_key(setup_entry)]["data"]
    assert saved["missing"] == [MAC_2.lower()]
    assert [d["mac"] for d in saved["devices"]] == [MAC_1]


async def test_device_left_out_of_the_list_but_answering_stays(hass, setup_entry, aioclient_mock):
    """The list is not reliable (#105): a unit that still answers is no removal."""
    haier = setup_entry.runtime_data
    aioclient_mock.clear_requests()
    aioclient_mock.get(DEVICES_URL, json=_page_without_mac_2())
    aioclient_mock.get(status_url(MAC_1), json=load_fixture("status_as25php3hra.json"))
    aioclient_mock.get(status_url(MAC_2), json=load_fixture("status_as70s2sf2fa.json"))
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        await haier._async_check_device_list()
    reload.assert_not_called()
    assert haier.missing == set()


async def test_saved_descriptions_leave_out_links_and_analytics(hass, setup_entry, hass_storage):
    """As 1.7 saves them: presigned links and app analytics are not kept."""
    haier = setup_entry.runtime_data
    device = haier.devices[0]
    device.status_data["trackingData"] = {"x": 1}
    device.status_data["icon"] = "https://storage.example/icon.png?X-Amz-Credential=secret"
    saved = haier.cache_dict()["devices"][0]["status"]
    assert "trackingData" not in saved
    assert saved["icon"] == ""
    assert "attributes" in saved


async def test_changed_description_reloads(hass, setup_entry, aioclient_mock):
    haier = setup_entry.runtime_data
    status = load_fixture("status_as25php3hra.json")
    status["settings"]["firmware"]["value"] = "9.9.9"
    aioclient_mock.clear_requests()
    aioclient_mock.get(status_url(MAC_1), json=status)
    aioclient_mock.get(status_url(MAC_2), json=load_fixture("status_as70s2sf2fa.json"))
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        await haier._async_resync()
        await haier._async_resync()
    reload.assert_called_once_with(setup_entry.entry_id)


async def test_same_description_does_not_reload(hass, setup_entry):
    haier = setup_entry.runtime_data
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        await haier._async_resync()
    reload.assert_not_called()


def test_appliances_without_wifi_are_left_out():
    import json

    page = load_fixture("devices_page.json")["data"]
    block = page["presentation"]["layout"]["scrollContainer"][1]

    state = json.loads(block["state"]) if isinstance(block["state"], str) else block["state"]
    state["items"].append({"title": "Посудомойка", "action": {"link": "haierevo://non-iot/device?deviceId=5b1c-uuid"}})
    state["items"].append({"title": "Странное", "action": {"link": "haierevo://device?deviceId=not-a-mac"}})
    block["state"] = json.dumps(state)
    assert [d["mac"] for d in api.parse_device_list(page)] == [MAC_1, MAC_2]


# --- diagnostics hide by the form of a value ---


async def test_diagnostics_hide_emails_tokens_ips(hass, setup_entry):
    device = setup_entry.runtime_data.devices[0]
    device._status_data = {
        **device.status_data,
        "shared": {"ownerLogin": "someone@example.org", "note": "write to other@example.com"},
        "debug": "jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.sig and 192.168.1.10, +7 (912) 345-67-89",
        "rawMac": "00005e005301",
    }
    text = str(await async_get_config_entry_diagnostics(hass, setup_entry))
    for secret in ("someone@example.org", "other@example.com", "eyJhbGci", "192.168.1.10",
                   "345-67-89", "00005e005301", MAC_1, MAC_2):
        assert secret not in text, secret


# --- profile names and old copies ---


def test_model_with_path_parts_uses_the_default_profile():
    for model in ("../../secrets", "AS25/../x", "a" * 40, ""):
        assert CFG.HaierACConfig(model, "")._find_config_file().endswith("default.yaml")


def test_old_copy_of_a_bundled_profile_is_removed(tmp_path):
    bundled = CFG.HaierACConfig("AS25PHP2HRA", "")._find_config_file()
    user = str(tmp_path / "haier_evo")
    copy_path = f"{user}.AS25PHP2HRA.yaml"
    shutil.copyfile(bundled, copy_path)
    assert CFG.HaierACConfig("AS25PHP2HRA", user)._find_config_file() == bundled
    assert not os.path.exists(copy_path)


def test_own_profile_is_kept(tmp_path):
    bundled = CFG.HaierACConfig("AS25PHP2HRA", "")._find_config_file()
    user = str(tmp_path / "haier_evo")
    own = f"{user}.AS25PHP2HRA.yaml"
    with open(bundled, encoding="utf-8") as src, open(own, "w", encoding="utf-8") as dst:
        dst.write(src.read() + "\n# my own change\n")
    assert CFG.HaierACConfig("AS25PHP2HRA", user)._find_config_file() == own
    with contextlib.suppress(FileNotFoundError):
        os.remove(own)
