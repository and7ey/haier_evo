import weakref
from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from .const import DOMAIN
from . import api


async def async_setup_entry(hass: HomeAssistant, config_entry, async_add_entities) -> bool:
    haier_object = hass.data[DOMAIN][config_entry.entry_id]
    entities = []
    for device in haier_object.devices:
        if isinstance(device, api.HaierLaundry):
            entities.extend(device.create_entities_button())
    if entities:
        async_add_entities(entities)
        haier_object.write_ha_state()
    return True


class HaierLaundryButton(ButtonEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, device: api.HaierLaundry, action: str) -> None:
        self._device = weakref.proxy(device)
        self._action = action
        self._attr_translation_key = f"laundry_{action}"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_{action}"
        device.add_write_ha_state_callback(self.async_write_ha_state)

    @property
    def device_info(self) -> dict:
        return self._device.device_info

    @property
    def available(self) -> bool:
        return (self._device.available and self._device.remote_control is True
                and not self._device._start_pending
                and self._action in api.LAUNDRY_ACTION_STATES
                and (self._device.program_status is None
                     or self._device.program_status in api.LAUNDRY_ACTION_STATES[self._action])
                and (self._action != "start" or self._device.selected_program is not None))

    async def async_press(self) -> None:
        await self.hass.async_add_executor_job(self._device.send_laundry_action, self._action)
