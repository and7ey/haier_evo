"""Cloud-confirmed laundry events on Home Assistant's standard event platform."""
from __future__ import annotations

from typing import Any

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback

from .device_laundry import HaierLaundry
from .entity import HaierEntity
from .laundry_events import EVENT_TYPES


async def async_setup_entry(hass: HomeAssistant, config_entry, async_add_entities) -> None:
    async_add_entities(
        HaierLaundryEvent(device) for device in config_entry.runtime_data.devices if isinstance(device, HaierLaundry)
    )


class HaierLaundryEvent(HaierEntity, EventEntity):
    _device: HaierLaundry
    _attr_translation_key = "laundry"
    _attr_event_types = EVENT_TYPES

    def __init__(self, device: HaierLaundry) -> None:
        super().__init__(device)
        self._attr_unique_id = device.laundry_events.unique_id
        self._attr_icon = "mdi:tumble-dryer" if device.is_dryer else "mdi:washing-machine"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._device.laundry_events.add_listener(self._receive))

    @callback
    def _receive(self, data: dict[str, Any]) -> None:
        self._trigger_event(data["type"], data)
        self.async_write_ha_state()
