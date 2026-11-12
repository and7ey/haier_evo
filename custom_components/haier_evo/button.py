"""Buttons of a washer or dryer program: start, pause, resume, cancel (beta, 1.7).

They exist only with the entry's option "laundry_control" on. A press sends
one command, as the app does; what is allowed is decided by the machine
(device_laundry.HaierLaundry: remote control on, a state the app offers the
action in).
"""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant

from .device_laundry import HaierLaundry
from .entity import HaierEntity, async_setup_platform

# action: name, icon
LAUNDRY_BUTTONS = {
    "start": ("Старт", "mdi:play"),
    "pause": ("Пауза", "mdi:pause"),
    "resume": ("Продолжить", "mdi:play-pause"),
    "cancel": ("Отменить программу", "mdi:stop"),
}


async def async_setup_entry(hass: HomeAssistant, config_entry, async_add_entities) -> None:
    async_setup_platform(hass, config_entry, async_add_entities, "button")


class HaierLaundryButton(HaierEntity, ButtonEntity):

    def __init__(self, device: HaierLaundry, action: str) -> None:
        super().__init__(device)
        self._action = action
        name, icon = LAUNDRY_BUTTONS[action]
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_laundry_{action}"
        self._attr_name = name
        self._attr_icon = icon

    async def async_press(self) -> None:
        await getattr(self._device, self._action)()
