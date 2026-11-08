from __future__ import annotations
import asyncio
import os
import re
import threading
from datetime import timedelta
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.const import Platform
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.loader import async_get_integration
from .logger import _LOGGER
from .const import (
    DOMAIN, SETUP_INCOMPLETE_RETRIES, STORAGE_VERSION, LEGACY_TOKENS_FILE,
    CACHE_VERSION, CACHE_RETRY_FIRST, CACHE_RETRY_MAX, CACHE_REFRESH_HOURS,
    STATE_REFRESH_MINUTES,
)
from . import api


PLATFORMS: list[str] = [
    Platform.CLIMATE,
    Platform.SWITCH,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.WATER_HEATER,
]

# entry_id -> setups in a row that got fewer devices than the registry knows
_INCOMPLETE_SETUPS: dict[str, int] = {}

_MAC = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
_HTTP_DEVICE = (DOMAIN, f"{DOMAIN}_http_switch")  # debug switches, removed in 1.2.0


def _known_device_ids(hass: HomeAssistant, entry: ConfigEntry) -> set[str]:
    """MACs of the Haier devices this entry created before (not the service HTTP device)."""
    registry = dr.async_get(hass)
    return {
        identifier.lower()
        for device in dr.async_entries_for_config_entry(registry, entry.entry_id)
        for domain, identifier in device.identifiers
        if domain == DOMAIN and _MAC.match(identifier.lower())
    }


def _find_device(registry: dr.DeviceRegistry, identifier: tuple[str, str], entry_id: str):
    """Device of this entry by identifier; async_get_device is deprecated from HA 2026.x."""
    if (by_identifier := getattr(registry, "async_get_device_by_identifier", None)) is not None:
        return by_identifier(identifier, entry_id)
    return registry.async_get_device(identifiers={identifier})


async def _stop(hass: HomeAssistant, haier_object: api.Haier) -> None:
    """Stop the client off the event loop, as the fork JampireX/haier_evo does:
    it closes sockets (up to 1.6.4 it also waited there for the cloud's answer
    to the websocket close, freezing Home Assistant for up to 3 s). A failure
    must not mask the setup error being raised or fail the unload."""
    try:
        await hass.async_add_executor_job(haier_object.stop)
    except Exception as e:
        _LOGGER.warning(f"Haier Evo: stopping the connection failed: {e!r}")


def _token_store(hass: HomeAssistant, entry_id: str) -> Store:
    # private: 0600, atomic: a crash never leaves a half-written token file
    return Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}", private=True, atomic_writes=True)


def _devices_store(hass: HomeAssistant, entry_id: str) -> Store:
    # device descriptions of the last good start (MACs, serials: private too)
    return Store(hass, CACHE_VERSION, f"{DOMAIN}.{entry_id}.devices", private=True, atomic_writes=True)


def _cached_devices(data) -> list[dict] | None:
    devices = data.get("devices") if isinstance(data, dict) else None
    if isinstance(devices, list) and devices and all(isinstance(d, dict) for d in devices):
        return devices
    return None


async def _save_devices(hass: HomeAssistant, store: Store, haier_object: api.Haier) -> None:
    try:
        await store.async_save(await hass.async_add_executor_job(haier_object.cache_dict))
    except Exception as e:
        _LOGGER.warning(f"Failed to save the device descriptions: {e!r}")


