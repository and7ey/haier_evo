from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import Callable

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

from . import api
from .client import (
    STORAGE_VERSION,
    HaierAuthError,
    HaierClient,
    HaierError,
    HaierRateLimitError,
    read_tokens,
)
from .const import (
    CACHE_SAVE_DELAY,
    CACHE_VERSION,
    CONF_LAUNDRY_CONTROL,
    CONF_REGION,
    CONF_TOKENS,
    DOMAIN,
    SETUP_INCOMPLETE_RETRIES,
)
from .logger import _LOGGER, mask_macs

PLATFORMS: list[Platform] = [
    Platform.BUTTON,
    Platform.CLIMATE,
    Platform.SWITCH,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.WATER_HEATER,
]

type HaierConfigEntry = ConfigEntry[api.Haier]

_MAC = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
# The device of the debug switches, removed in 1.2.0.
_HTTP_DEVICE = (DOMAIN, f"{DOMAIN}_http_switch")
# entry_id -> setups in a row that got fewer devices than the registry knows
_INCOMPLETE_SETUPS: dict[str, int] = {}


def token_store(hass: HomeAssistant, entry_id: str) -> Store:
    """Tokens live in .storage, not in /config (they used to be /config/haier_evo)."""
    # atomic: a power cut in the middle of a write must not leave a broken file (1.2)
    return Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}", private=True, atomic_writes=True)


def devices_store(hass: HomeAssistant, entry_id: str) -> Store:
    """Device descriptions of the last good start (MACs, serials: private too).

    Written up to once a minute while pushes come, so the writes are atomic: a
    power cut in the middle of one must not leave a broken file for the very
    start that needs it.
    """
    return Store(
        hass, CACHE_VERSION, f"{DOMAIN}.{entry_id}.devices", private=True, atomic_writes=True
    )


def _known_macs(hass: HomeAssistant, entry: ConfigEntry) -> set[str]:
    """MACs of the Haier devices this entry created before."""
    registry = dr.async_get(hass)
    return {
        identifier.lower()
        for device in dr.async_entries_for_config_entry(registry, entry.entry_id)
        for domain, identifier in device.identifiers
        if domain == DOMAIN and _MAC.match(identifier.lower())
    }


