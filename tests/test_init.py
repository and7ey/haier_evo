"""Entry setup/unload, empty device list, 429, auth failure, diagnostics."""
from __future__ import annotations

import json
import os

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.haier_evo.const import DOMAIN
from custom_components.haier_evo.diagnostics import async_get_config_entry_diagnostics

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


async def test_setup_creates_entities(hass: HomeAssistant, setup_entry):
    assert setup_entry.state is ConfigEntryState.LOADED
    registry = er.async_get(hass)
    unique_ids = {e.unique_id for e in er.async_entries_for_config_entry(registry, setup_entry.entry_id)}
    assert f"{MAC_1}_AS25PHP3HRA" in unique_ids
    assert f"{MAC_2}_AS70S2SF2FA" in unique_ids
    # no more "Haier Evo HTTP GET/POST" switches (#100)
    assert not [u for u in unique_ids if "http_switch" in u]

    entity_id = registry.async_get_entity_id("climate", DOMAIN, f"{MAC_1}_AS25PHP3HRA")
    state = hass.states.get(entity_id)
    assert state.state == "cool"
    assert state.attributes["current_temperature"] == 22.5
    assert state.attributes["temperature"] == 24
    assert state.attributes["fan_mode"] == "auto"
    assert state.attributes["min_temp"] == 16
    assert state.attributes["max_temp"] == 30
    assert set(state.attributes["hvac_modes"]) == {"auto", "cool", "dry", "heat", "fan_only", "off"}


async def test_tokens_not_written_to_config_dir(hass: HomeAssistant, setup_entry, hass_storage):
    assert f"{DOMAIN}.{setup_entry.entry_id}" in hass_storage
    # the old client kept tokens in /config/haier_evo next to configuration.yaml
    import os
    assert not os.path.exists(hass.config.path(DOMAIN))


async def test_empty_device_list_retries(hass: HomeAssistant, config_entry, mock_cloud, ws_factory):
    """#105: an empty deviceList must not load an empty integration."""
    mock_cloud(devices="devices_page_empty.json")
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_RETRY
    assert ws_factory.urls == []


async def test_429_on_setup_retries(hass: HomeAssistant, config_entry, mock_cloud):
    mock_cloud(login_status=429)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_cloud_down_on_setup_retries(hass: HomeAssistant, config_entry, aioclient_mock):
    aioclient_mock.post(LOGIN_URL, status=502, text="Bad gateway")
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_wrong_password_starts_reauth(hass: HomeAssistant, config_entry, aioclient_mock):
    aioclient_mock.post(
        LOGIN_URL, status=400, json={"error": {"validation": {"password": "Неверный пароль"}}}
    )
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]


async def test_old_entry_without_unique_id_is_migrated(hass, mock_cloud, ws_factory):
    entry = MockConfigEntry(
        domain=DOMAIN, data={"email": EMAIL, "password": PASSWORD, "region": "ru"}
    )
    entry.add_to_hass(hass)
    mock_cloud()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.unique_id == f"{EMAIL}_ru"
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_unload(hass: HomeAssistant, setup_entry, ws_factory):
    ws = ws_factory.current
    assert await hass.config_entries.async_unload(setup_entry.entry_id)
    await hass.async_block_till_done()
    assert setup_entry.state is ConfigEntryState.NOT_LOADED
    assert ws.closed


async def test_diagnostics_are_redacted(hass: HomeAssistant, setup_entry):
    # Real status payloads carry the MAC in free-form strings and presigned icon URLs.
    device = setup_entry.runtime_data.devices[0]
    device._status_data = {
        **(device.status_data or {}),
        "scenarios": {"default": {
            "title": "Сценарии",
            "deeplink": f"haierevo://hiHome/device/scenario?macAddress={MAC_1}",
            "icon": "https://storage.example/icon.png?X-Amz-Credential=TESTKEY&X-Amz-Signature=abc",
        }},
        "note": f"encoded {MAC_2.replace(':', '%3A').upper()}",
    }
    diag = await async_get_config_entry_diagnostics(hass, setup_entry)
    text = json.dumps(diag, ensure_ascii=False)
    for secret in (
        PASSWORD, EMAIL, MAC_1, MAC_2, MAC_2.replace(":", "%3A").upper(),
        "TESTSERIAL0001", "test-access-token", "TESTKEY",
    ):
        assert secret not in text
    assert diag["entry"]["title"] == "**REDACTED**"
    scenario = diag["haier"]["devices"][0]["backend_data"]["scenarios"]["default"]
    assert scenario["title"] == "Сценарии"
    assert scenario["icon"] == "https://storage.example/icon.png"
    assert diag["haier"]["devices"][0]["device_model"] == "AS25PHP3HRA"


async def test_diagnostics_of_a_failed_entry(hass: HomeAssistant, config_entry, mock_cloud):
    mock_cloud(login_status=429)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    diag = await async_get_config_entry_diagnostics(hass, config_entry)
    assert diag["haier"] is None
    assert EMAIL not in json.dumps(diag)


async def test_legacy_token_file_is_removed(hass: HomeAssistant, config_entry, mock_cloud, ws_factory):
    path = hass.config.path(DOMAIN)
    await hass.async_add_executor_job(
        lambda: open(path, "w", encoding="utf-8").write('{"token": "x", "refresh_token": "y"}')
    )
    mock_cloud()
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert not await hass.async_add_executor_job(os.path.exists, path)
    await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()