async def _refresh(hass: HomeAssistant, entry: ConfigEntry, haier_object: api.Haier, store: Store,
                   until_synced: bool) -> None:
    """Read the real state of the devices, then save their descriptions.

    until_synced: after a start from the saved descriptions keep retrying
    (with a growing pause) until every device has been read; a periodic
    refresh tries once. A device the app did not list before, or one the
    cloud now describes differently, reloads the integration.
    """
    async def rebuild(reason: str) -> None:
        # the saved list is out of date: the next start reads everything from the cloud
        _LOGGER.info(f"Haier Evo: {reason}, reloading")
        await store.async_remove()
        hass.config_entries.async_schedule_reload(entry.entry_id)

    delay = CACHE_RETRY_FIRST
    result = None
    try:
        while not haier_object.disconnect_requested:
            try:
                result = await hass.async_add_executor_job(haier_object.refresh_devices)
            except Exception as e:
                if isinstance(e, (api.ReauthRequired, api.AuthUserError)):
                    raise
                _LOGGER.warning(f"Haier Evo: reading the devices failed: {e!r}")
                result = None
            if result is not None and not result["failed"]:
                break
            if result is not None:
                # a device the app no longer lists will never answer: drop it
                listed = await hass.async_add_executor_job(haier_object.list_device_ids)
                if listed is not None and set(result["failed"]) - listed:
                    await rebuild("a device was removed in the app")
                    return
            if not until_synced:
                break
            _LOGGER.info(f"Haier Evo: no fresh state yet for {len(result['failed']) if result else 'all'} "
                         f"device(s), next try in {delay} s")
            await asyncio.sleep(delay)
            delay = min(delay * 2, CACHE_RETRY_MAX)
        if haier_object.disconnect_requested or result is None:
            return
        listed = await hass.async_add_executor_job(haier_object.list_device_ids)
    except (api.ReauthRequired, api.AuthUserError) as e:
        _LOGGER.error(f"Haier Evo rejected the saved sign-in, re-authentication is needed: {e!r}")
        await _stop(hass, haier_object)
        entry.async_start_reauth(hass)
        return
    known = {d.device_id.lower() for d in haier_object.devices}
    if listed is not None and (new := listed - known):
        try:
            # a device skipped at the start because the cloud refuses it
            # (HTTP 403/404) is not new: rebuild once the cloud describes it
            denied = await hass.async_add_executor_job(haier_object.denied_devices, new)
        except (api.ReauthRequired, api.AuthUserError) as e:
            _LOGGER.error(f"Haier Evo rejected the saved sign-in, re-authentication is needed: {e!r}")
            await _stop(hass, haier_object)
            entry.async_start_reauth(hass)
            return
        if new - denied:
            await rebuild(f"the app lists {len(new - denied)} new device(s)")
            return
    await _save_devices(hass, store, haier_object)
    if result["changed"]:
        hass.config_entries.async_schedule_reload(entry.entry_id)


async def _refresh_state(hass: HomeAssistant, entry: ConfigEntry, haier_object: api.Haier, store: Store) -> None:
    """Read the state of every device again (between the full refreshes).

    Only the values: the device list and the saved descriptions are left to
    _refresh, unless the cloud now describes a device differently.
    """
    if haier_object.disconnect_requested:
        return
    try:
        result = await hass.async_add_executor_job(haier_object.refresh_devices)
    except (api.ReauthRequired, api.AuthUserError) as e:
        _LOGGER.error(f"Haier Evo rejected the saved sign-in, re-authentication is needed: {e!r}")
        await _stop(hass, haier_object)
        entry.async_start_reauth(hass)
        return
    except Exception as e:
        _LOGGER.debug(f"Haier Evo: reading the state failed: {e!r}")
        return
    if result["changed"]:
        await _save_devices(hass, store, haier_object)
        hass.config_entries.async_schedule_reload(entry.entry_id)


def _token_saver(hass: HomeAssistant, store: Store):
    """Save rotated tokens from any thread, waiting for the write when possible."""
    def save(tokens: dict) -> None:
        if threading.get_ident() == hass.loop_thread_id:
            hass.async_create_task(store.async_save(tokens))
            return
        asyncio.run_coroutine_threadsafe(store.async_save(tokens), hass.loop).result(timeout=30)
    return save


def _only_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return all(e.entry_id == entry.entry_id for e in hass.config_entries.async_entries(DOMAIN))


def _read_legacy_tokens(path: str) -> dict | None:
    import json
    try:
        with open(path, "r") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:
        _LOGGER.warning(f"Failed to read the old tokens file: {e!r}")
        return None
    return data if isinstance(data, dict) and data.get("refreshtoken") else None