def entry_device_id(entry_id: str) -> str:
    """Stable Device-Id per entry; 0.3.x sent a new random one on every start."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{DOMAIN}:{entry_id}"))


def _take_legacy_token_file(path: str) -> dict | None:
    """Versions up to 1.1 kept live tokens as plain JSON in /config/haier_evo.

    The file is removed; its tokens are returned for an entry that has none
    (with no password kept, they are the only way in without reauth).
    """
    try:
        with open(path, encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not {"token", "refreshtoken", "refresh_token"} & data.keys():
        return None
    try:
        os.remove(path)
    except OSError as err:
        # read-only or no rights: the tokens are still good, the setup goes on
        _LOGGER.warning("Failed to remove the old Haier Evo token file: %s", err)
    else:
        _LOGGER.info("Removed the token file of the old integration")
    return data if read_tokens(data)["refresh_token"] else None


async def _async_take_handed_tokens(
    hass: HomeAssistant, entry: ConfigEntry, client: HaierClient
) -> None:
    """Tokens come to the entry once, then live in .storage only.

    The config flow hands the tokens of its sign-in over in the entry data
    (no second sign-in right after it: the endpoint is quick to answer 429,
    #87); they are moved to the token store and dropped from the
    entry. Entries of versions up to 1.1 had them in /config/haier_evo.
    """
    tokens = entry.data.get(CONF_TOKENS)
    if isinstance(tokens, dict):
        await client.async_import_tokens(tokens)
        hass.config_entries.async_update_entry(
            entry, data={k: v for k, v in entry.data.items() if k != CONF_TOKENS}
        )
    if len(hass.config_entries.async_entries(DOMAIN)) != 1:
        # The file had no owner: with more entries there is no telling whose
        # it is, so it is neither used nor deleted (as in 1.7).
        return
    old = await hass.async_add_executor_job(_take_legacy_token_file, hass.config.path(DOMAIN))
    if old and not client.has_refresh_token:
        await client.async_import_tokens(old)
        _LOGGER.info("Moved the Haier Evo tokens to Home Assistant storage")


def _forget_password(hass: HomeAssistant, entry: ConfigEntry, client: HaierClient) -> None:
    """The password is not kept once the cloud took the saved tokens.

    Entries of versions up to 1.1 and of the temandroid/haier_evo_2 fork (0.x)
    have it in the entry data (as plain text in core.config_entries). A refresh token the cloud rejects
    later asks the user for it (reauth), as 1.2+ does.
    """
    if CONF_PASSWORD in entry.data and client.has_refresh_token:
        hass.config_entries.async_update_entry(
            entry, data={k: v for k, v in entry.data.items() if k != CONF_PASSWORD}
        )
        _LOGGER.info("Haier Evo password removed from the configuration, the saved sign-in is used instead")


async def async_setup_entry(hass: HomeAssistant, entry: HaierConfigEntry) -> bool:
    if entry.unique_id is None:
        # Entries made by earlier versions have no unique_id.
        unique_id = f"{str(entry.data[CONF_EMAIL]).strip().lower()}_{entry.data.get(CONF_REGION) or 'ru'}"
        if hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, unique_id) is None:
            hass.config_entries.async_update_entry(entry, unique_id=unique_id)
        else:
            _LOGGER.warning("Another Haier Evo entry already uses this account, leaving this one as is")
    session = async_get_clientsession(hass)
    client = HaierClient(
        session,
        entry.data[CONF_EMAIL],
        entry.data.get(CONF_PASSWORD),  # only entries of earlier versions have it
        entry.data.get(CONF_REGION) or "ru",
        store=token_store(hass, entry.entry_id),
        device_id=entry_device_id(entry.entry_id),
    )
    await client.async_load_tokens()
    await _async_take_handed_tokens(hass, entry, client)
    haier = api.Haier(hass, client, session)
    # the control of washers and dryers (beta) is an option of the entry, off by default
    haier.laundry_control = bool(entry.options.get(CONF_LAUNDRY_CONTROL, False))
    cache = devices_store(hass, entry.entry_id)
    # As 1.4+: a start builds the devices from the descriptions of the last
    # good one at once, and the real state is read in the background (an HA
    # start does not wait for a slow cloud). A reload that is there because
    # those descriptions are out of date reads the cloud.
    from_cloud = entry.entry_id in api.RELOAD_FROM_CLOUD
    api.RELOAD_FROM_CLOUD.discard(entry.entry_id)
    if from_cloud or not await _async_setup_from_cache(haier, cache, None):
        try:
            await haier.async_setup()
        except HaierAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except HaierError as err:
            # #105 (an empty list right after a restart), 429, network,
            # a token the cloud keeps refusing: the cloud is not usable right now.
            haier.sync_delayed = from_cloud
            if not from_cloud or not await _async_setup_from_cache(haier, cache, err):
                if isinstance(err, api.HaierEmptyDeviceList):
                    raise ConfigEntryNotReady("Haier Evo returned no devices, will retry") from err
                if isinstance(err, HaierRateLimitError):
                    raise ConfigEntryNotReady(str(err)) from err
                raise ConfigEntryNotReady(f"Haier Evo cloud is unavailable: {err}") from err
        else:
            await _async_check_complete(hass, entry, haier, cache)
            await cache.async_save(haier.cache_dict())
            _forget_password(hass, entry, client)
    haier.on_synced = lambda: _forget_password(hass, entry, client)

    haier.cache_store = cache
    haier.on_refreshed = _cache_saver(haier, cache)
    registry = dr.async_get(hass)
    if (http_device := registry.async_get_device(identifiers={_HTTP_DEVICE})) is not None and (
        entry.entry_id in http_device.config_entries
    ):
        registry.async_remove_device(http_device.id)
    entry.runtime_data = haier
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    haier.start(entry)
    _LOGGER.debug("Haier Evo set up with %d device(s)", len(haier.devices))
    # A boolean: Home Assistant up to 2026.10 requires one; 2026.11 takes it
    # from custom integrations as well (it wants None from core ones only).
    return True


async def _async_setup_from_cache(haier: api.Haier, cache: Store, err: Exception | None) -> bool:
    """Build the devices from the descriptions of the last good start.

    The entities come up at once instead of waiting for the cloud (or in
    "setup retry" when it does not answer: the power cut of 2026-11-04, the
    cloud came back later than HA); the WebSocket and a background read bring
    the real state. err: why the cloud was not used, None for a normal start.
    """
    try:
        cached = await cache.async_load()
    except Exception as load_err:  # noqa: BLE001 - a broken file is no reason to fail
        _LOGGER.warning("Saved Haier Evo device descriptions are not readable: %s", load_err)
        return False
    if cached is None:
        return False
    try:
        await haier.async_setup_from_cache(cached)
    except (ValueError, TypeError, KeyError, AttributeError) as build_err:
        _LOGGER.warning("Saved Haier Evo device descriptions are not usable: %s", build_err)
        await cache.async_remove()
        return False
    if err is None:
        _LOGGER.debug("Started %d device(s) from the last good start, reading the cloud", len(haier.devices))
    else:
        _LOGGER.warning(
            "Haier Evo cloud is not available (%s): started %d device(s) from the last "
            "good start, the real state follows once the cloud answers",
            err, len(haier.devices),
        )
    return True


def _cache_saver(haier: api.Haier, cache: Store) -> Callable[[], None]:
    """Schedules a write of the saved descriptions CACHE_SAVE_DELAY s from now.

    Called after a REST refresh and after every push. A call while a write is
    already scheduled does nothing: Store.async_delay_save restarts its timer
    on every call, and pushes coming more often than the delay would put the
    write off for ever.
    """
    pending = False

    def data() -> dict:
        nonlocal pending
        pending = False
        return haier.cache_dict()

    def schedule() -> None:
        nonlocal pending
        if pending:
            return
        pending = True
        cache.async_delay_save(data, CACHE_SAVE_DELAY)

    return schedule


async def _async_check_complete(
    hass: HomeAssistant, entry: ConfigEntry, haier: api.Haier, cache: Store
) -> None:
    """Fewer devices than the registry knows: retry the setup a few times.

    The cloud sometimes leaves a unit out of the list or does not describe it
    right after an outage; starting without it would make its entities
    disappear until the next restart.

    A unit that stays away is waited for once, not at every start: the ones a
    setup gave up on are kept with the saved descriptions.
    """
    found = {str(device.device_id).lower() for device in haier.devices}
    # A unit the cloud returned but this code could not read is not missing:
    # a retry reads the same description, and it is not gone from the account.
    missing = _known_macs(hass, entry) - found - haier.unreadable
    if not missing:
        _INCOMPLETE_SETUPS.pop(entry.entry_id, None)
        return
    try:
        given_up = api.saved_missing(await cache.async_load())
    except Exception:  # noqa: BLE001 - an unreadable file only costs the retries
        given_up = set()
    if missing <= given_up:
        _INCOMPLETE_SETUPS.pop(entry.entry_id, None)
        haier.missing = missing
        _LOGGER.info(
            "Haier Evo does not return %s, as at the last start; "
            "delete the device in Home Assistant if it is gone from the Evo app",
            ", ".join(sorted(missing)),
        )
        return
    attempts = _INCOMPLETE_SETUPS.get(entry.entry_id, 0) + 1
    if attempts <= SETUP_INCOMPLETE_RETRIES:
        _INCOMPLETE_SETUPS[entry.entry_id] = attempts
        raise ConfigEntryNotReady(mask_macs(
            f"Haier Evo did not return {len(missing)} known device(s): {', '.join(sorted(missing))} "
            f"(attempt {attempts}/{SETUP_INCOMPLETE_RETRIES})"
        ))
    _INCOMPLETE_SETUPS.pop(entry.entry_id, None)
    haier.missing = missing
    _LOGGER.warning(
        "Haier Evo still does not return %s, starting with the %d device(s) it returned; "
        "delete the device in Home Assistant (its page, the menu, Delete) if it is gone from the Evo app",
        ", ".join(sorted(missing)), len(found),
    )


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: HaierConfigEntry, device_entry: dr.DeviceEntry
) -> bool:
    """A device may be deleted in the UI once the cloud no longer returns it.

    Without this hook Home Assistant offers no way to delete a device, and a
    unit removed from the Evo account stayed in the registry for good.
    """
    haier = getattr(entry, "runtime_data", None)
    if entry.state is not ConfigEntryState.LOADED or haier is None:
        # Not loaded: there is no telling which units the cloud still has, and
        # deleting a live one loses its name, area and disabled entities.
        return False
    live = {str(device.device_id).lower() for device in haier.devices} | haier.unreadable
    return not any(
        domain == DOMAIN and str(identifier).lower() in live
        for domain, identifier in device_entry.identifiers
    )


async def async_unload_entry(hass: HomeAssistant, entry: HaierConfigEntry) -> bool:
    # A boolean, as async_setup_entry: async_unload_platforms_or_raise is 2026.11+.
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    haier = entry.runtime_data
    await haier.async_stop()
    if haier.cache_store is not None:
        # Written now: a delayed write left pending would outlive the entry
        # and could bring the file back after the integration is deleted.
        haier.on_refreshed = None
        await haier.cache_store.async_save(haier.cache_dict())
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await token_store(hass, entry.entry_id).async_remove()
    await devices_store(hass, entry.entry_id).async_remove()
    _INCOMPLETE_SETUPS.pop(entry.entry_id, None)