async def test_legacy_token_file_of_two_entries_is_neither_used_nor_deleted(
    hass: HomeAssistant, config_entry, mock_cloud, ws_factory
):
    """1.7: with two entries there is no telling whose the file is."""
    MockConfigEntry(domain=DOMAIN, data={"email": "other@example.com", "region": "ru"},
                    unique_id="other@example.com_ru").add_to_hass(hass)
    path = hass.config.path(DOMAIN)
    await hass.async_add_executor_job(
        lambda: open(path, "w", encoding="utf-8").write('{"token": "x", "refreshtoken": "y"}')
    )
    mock_cloud()
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert await hass.async_add_executor_job(os.path.exists, path)
    await hass.async_add_executor_job(os.remove, path)
    await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()


async def test_legacy_token_file_that_cannot_be_removed_does_not_fail_the_setup(
    hass: HomeAssistant, config_entry, mock_cloud, ws_factory
):
    from unittest.mock import patch

    path = hass.config.path(DOMAIN)
    await hass.async_add_executor_job(
        lambda: open(path, "w", encoding="utf-8").write('{"token": "x", "refreshtoken": "y"}')
    )
    mock_cloud()
    with patch("custom_components.haier_evo.os.remove", side_effect=PermissionError("read-only")):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.LOADED
    await hass.async_add_executor_job(os.remove, path)
    await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()


async def test_devices_request_has_no_token_in_log(hass, setup_entry, aioclient_mock, caplog):
    assert DEVICES_URL.split("?")[0] in str(aioclient_mock.mock_calls[1][1])
    assert "test-access-token" not in caplog.text


async def test_token_rejected_on_setup_retries_instead_of_reauth(
    hass: HomeAssistant, config_entry, mock_cloud, aioclient_mock
):
    """2026-11-04, after a power cut, 'Access token rejected twice' ended in reauth."""
    mock_cloud()
    aioclient_mock.clear_requests()
    aioclient_mock.post(LOGIN_URL, json=load_fixture("login.json"))
    aioclient_mock.post(REFRESH_URL, json=load_fixture("login.json"))
    aioclient_mock.get(DEVICES_URL, json=load_fixture("devices_page.json"))
    aioclient_mock.get(status_url(MAC_1), status=403, text="")
    aioclient_mock.get(status_url(MAC_2), status=401, text="")
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_RETRY
    assert not hass.config_entries.flow.async_progress()


async def test_names_and_native_temperatures(hass: HomeAssistant, setup_entry, caplog):
    """HA 2026.11: has_entity_name, native_* climate temperatures, no deprecation warnings."""
    registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(registry, setup_entry.entry_id)
    assert entries and all(e.has_entity_name for e in entries)
    climate_id = registry.async_get_entity_id("climate", DOMAIN, f"{MAC_1}_AS25PHP3HRA")
    climate = registry.async_get(climate_id)
    device = dr.async_get(hass).async_get(climate.device_id)
    assert climate.original_name is None
    assert hass.states.get(climate_id).name == device.name
    light = next(e for e in entries if e.entity_id.startswith("switch.") and e.device_id == climate.device_id
                 and e.original_name == "Подсветка блока")
    assert hass.states.get(light.entity_id).name == f"{device.name} Подсветка блока"
    assert hass.states.get(climate_id).attributes["current_temperature"] == 22.5
    assert "deprecated" not in caplog.text


async def test_requests_look_like_the_app(hass: HomeAssistant, setup_entry, aioclient_mock):
    """The headers of the Evo app, the same Device-Id on every request of an entry."""
    from custom_components.haier_evo import entry_device_id

    calls = [c for c in aioclient_mock.mock_calls if c[3]]
    assert calls
    for _method, _url, _data, headers in calls:
        assert headers["User-Agent"] == "evo-mobile"
        assert headers["Platform"] == "android"
        assert headers["Device-Id"] == entry_device_id(setup_entry.entry_id)


async def test_remove_deletes_tokens_and_descriptions(hass: HomeAssistant, config_entry, mock_cloud, ws_factory,
                                                      hass_storage):
    mock_cloud()
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    tokens, devices = f"{DOMAIN}.{config_entry.entry_id}", f"{DOMAIN}.{config_entry.entry_id}.devices"
    assert tokens in hass_storage
    assert await hass.config_entries.async_remove(config_entry.entry_id)
    await hass.async_block_till_done()
    assert tokens not in hass_storage
    assert devices not in hass_storage


async def test_diagnostics_are_for_administrators_only(hass: HomeAssistant, setup_entry, hass_client):
    from pytest_homeassistant_custom_component.components.diagnostics import get_diagnostics_for_config_entry

    diagnostics = await get_diagnostics_for_config_entry(hass, hass_client, setup_entry)  # sets diagnostics up
    assert diagnostics["haier"]["devices"]
    user = await hass.auth.async_create_user("Guest", group_ids=["system-users"])
    refresh = await hass.auth.async_create_refresh_token(user, client_id="http://localhost/")
    client = await hass_client(hass.auth.async_create_access_token(refresh))
    response = await client.get(f"/api/diagnostics/config_entry/{setup_entry.entry_id}")
    assert response.status == 401


async def test_debug_switch_device_of_1_1_is_removed(hass: HomeAssistant, config_entry, mock_cloud, ws_factory):
    """The device of the HTTP switches (removed in 1.2.0) leaves the registry, as in 1.7."""
    registry = dr.async_get(hass)
    old = registry.async_get_or_create(
        config_entry_id=config_entry.entry_id, identifiers={(DOMAIN, f"{DOMAIN}_http_switch")}
    )
    mock_cloud()
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert registry.async_get(old.id) is None
    await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
