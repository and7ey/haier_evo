"""Setting up, starting again, unloading and removing the integration in a
real Home Assistant: config entry states, registries, storage, diagnostics."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time

import pytest
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.components.diagnostics import get_diagnostics_for_config_entry

from custom_components.haier_evo import api
from custom_components.haier_evo import const as C

from .conftest import (
    AC_OFF, BOILER, DOMAIN, DUMP, EMAIL, PASSWORD, SERIALS, FakeCloud, entity_id, haier, last_release,
    store_key, wait_for,
)


def _registry(hass: HomeAssistant, entry) -> tuple[set, set]:
    entities = {(e.domain, e.unique_id) for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)}
    devices = {frozenset(d.identifiers) for d in dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)}
    return entities, devices


async def test_setup_creates_what_the_last_release_does(hass: HomeAssistant, setup_entry, caplog) -> None:
    """The registries of a real Home Assistant hold exactly the entities and
    devices that tests/fixtures/entities_by_release.json records for the newest
    release (what check_upgrade.py works out with fakes)."""
    snapshot = last_release()
    entities, devices = _registry(hass, setup_entry)
    assert entities == {tuple(e) for e in snapshot["entities"]}
    assert devices == {frozenset(tuple(i) for i in d) for d in snapshot["devices"]}

    for device in dr.async_entries_for_config_entry(dr.async_get(hass), setup_entry.entry_id):
        assert device.manufacturer == "Haier"
        assert device.model
        assert device.sw_version is None or isinstance(device.sw_version, str)

    # every entity has a state, and the devices are there: online, websocket up
    for domain, unique_id in entities:
        state = hass.states.get(entity_id(hass, domain, unique_id))
        if state is None:
            assert er.async_get(hass).async_get(entity_id(hass, domain, unique_id)).disabled_by is not None
            continue
        assert state.state != STATE_UNAVAILABLE, f"{state.entity_id} is unavailable"

    # a normal start says nothing alarming
    noisy = [r for r in caplog.records if r.levelno >= logging.WARNING and "haier_evo" in (r.name + r.getMessage())
             and "has not been tested by Home Assistant" not in r.getMessage()]
    assert not noisy, [f"{r.levelname} {r.name}: {r.getMessage()}" for r in noisy]


async def test_setup_sends_no_command(hass: HomeAssistant, setup_entry, cloud: FakeCloud) -> None:
    """Starting Home Assistant must not make a sound: every command beeps."""
    await hass.async_block_till_done()
    await wait_for(lambda: haier(hass, setup_entry)._connected.is_set(), "the websocket to open")
    assert cloud.commands() == []


async def test_requests_look_like_the_app(hass: HomeAssistant, setup_entry, cloud: FakeCloud) -> None:
    """The headers of the Evo app, the same Device-Id on every request of an entry."""
    device_id = api.entry_device_id(setup_entry.entry_id)
    requests = cloud.seen("home") + cloud.seen("description")
    assert requests
    for request in requests:
        headers = request["headers"]
        assert headers["User-Agent"] == "evo-mobile"
        assert headers["Platform"] == "android"
        assert headers["Device-Id"] == device_id


async def test_config_flow_tokens_go_to_private_storage(
    hass: HomeAssistant, cloud: FakeCloud, make_entry, hass_storage
) -> None:
    """Tokens handed over by the config flow move to .storage; the entry keeps
    the e-mail and the region only."""
    entry = make_entry(tokens=cloud.tokens())
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert dict(entry.data) == {"email": EMAIL, "region": "ru"}
    assert hass_storage[store_key(entry)]["data"]["refreshtoken"] in cloud.refresh
    cached = hass_storage[store_key(entry, ".devices")]["data"]["devices"]
    assert sorted(d["mac"] for d in cached) == sorted(d["device_id"] for d in DUMP["devices"])


async def test_empty_device_list_retries_setup(hass: HomeAssistant, cloud: FakeCloud, stored_entry) -> None:
    """The cloud sometimes answers with no devices: Home Assistant retries."""
    cloud.plan("home", (200, {"data": {}}))
    assert not await hass.config_entries.async_setup(stored_entry.entry_id)
    assert stored_entry.state is ConfigEntryState.SETUP_RETRY

    assert await hass.config_entries.async_reload(stored_entry.entry_id)
    await hass.async_block_till_done()
    assert stored_entry.state is ConfigEntryState.LOADED


@pytest.mark.parametrize("status", [500, 503])
async def test_cloud_failure_retries_setup(hass: HomeAssistant, cloud: FakeCloud, stored_entry, status: int) -> None:
    cloud.plan("description", (status, "<html>oops</html>"), (status, "<html>oops</html>"))
    assert not await hass.config_entries.async_setup(stored_entry.entry_id)
    assert stored_entry.state is ConfigEntryState.SETUP_RETRY


async def test_device_the_cloud_refuses_is_skipped(hass: HomeAssistant, cloud: FakeCloud, stored_entry) -> None:
    """HTTP 403 for one description (a device no longer shared): the others start."""
    cloud.denied.add(AC_OFF)
    assert await hass.config_entries.async_setup(stored_entry.entry_id)
    await hass.async_block_till_done()
    assert stored_entry.state is ConfigEntryState.LOADED
    macs = {d.device_id for d in haier(hass, stored_entry).devices}
    assert macs == {d["device_id"] for d in DUMP["devices"]} - {AC_OFF}


async def test_rejected_sign_in_asks_for_the_password(
    hass: HomeAssistant, cloud: FakeCloud, make_entry, hass_storage
) -> None:
    """Expired access token, refresh token refused, no password kept: re-authentication."""
    entry = make_entry()
    tokens = cloud.tokens(minutes=-10)
    tokens["refreshtoken"] = "ref-revoked"
    hass_storage[store_key(entry)] = {"version": 1, "minor_version": 1, "key": store_key(entry), "data": tokens}
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]
    assert not cloud.seen("sign-in"), "the password is not kept, so no sign-in can be tried"


async def test_start_from_saved_descriptions_without_the_cloud(
    hass: HomeAssistant, cloud: FakeCloud, setup_entry
) -> None:
    """Second start with the cloud down: the entities are there at once,
    unavailable until the real state has been read."""
    climate = entity_id(hass, "climate", f"{AC_OFF}_AS25S2SF2FA")
    assert await hass.config_entries.async_unload(setup_entry.entry_id)
    cloud.down.update({"home", "description"})
    cloud.ws_refused = True

    assert await hass.config_entries.async_setup(setup_entry.entry_id)
    await hass.async_block_till_done()
    assert setup_entry.state is ConfigEntryState.LOADED
    assert hass.states.get(climate).state == STATE_UNAVAILABLE

    cloud.down.clear()
    cloud.ws_refused = False
    await wait_for(lambda: hass.states.get(climate).state != STATE_UNAVAILABLE, "the state read in the background")
    assert hass.states.get(climate).state == "off"


async def test_unload_closes_the_websocket(hass: HomeAssistant, cloud: FakeCloud, setup_entry) -> None:
    assert await hass.config_entries.async_unload(setup_entry.entry_id)
    await hass.async_block_till_done()
    assert setup_entry.state is ConfigEntryState.NOT_LOADED
    await wait_for(lambda: not cloud.sockets, "the websocket to close")
    assert DOMAIN not in hass.data or setup_entry.entry_id not in hass.data[DOMAIN]


@pytest.mark.parametrize("answer_close", [True, False], ids=["cloud answers the close", "half-dead connection"])
async def test_unload_does_not_hold_up_the_event_loop(
    hass: HomeAssistant, cloud: FakeCloud, setup_entry, answer_close: bool
) -> None:
    """Up to 1.6.4 the unload closed the websocket in the event loop, where all
    of Home Assistant runs, and websocket-client waited there for the cloud's
    answer to the close frame: as long as the cloud took, up to 3 s. Now the
    session ends at once, whatever the cloud does."""
    cloud.answer_close = answer_close
    client = haier(hass, setup_entry)
    gaps: list[float] = []
    done = asyncio.Event()

    async def heartbeat() -> None:
        last = time.monotonic()
        while not done.is_set():
            await asyncio.sleep(0.01)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    beat = hass.async_create_task(heartbeat())
    started = time.monotonic()
    assert await hass.config_entries.async_unload(setup_entry.entry_id)
    unload = time.monotonic() - started
    await wait_for(lambda: not cloud.sockets, "the websocket to close", timeout=2)
    await wait_for(lambda: not client.socket_thread.is_alive(), "the websocket thread to end", timeout=2)
    done.set()
    await beat
    assert max(gaps) < 0.5, f"the event loop stood still for {max(gaps):.1f} s"
    assert unload < 0.5, f"the unload took {unload:.1f} s"


async def test_reload_reconnects(hass: HomeAssistant, cloud: FakeCloud, setup_entry) -> None:
    assert await hass.config_entries.async_reload(setup_entry.entry_id)
    await hass.async_block_till_done()
    assert setup_entry.state is ConfigEntryState.LOADED
    await wait_for(lambda: len(cloud.sockets) == 1 and len(cloud.ws_tokens) >= 2, "one new session")


async def test_remove_deletes_tokens_and_descriptions(hass: HomeAssistant, setup_entry, hass_storage) -> None:
    assert store_key(setup_entry) in hass_storage
    assert store_key(setup_entry, ".devices") in hass_storage
    await hass.config_entries.async_remove(setup_entry.entry_id)
    await hass.async_block_till_done()
    assert store_key(setup_entry) not in hass_storage
    assert store_key(setup_entry, ".devices") not in hass_storage


async def test_upgrade_from_1_1_moves_the_tokens_and_drops_the_password(
    hass: HomeAssistant, cloud: FakeCloud, make_entry, hass_storage
) -> None:
    """Up to 1.1.x: the password in the entry, the tokens in /config/haier_evo."""
    entry = make_entry(password=PASSWORD)
    legacy = hass.config.path(DOMAIN)
    tokens = cloud.tokens()
    await hass.async_add_executor_job(lambda: open(legacy, "w", encoding="utf-8").write(json.dumps(tokens)))

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert "password" not in entry.data
    assert hass_storage[store_key(entry)]["data"]["refreshtoken"] == tokens["refreshtoken"]
    assert not await hass.async_add_executor_job(os.path.exists, legacy)
    assert not cloud.seen("sign-in"), "the saved tokens are enough"


async def test_diagnostics_are_redacted(hass: HomeAssistant, setup_entry, hass_client) -> None:
    diagnostics = await get_diagnostics_for_config_entry(hass, hass_client, setup_entry)
    text = json.dumps(diagnostics, ensure_ascii=False)
    tokens = haier(hass, setup_entry).tokens_dict()
    for secret in (EMAIL, PASSWORD, tokens["token"], tokens["refreshtoken"], *SERIALS,
                   *(d["device_id"] for d in DUMP["devices"])):
        assert secret not in text, f"{secret} is in the diagnostics"
    devices = diagnostics["integration"]["devices"]
    assert len(devices) == len(DUMP["devices"])
    assert {d["device_name"] for d in devices} >= {"Бойлер"}


async def test_diagnostics_are_for_administrators_only(hass: HomeAssistant, setup_entry, hass_client) -> None:
    await get_diagnostics_for_config_entry(hass, hass_client, setup_entry)  # sets diagnostics up
    user = await hass.auth.async_create_user("Guest", group_ids=["system-users"])
    refresh = await hass.auth.async_create_refresh_token(user, client_id="http://localhost/")
    client = await hass_client(hass.auth.async_create_access_token(refresh))
    response = await client.get(f"/api/diagnostics/config_entry/{setup_entry.entry_id}")
    assert response.status == 401


async def test_water_heater_entity_is_there(hass: HomeAssistant, setup_entry) -> None:
    state = hass.states.get(entity_id(hass, "water_heater", f"{BOILER}_ES80VF7"))
    assert state is not None and state.state != STATE_UNAVAILABLE
    assert state.attributes["temperature"] == 75
    assert state.attributes["current_temperature"] == 69


async def test_without_the_websocket_the_entities_go_unavailable(
    hass: HomeAssistant, setup_entry, cloud: FakeCloud, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The state shown may be stale once the websocket has been down a while
    (3 minutes, here 1 s); it is shown again once it is back."""
    monkeypatch.setattr(C, "WS_UNAVAILABLE_AFTER", 1)
    climate = entity_id(hass, "climate", f"{AC_OFF}_AS25S2SF2FA")
    cloud.ws_refused = True
    await cloud.drop_sessions()
    await wait_for(lambda: hass.states.get(climate).state == STATE_UNAVAILABLE, "the entities to go unavailable")
    cloud.ws_refused = False
    await wait_for(lambda: hass.states.get(climate).state == "off", "the entities to come back")


