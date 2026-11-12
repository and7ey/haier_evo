"""Laundry entities and explicit actions for the Haier Evo domain."""
import sys
from datetime import timedelta

import voluptuous as vol
from homeassistant.helpers import entity_platform
from homeassistant.helpers.entity_platform import EntityPlatform

from .const import DOMAIN
from .device_laundry import LAUNDRY_ACTION_STATES, HaierLaundry
from .entity import HaierEntity
from .logger import _LOGGER


async def async_setup_entry(hass, entry, async_add_entities):
    cloud = entry.runtime_data
    async_add_entities([HaierLaundryEntity(device) for device in cloud.devices
                        if isinstance(device, HaierLaundry)])
    entity_platform.async_get_current_platform().async_register_entity_service(
        "execute_action", {vol.Required("action"): vol.In(LAUNDRY_ACTION_STATES)},
        "async_execute_action",
    )
    entity_platform.async_get_current_platform().async_register_entity_service(
        "configure_program", {vol.Required("program"): str, vol.Required("parameters"): dict},
        "async_configure_program",
    )


async def async_setup_machines(hass, entry):
    platform = EntityPlatform(
        hass=hass, logger=_LOGGER, domain=DOMAIN, platform_name=DOMAIN,
        platform=sys.modules[__name__],
        scan_interval=timedelta(seconds=30), entity_namespace=None,
    )
    platform.async_prepare()
    try:
        if not await platform.async_setup_entry(entry):
            raise RuntimeError("Could not set up Haier laundry entities")
    except BaseException:
        await platform.async_destroy()
        raise
    return platform


class HaierLaundryEntity(HaierEntity):
    _unrecorded_attributes = frozenset({"program_catalog", "program_settings", "active_program_details"})
    _attr_has_entity_name = True
    _attr_name = None
    _attr_should_poll = False
    _attr_translation_key = "laundry_machine"

    def __init__(self, device):
        super().__init__(device)
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_machine"
        self._attr_icon = "mdi:tumble-dryer" if device.is_dryer else "mdi:washing-machine"

    async def async_added_to_hass(self):
        self._device.machine_entity_id = self.entity_id
        await super().async_added_to_hass()

    async def async_will_remove_from_hass(self):
        self._device.machine_entity_id = None

    @property
    def device_info(self):
        return self._device.device_info

    @property
    def available(self):
        return self._device.available

    @property
    def state(self):
        return self._device.state or "not_reported"

    @property
    def extra_state_attributes(self):
        return {
            "machine_kind": "dryer" if self._device.is_dryer else "washer",
            "laundry_ready": self._device.laundry_ready,
            "care_name": self._device.postcare_name if self._device.in_postcare else None,
            "available_actions": self._device.available_actions,
            "control_enabled": self._device.control_enabled,
            "connection_status": "ONLINE" if self._device.connected else "OFFLINE",
            "program_catalog": self._device.program_catalog,
            "program_settings": self._device.program_settings,
            "active_program_details": self._device.active_program_details,
        }

    async def async_execute_action(self, action):
        await self._device.send_laundry_action(action)

    async def async_configure_program(self, program, parameters):
        self._device.configure_program(program, parameters)
