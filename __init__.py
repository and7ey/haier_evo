from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
import asyncio
from . import api
from .const import DOMAIN
import logging
_LOGGER = logging.getLogger(__name__)
PLATFORMS: list[str] = ["climate"]

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    haier_object = api.Haier(hass, entry.data["email"], entry.data["password"])
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = haier_object
    await hass.async_add_executor_job(
             haier_object.pull_data
         )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok
