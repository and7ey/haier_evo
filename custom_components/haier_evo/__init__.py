from __future__ import annotations
from pathlib import Path

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.const import Platform
from homeassistant.loader import async_get_integration
from .logger import _LOGGER
from .const import DOMAIN
from . import api


PLATFORMS: list[str] = [
    Platform.CLIMATE,
    Platform.SWITCH,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    card_path = Path(__file__).parent / "haier-laundry-card.js"
    url = "/haier_evo_frontend/haier-laundry-card.js"
    await hass.http.async_register_static_paths([StaticPathConfig(url, str(card_path), False)])
    add_extra_js_url(hass, url)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    integration = await async_get_integration(hass, DOMAIN)
    _LOGGER.debug(f'Integration version: {integration.version}')
    username = entry.data.get("email") or ""
    password = entry.data.get("password") or ""
    region = entry.data.get("region") or "ru"
    haier_object = api.Haier(hass, username, password, region)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = haier_object
    await hass.async_add_executor_job(haier_object.load_tokens)
    await hass.async_add_executor_job(haier_object.pull_data)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        haier_object = hass.data[DOMAIN].pop(entry.entry_id)
        _LOGGER.debug(f'Integration {haier_object} unload...')
        haier_object.stop()
    return unload_ok
