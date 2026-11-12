from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant

from . import api
from .entity import HaierEntity, async_setup_platform


async def async_setup_entry(hass: HomeAssistant, config_entry, async_add_entities) -> None:
    async_setup_platform(hass, config_entry, async_add_entities, "select")


class HaierSelect(HaierEntity, SelectEntity):
    _attr_icon = "mdi:format-list-bulleted"

    def __init__(self, device: api.HaierDevice) -> None:
        super().__init__(device)
        self._attr_options: list[str] = []

    @property
    def current_option(self) -> str | None:
        value = getattr(self._device, self._device_attr_name, None)
        return value if value in self.options else None

    async def async_select_option(self, option: str) -> None:
        method = getattr(self._device, f"set_{self._device_attr_name}", None)
        if method is not None:
            await method(option)
            self.async_write_ha_state()


class HaierACEcoSensorSelect(HaierSelect):
    _attr_translation_key = "conditioner_eco_sensor"

    def __init__(self, device: api.HaierAC) -> None:
        super().__init__(device)
        self._device_attr_name = "eco_sensor"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_eco_sensor"
        self._attr_name = "Эко-датчик"
        self._attr_options = device.get_eco_sensor_options()


class HaierREFFridgeModeSelect(HaierSelect):

    def __init__(self, device: api.HaierREF) -> None:
        super().__init__(device)
        self._device_attr_name = "fridge_mode"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_fridge_mode_select"
        self._attr_name = "Режим холодильной камеры"
        self._attr_options = device.get_fridge_mode_options()


class HaierREFFreezerModeSelect(HaierSelect):

    def __init__(self, device: api.HaierREF) -> None:
        super().__init__(device)
        self._device_attr_name = "freezer_mode"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_freezer_mode_select"
        self._attr_name = "Режим морозильной камеры"
        self._attr_options = device.get_freezer_mode_options()


class HaierREFMyZoneSelect(HaierSelect):

    def __init__(self, device: api.HaierREF) -> None:
        super().__init__(device)
        self._device_attr_name = "my_zone"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_my_zone"
        self._attr_name = "My Zone"
        self._attr_options = device.get_my_zone_options()


class HaierWMProgramSelect(HaierSelect):

    def __init__(self, device: api.HaierWM) -> None:
        super().__init__(device)
        self._device_attr_name = "program"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_program"
        self._attr_name = "Программа"
        self._attr_options = device.get_program_options()


class HaierWMTemperatureSelect(HaierSelect):

    def __init__(self, device: api.HaierWM) -> None:
        super().__init__(device)
        self._device_attr_name = "temperature"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_temperature"
        self._attr_name = "Температура"
        self._attr_options = device.get_temperature_options()


class HaierWMSpinSpeedSelect(HaierSelect):

    def __init__(self, device: api.HaierWM) -> None:
        super().__init__(device)
        self._device_attr_name = "spin_speed"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_spin_speed"
        self._attr_name = "Скорость отжима"
        self._attr_options = device.get_spin_speed_options()


class HaierLaundryProgramSelect(HaierSelect):
    """The program the start button runs (beta, washers and dryers, 1.7).

    Choosing one sends nothing: the program starts only with the button.
    """

    _attr_icon = "mdi:washing-machine"

    def __init__(self, device: api.HaierDevice) -> None:
        super().__init__(device)
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_laundry_program"
        self._attr_name = "Программа для запуска"
        self._attr_options = [p["title"] for p in device.programs]

    @property
    def current_option(self) -> str | None:
        return self._device.selected_program

    async def async_select_option(self, option: str) -> None:
        self._device.set_selected_program(option)
        self.async_write_ha_state()
