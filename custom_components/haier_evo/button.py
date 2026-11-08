"""Buttons of a washer or dryer program: start, pause, resume, cancel (beta).

They exist only with the entry's option "laundry_control" on. A press sends
one command, as the app does; what is allowed is decided by the device
(api.HaierLaundry: remote control on, a state the app offers the action in).
"""
import weakref
from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from .const import DOMAIN
from . import api
from .entity import HaierEntityMixin

# action: name, icon
LAUNDRY_BUTTONS = {
    "start": ("Старт", "mdi:play"),
    "pause": ("Пауза", "mdi:pause"),
    "resume": ("Продолжить", "mdi:play-pause"),
    "cancel": ("Отменить программу", "mdi:stop"),
}


async def async_setup_entry(hass: HomeAssistant, config_entry, async_add_entities) -> bool:
    haier_object = hass.data[DOMAIN][config_entry.entry_id]
    entities = []
    for device in haier_object.devices:
        entities.extend(device.create_entities_button())
    remove_control_entities(hass, haier_object.devices, "button", [f"laundry_{a}" for a in LAUNDRY_BUTTONS])
    if entities:
        async_add_entities(entities)
    return True


def remove_control_entities(hass: HomeAssistant, devices, platform: str, suffixes: list[str]) -> None:
    """With the control turned off, its entities leave the registry (no orphans)."""
    registry = er.async_get(hass)
    for device in devices:
        if isinstance(device, api.HaierLaundry) and not device.control_enabled:
            for suffix in suffixes:
                unique_id = f"{device.device_id}_{device.device_model}_{suffix}"
                if entity := registry.async_get_entity_id(platform, DOMAIN, unique_id):
                    registry.async_remove(entity)


class HaierLaundryButton(HaierEntityMixin, ButtonEntity):
    _attr_should_poll = False

    def __init__(self, device: api.HaierLaundry, action: str) -> None:
        self._device = weakref.proxy(device)
        self._action = action
        name, icon = LAUNDRY_BUTTONS[action]
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_laundry_{action}"
        self._attr_name = f"{device.device_name} {name}"
        self._attr_icon = icon

    @property
    def device_info(self) -> dict:
        return self._device.device_info

    @property
    def available(self) -> bool:
        return self._device.available

    def press(self) -> None:
        # in the executor: the command waits for the cloud's confirmation
        getattr(self._device, self._action)()