async def test_tokens_revoked_while_running_ask_for_the_password(
    hass: HomeAssistant, setup_entry, cloud: FakeCloud
) -> None:
    """The websocket thread finds the sign-in refused: Home Assistant shows
    the re-authentication (started from that thread, in the event loop)."""
    cloud.access.clear()
    cloud.refresh.clear()
    await cloud.drop_sessions()
    await wait_for(lambda: hass.config_entries.flow.async_progress_by_handler(DOMAIN), "a re-authentication")
    [flow] = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert flow["context"]["source"] == SOURCE_REAUTH
    assert haier(hass, setup_entry).disconnect_requested


async def test_diagnostics_of_an_entry_that_did_not_start(
    hass: HomeAssistant, cloud: FakeCloud, stored_entry, hass_client
) -> None:
    cloud.down.add("home")
    assert not await hass.config_entries.async_setup(stored_entry.entry_id)
    assert stored_entry.state is ConfigEntryState.SETUP_RETRY
    diagnostics = await get_diagnostics_for_config_entry(hass, hass_client, stored_entry)
    assert diagnostics["integration"] is None
    assert EMAIL not in json.dumps(diagnostics, ensure_ascii=False)


async def test_switches_are_named_as_in_the_app(hass: HomeAssistant, setup_entry) -> None:
    names = {
        hass.states.get(e.entity_id).name
        for e in er.async_entries_for_config_entry(er.async_get(hass), setup_entry.entry_id)
        if e.domain == "switch" and e.unique_id.startswith(AC_OFF)
    }
    assert names == {f"Кондиционер 2 {name}" for name in (
        "Подсветка блока", "Тихий", "Турбо", "Здоровье", "Комфортный сон", "Стерильная очистка",
        "10 градусов", "Самоочистка",
    )}
