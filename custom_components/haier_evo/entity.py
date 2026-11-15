"""What every entity of the integration shares."""
from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from . import const as C  # noqa: N812
from .logger import _LOGGER

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .device import HaierDevice


class HaierEntity(Entity):
    """One function of a Haier device, pushed by the device, never polled.

    The subscription starts when Home Assistant adds the entity and ends when
    it is removed: a disabled entity is never added and so never written to
    (#75).
    """

    _attr_should_poll = False
    # Names describe the function only; HA puts the device name in front.
    _attr_has_entity_name = True

    def __init__(self, device: HaierDevice) -> None:
        self._device = device
        self._device_attr_name: str | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._device.add_listener(self.async_write_ha_state))

    @property
    def device_info(self) -> DeviceInfo:
        return self._device.device_info

    @property
    def available(self) -> bool:
        return self._device.available


def async_setup_platform(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    platform: str,
) -> None:
    """Add the entities every device offers on a platform.

    Entities earlier versions created and the device no longer offers are
    removed from the registry, so they do not stay "unavailable" for ever.
    """
    registry = er.async_get(hass)
    entities = []
    for device in entry.runtime_data.devices:
        try:
            created = getattr(device, f"create_entities_{platform}")()
            retired = device.retired_unique_ids(platform)
        except Exception:  # noqa: BLE001 - one device's bug must not take the platform of the others down
            # Nothing is removed from the registry either: what the device
            # no longer offers is not known.
            _LOGGER.exception("Failed to create the %s entities of %s", platform, device.device_name)
            continue
        entities.extend(created)
        for unique_id in retired:
            if entity_id := registry.async_get_entity_id(platform, C.DOMAIN, unique_id):
                registry.async_remove(entity_id)
    if entities:
        async_add_entities(entities)
