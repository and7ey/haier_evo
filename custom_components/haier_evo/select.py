import weakref
from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from .const import DOMAIN
from . import api


async def async_setup_entry(hass: HomeAssistant, config_entry, async_add_entities) -> bool:
    haier_object = hass.data[DOMAIN][config_entry.entry_id]
    entities = []
    for device in haier_object.devices:
        entities.extend(device.create_entities_select())
    if entities:
        async_add_entities(entities)
        haier_object.write_ha_state()
    return True


class HaierSelect(SelectEntity):
    _attr_should_poll = False
    _attr_icon = "mdi:format-list-bulleted"

    def __init__(self, device: api.HaierDevice) -> None:
        self._device = weakref.proxy(device)
        self._device_attr_name = None
        self._attr_options = []

        device.add_write_ha_state_callback(self.async_write_ha_state)

    @property
    def device_info(self) -> dict:
        return self._device.device_info

    @property
    def available(self) -> bool:
        return self._device.available

    @property
    def current_option(self) -> str:
        return getattr(self._device, self._device_attr_name, None)

    async def async_select_option(self, option: str) -> None:
        await self.hass.async_add_executor_job(self.set_option, option)

    def set_option(self, value) -> None:
        method = getattr(self._device, f"set_{self._device_attr_name}", None)
        if method is not None:
            method(value)


class HaierACEcoSensorSelect(HaierSelect):
    _attr_translation_key = "conditioner_eco_sensor"

    def __init__(self, device: api.HaierAC) -> None:
        super().__init__(device)
        self._device_attr_name = "eco_sensor"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_eco_sensor"
        self._attr_name = f"{device.device_name} Эко-датчик"
        self._attr_options = device.get_eco_sensor_options()


class HaierREFFridgeModeSelect(HaierSelect):

    def __init__(self, device: api.HaierREF) -> None:
        super().__init__(device)
        self._device_attr_name = "fridge_mode"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_fridge_mode_select"
        self._attr_name = f"{device.device_name} Режим холодильной камеры"
        self._attr_options = device.get_fridge_mode_options()


class HaierREFFreezerModeSelect(HaierSelect):

    def __init__(self, device: api.HaierREF) -> None:
        super().__init__(device)
        self._device_attr_name = "freezer_mode"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_freezer_mode_select"
        self._attr_name = f"{device.device_name} Режим морозильной камеры"
        self._attr_options = device.get_freezer_mode_options()


class HaierREFMyZoneSelect(HaierSelect):

    def __init__(self, device: api.HaierREF) -> None:
        super().__init__(device)
        self._device_attr_name = "my_zone"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_my_zone"
        self._attr_name = f"{device.device_name} My Zone"
        self._attr_options = device.get_my_zone_options()


class HaierWMProgramSelect(HaierSelect):

    def __init__(self, device: api.HaierWM) -> None:
        super().__init__(device)
        self._device_attr_name = "program"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_program"
        self._attr_name = f"{device.device_name} Программа"
        self._attr_options = device.get_program_options()


class HaierWMTemperatureSelect(HaierSelect):

    def __init__(self, device: api.HaierWM) -> None:
        super().__init__(device)
        self._device_attr_name = "temperature"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_temperature"
        self._attr_name = f"{device.device_name} Температура"
        self._attr_options = device.get_temperature_options()


class HaierWMSpinSpeedSelect(HaierSelect):

    def __init__(self, device: api.HaierWM) -> None:
        super().__init__(device)
        self._device_attr_name = "spin_speed"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_spin_speed"
        self._attr_name = f"{device.device_name} Скорость отжима"
        self._attr_options = device.get_spin_speed_options()


class HaierLaundryProgramSelect(HaierSelect):
    _attr_has_entity_name = True
    _attr_translation_key = "laundry_program_selection"
    _attr_icon = None

    def __init__(self, device: api.HaierLaundry) -> None:
        super().__init__(device)
        self._device_attr_name = "selected_program"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_selected_program"
        language = getattr(device.hass.config, "language", "en")
        self._unselected = "Не выбрана" if isinstance(language, str) and language.startswith("ru") else "Not selected"
        self._attr_options = [self._unselected, *device.programs]

    @property
    def current_option(self) -> str:
        return self._device.selected_program or self._unselected

    def set_option(self, value: str) -> None:
        self._device.set_selected_program(None if value == self._unselected else value)
