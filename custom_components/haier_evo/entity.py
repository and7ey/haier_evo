"""What every entity of the integration shares."""
from __future__ import annotations

from typing import Any

from homeassistant.helpers.entity import Entity


class HaierEntityMixin(Entity):
    """Takes state updates of its device only while Home Assistant has it.

    A disabled entity is never added: up to 1.6.3 it subscribed when it was
    created, and every update of its device logged "Attribute hass is None"
    (upstream issue #75). The subscription now starts when the entity is
    added and ends when it is removed.
    """

    _device: Any  # a weak proxy of the api.HaierDevice

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._device.add_write_ha_state_callback(self.async_write_ha_state))
