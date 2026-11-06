from __future__ import annotations
import asyncio
import os
import re
import threading
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.const import Platform
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.storage import Store
from homeassistant.loader import async_get_integration
from .logger import _LOGGER
from .const import DOMAIN, SETUP_INCOMPLETE_RETRIES, STORAGE_VERSION, LEGACY_TOKENS_FILE
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


def _token_store(hass: HomeAssistant, entry_id: str) -> Store:
    # private: 0600, atomic: a crash never leaves a half-written token file
    return Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}", private=True, atomic_writes=True)


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
    )
    try:
        await hass.async_add_executor_job(haier_object.pull_data)
    except (api.ReauthRequired, api.AuthUserError) as e:
        haier_object.stop()
        raise ConfigEntryAuthFailed(f"Haier Evo needs signing in again: {e!r}") from e
    except Exception as e:
        haier_object.stop()
        # network problems, cloud errors, timeouts: let Home Assistant retry the setup
        raise ConfigEntryNotReady(f"Haier Evo is not available: {e!r}") from e

    found = {str(device.device_id).lower() for device in haier_object.devices}
    missing = _known_device_ids(hass, entry) - found
    if not found:
        haier_object.stop()
        raise ConfigEntryNotReady("Haier Evo returned an empty device list")
    if missing:
        attempts = _INCOMPLETE_SETUPS.get(entry.entry_id, 0) + 1
        if attempts <= SETUP_INCOMPLETE_RETRIES:
            _INCOMPLETE_SETUPS[entry.entry_id] = attempts
            haier_object.stop()
            raise ConfigEntryNotReady(
                f"Haier Evo did not return {len(missing)} known device(s): "
                f"{', '.join(sorted(missing))} (attempt {attempts}/{SETUP_INCOMPLETE_RETRIES})"
            )
        _LOGGER.warning(
            f"Haier Evo still does not return {', '.join(sorted(missing))}, "
            f"starting with the {len(found)} device(s) it returned"
        )
    _INCOMPLETE_SETUPS.pop(entry.entry_id, None)

    # the cloud accepted the saved tokens: the password is not kept any more
    if "password" in entry.data and haier_object.refreshtoken:
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
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        haier_object = hass.data[DOMAIN].pop(entry.entry_id)
        _LOGGER.debug(f'Integration {haier_object} unload...')
        haier_object.stop()
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Deleting the integration also deletes its tokens."""
    await _token_store(hass, entry.entry_id).async_remove()
    if not hass.config_entries.async_entries(DOMAIN):
        await hass.async_add_executor_job(_remove_legacy_tokens, hass.config.path(LEGACY_TOKENS_FILE))
