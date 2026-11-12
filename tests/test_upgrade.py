"""An update from 1.x and from the temandroid/haier_evo_2 fork (0.x).

Same domain, same Store keys, other formats: tokens named "tokenexpire",
"refreshtoken", "refreshexpire", no password in the entry, the saved device
descriptions under "config". The entry must start as it is, without asking
the user to sign in again, and keep no password.
"""
from __future__ import annotations

import contextlib
import json
import os

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.haier_evo.const import DOMAIN

from .conftest import (
    DEVICES_URL,
    EMAIL,
    LOGIN_URL,
    MAC_1,
    MAC_2,
    PASSWORD,
    REFRESH_URL,
    load_fixture,
    status_url,
)

UPSTREAM_TOKENS = {
    "token": "v1-access-token",
    "tokenexpire": "2099-01-01T00:00:00+03:00",
    "refreshtoken": "v1-refresh-token",
    "refreshexpire": "2099-02-01T00:00:00+03:00",
}


def v1_entry(hass: HomeAssistant) -> MockConfigEntry:
    """An entry as 1.2+ leaves it: no password, the tokens in .storage."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=f"{EMAIL} (ru)",
        unique_id=f"{EMAIL}_ru",
        data={"email": EMAIL, "region": "ru"},
        options={"laundry_control": False},
    )
    entry.add_to_hass(hass)
    return entry


def store(hass_storage, key: str, data: dict) -> None:
    hass_storage[key] = {"version": 1, "key": key, "data": data}


def _write_json(path: str, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as file:
        json.dump(data, file)


def sign_ins(aioclient_mock) -> int:
    return sum(1 for c in aioclient_mock.mock_calls if str(c[1]).endswith("/auth/sign-in"))


def auth_headers(aioclient_mock, path: str) -> list[str]:
    return [
        c[3].get("X-Auth-Token") for c in aioclient_mock.mock_calls
        if str(c[1]).split("?")[0].endswith(path)
    ]


async def _unload(hass, entry) -> None:
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_v1_tokens_are_read_and_resaved(hass, mock_cloud, ws_factory, hass_storage):
    entry = v1_entry(hass)
    store(hass_storage, f"{DOMAIN}.{entry.entry_id}", dict(UPSTREAM_TOKENS))
    aioclient_mock = mock_cloud()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert sign_ins(aioclient_mock) == 0
    assert auth_headers(aioclient_mock, "/smartHome") == ["v1-access-token"]
    await ws_factory.wait_for(1)
    assert ws_factory.urls[0].endswith("/v1-access-token")
    assert "password" not in entry.data
    await _unload(hass, entry)


async def test_v1_tokens_saved_in_the_current_format_after_refresh(hass, mock_cloud, ws_factory, hass_storage):
    entry = v1_entry(hass)
    store(hass_storage, f"{DOMAIN}.{entry.entry_id}", {**UPSTREAM_TOKENS, "tokenexpire": "2000-01-01T00:00:00+03:00"})
    aioclient_mock = mock_cloud()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert sign_ins(aioclient_mock) == 0
    refresh = [c for c in aioclient_mock.mock_calls if str(c[1]).endswith("/auth/refresh")]
    assert refresh[0][2] == {"refreshToken": "v1-refresh-token"}
    saved = hass_storage[f"{DOMAIN}.{entry.entry_id}"]["data"]
    assert saved["token"] == "test-access-token"
    assert saved["refreshtoken"] == "test-refresh-token"
    await _unload(hass, entry)


async def test_rejected_refresh_without_password_asks_the_user(hass, aioclient_mock, hass_storage):
    """With no password kept, only the user can sign in again: reauth, no sign-in attempt."""
    entry = v1_entry(hass)
    store(hass_storage, f"{DOMAIN}.{entry.entry_id}", {**UPSTREAM_TOKENS, "tokenexpire": "2000-01-01T00:00:00+03:00"})
    aioclient_mock.post(REFRESH_URL, status=400, json={"error": {"validation": {"refreshToken": "expired"}}})
    aioclient_mock.post(LOGIN_URL, json=load_fixture("login.json"))
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert sign_ins(aioclient_mock) == 0
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]


async def test_token_rejected_twice_without_password_is_not_reauth(hass, aioclient_mock, hass_storage):
    """Without a password, a data endpoint refusing fresh tokens is a cloud problem."""
    entry = v1_entry(hass)
    store(hass_storage, f"{DOMAIN}.{entry.entry_id}", dict(UPSTREAM_TOKENS))
    aioclient_mock.post(REFRESH_URL, json=load_fixture("login.json"))
    aioclient_mock.get(DEVICES_URL, status=401, text="")
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert sign_ins(aioclient_mock) == 0
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)


async def test_password_is_dropped_after_a_good_start(hass, setup_entry, hass_storage):
    """Our 0.x kept the password in the entry: it goes once the cloud took the tokens."""
    assert "password" not in setup_entry.data
    assert hass_storage[f"{DOMAIN}.{setup_entry.entry_id}"]["data"]["refreshtoken"] == "test-refresh-token"


async def test_password_kept_while_started_from_cache(hass, config_entry, aioclient_mock, ws_factory, hass_storage):
    """No verdict from the cloud yet: the password stays until a real start."""
    store(hass_storage, f"{DOMAIN}.{config_entry.entry_id}.devices", {"devices": [
        {"mac": MAC_1, "type": "AC", "serial": "", "title": "Кабинет",
         "status": load_fixture("status_as25php3hra.json")},
    ]})
    aioclient_mock.post(LOGIN_URL, json=load_fixture("login.json"))
    aioclient_mock.get(DEVICES_URL, status=502, text="Bad gateway")
    aioclient_mock.get(status_url(MAC_1), status=502, text="Bad gateway")
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.runtime_data.from_cache
    assert config_entry.data["password"] == PASSWORD
    await _unload(hass, config_entry)


async def test_old_token_file_is_moved_to_storage(hass, mock_cloud, ws_factory, hass_storage):
    """Upstream up to 1.1 kept the tokens in /config/haier_evo; without a password they are needed."""
    entry = v1_entry(hass)
    path = hass.config.path(DOMAIN)
    await hass.async_add_executor_job(_write_json, path, UPSTREAM_TOKENS)
    try:
        aioclient_mock = mock_cloud()
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        assert sign_ins(aioclient_mock) == 0
        assert auth_headers(aioclient_mock, "/smartHome") == ["v1-access-token"]
        assert not os.path.exists(path)
        assert hass_storage[f"{DOMAIN}.{entry.entry_id}"]["data"]["refreshtoken"] == "v1-refresh-token"
        await _unload(hass, entry)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.remove(path)


async def test_v1_saved_descriptions_start_without_cloud(hass, aioclient_mock, ws_factory, hass_storage):
    """The cache of 1.x ("config" per device) serves a start without the cloud."""
    entry = v1_entry(hass)
    store(hass_storage, f"{DOMAIN}.{entry.entry_id}", dict(UPSTREAM_TOKENS))
    store(hass_storage, f"{DOMAIN}.{entry.entry_id}.devices", {"version": 1, "devices": [
        {"type": "AC", "mac": MAC_1, "serial": "", "title": "Кабинет",
         "config": load_fixture("status_as25php3hra.json")},
        {"type": "AC", "mac": MAC_2, "serial": "", "title": "Зал",
         "config": load_fixture("status_as70s2sf2fa.json")},
    ]})
    aioclient_mock.get(DEVICES_URL, status=502, text="Bad gateway")
    aioclient_mock.get(status_url(MAC_1), status=502, text="")
    aioclient_mock.get(status_url(MAC_2), status=502, text="")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    haier = entry.runtime_data
    assert haier.from_cache
    assert {d.device_id for d in haier.devices} == {MAC_1, MAC_2}
    await _unload(hass, entry)