def _remove_legacy_tokens(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except Exception as e:
        _LOGGER.warning(f"Failed to remove the old tokens file {path}: {e!r}")


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    integration = await async_get_integration(hass, DOMAIN)
    _LOGGER.debug(f'Integration version: {integration.version}')
    email = entry.data.get("email") or ""
    region = entry.data.get("region") or "ru"
    store = _token_store(hass, entry.entry_id)
    legacy_path = hass.config.path(LEGACY_TOKENS_FILE)

    tokens = await store.async_load()
    if isinstance(entry.data.get("tokens"), dict):
        # handed over by the config flow / re-authentication
        tokens = entry.data["tokens"]
        await store.async_save(tokens)
        hass.config_entries.async_update_entry(
            entry, data={k: v for k, v in entry.data.items() if k != "tokens"}
        )
    if not tokens and _only_entry(hass, entry):
        # up to 1.1.x the tokens of all entries lived in /config/haier_evo
        tokens = await hass.async_add_executor_job(_read_legacy_tokens, legacy_path)
        if tokens:
            await store.async_save(tokens)
            _LOGGER.info("Moved the Haier Evo tokens to Home Assistant storage")

    if entry.unique_id is None and email:
        unique_id = f"{email.lower()}_{region}"
        if not any(e.unique_id == unique_id for e in hass.config_entries.async_entries(DOMAIN)):
            hass.config_entries.async_update_entry(entry, unique_id=unique_id)

    haier_object = api.Haier(
        hass,
        email,
        entry.data.get("password"),  # only entries created before 1.2.0 have it
        region,
        tokens=tokens,
        token_saver=_token_saver(hass, store),
        on_auth_failed=lambda: hass.loop.call_soon_threadsafe(entry.async_start_reauth, hass),
        device_id=api.entry_device_id(entry.entry_id),
    )
    devices_store = _devices_store(hass, entry.entry_id)
    cached = _cached_devices(await devices_store.async_load())
    from_cache = False
    if cached:
        # start at once from the last good start; the real state is read in the background
        try:
            await hass.async_add_executor_job(haier_object.load_cached, cached)
            from_cache = True
        except Exception as e:
            _LOGGER.warning(f"Saved device descriptions are not usable ({e!r}), reading them from the cloud")
            haier_object.devices.clear()
            await devices_store.async_remove()

    if not from_cache:
        try:
            await hass.async_add_executor_job(haier_object.pull_data)
        except (api.ReauthRequired, api.AuthUserError) as e:
            await _stop(hass, haier_object)
            raise ConfigEntryAuthFailed(f"Haier Evo needs signing in again: {e!r}") from e
        except Exception as e:
            await _stop(hass, haier_object)
            # network problems, cloud errors, timeouts: let Home Assistant retry the setup
            raise ConfigEntryNotReady(f"Haier Evo is not available: {e!r}") from e

        found = {str(device.device_id).lower() for device in haier_object.devices}
        missing = _known_device_ids(hass, entry) - found
        if not found:
            await _stop(hass, haier_object)
            raise ConfigEntryNotReady("Haier Evo returned an empty device list")
        if missing:
            attempts = _INCOMPLETE_SETUPS.get(entry.entry_id, 0) + 1
            if attempts <= SETUP_INCOMPLETE_RETRIES:
                _INCOMPLETE_SETUPS[entry.entry_id] = attempts
                await _stop(hass, haier_object)
                raise ConfigEntryNotReady(
                    f"Haier Evo did not return {len(missing)} known device(s): "
                    f"{', '.join(sorted(missing))} (attempt {attempts}/{SETUP_INCOMPLETE_RETRIES})"
                )
            _LOGGER.warning(
                f"Haier Evo still does not return {', '.join(sorted(missing))}, "
                f"starting with the {len(found)} device(s) it returned"
            )
        _INCOMPLETE_SETUPS.pop(entry.entry_id, None)
        await _save_devices(hass, devices_store, haier_object)

    # the cloud accepted the saved tokens: the password is not kept any more
    if not from_cache and "password" in entry.data and haier_object.refreshtoken:
        hass.config_entries.async_update_entry(
            entry, data={k: v for k, v in entry.data.items() if k != "password"}
        )
        _LOGGER.info("Haier Evo password removed from the configuration, tokens are used instead")
    if _only_entry(hass, entry):
        await hass.async_add_executor_job(_remove_legacy_tokens, legacy_path)

    registry = dr.async_get(hass)
    if http_device := _find_device(registry, _HTTP_DEVICE, entry.entry_id):
        registry.async_remove_device(http_device.id)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = haier_object
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    if from_cache:
        haier_object.connect_in_thread()
        entry.async_create_background_task(
            hass, _refresh(hass, entry, haier_object, devices_store, until_synced=True),
            f"{DOMAIN} initial refresh",
        )

    async def periodic(_now) -> None:
        await _refresh(hass, entry, haier_object, devices_store, until_synced=False)

    entry.async_on_unload(async_track_time_interval(
        hass, periodic, timedelta(hours=CACHE_REFRESH_HOURS), cancel_on_shutdown=True,
    ))

    async def state(_now) -> None:
        await _refresh_state(hass, entry, haier_object, devices_store)

    entry.async_on_unload(async_track_time_interval(
        hass, state, timedelta(minutes=STATE_REFRESH_MINUTES), cancel_on_shutdown=True,
    ))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        haier_object = hass.data[DOMAIN].pop(entry.entry_id)
        _LOGGER.debug(f'Integration {haier_object} unload...')
        await _stop(hass, haier_object)
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Deleting the integration also deletes its tokens and saved device descriptions."""
    await _token_store(hass, entry.entry_id).async_remove()
    await _devices_store(hass, entry.entry_id).async_remove()
    if not hass.config_entries.async_entries(DOMAIN):
        await hass.async_add_executor_job(_remove_legacy_tokens, hass.config.path(LEGACY_TOKENS_FILE))
