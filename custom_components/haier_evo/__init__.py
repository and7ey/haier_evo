from __future__ import annotations
import re
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.const import Platform
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.loader import async_get_integration
from .logger import _LOGGER
from .const import DOMAIN, SETUP_INCOMPLETE_RETRIES
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


def _known_device_ids(hass: HomeAssistant, entry: ConfigEntry) -> set[str]:
    """MACs of the Haier devices this entry created before (not the service HTTP device)."""
    registry = dr.async_get(hass)
    return {
        identifier.lower()
        for device in dr.async_entries_for_config_entry(registry, entry.entry_id)
        for domain, identifier in device.identifiers
        if domain == DOMAIN and _MAC.match(identifier.lower())
    }


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    integration = await async_get_integration(hass, DOMAIN)
    _LOGGER.debug(f'Integration version: {integration.version}')
    username = entry.data.get("email") or ""
    password = entry.data.get("password") or ""
    region = entry.data.get("region") or "ru"
    haier_object = api.Haier(hass, username, password, region)
    try:
        await hass.async_add_executor_job(haier_object.load_tokens)
        await hass.async_add_executor_job(haier_object.pull_data)
    except Exception as e:
        # auth_login sets disconnect_requested when the cloud rejects the e-mail
        # or the password (read it before stop(), which sets it too)
        rejected = haier_object.disconnect_requested
        haier_object.stop()
        if rejected:
            raise ConfigEntryError(f"Haier Evo rejected the credentials: {e!r}") from e
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
