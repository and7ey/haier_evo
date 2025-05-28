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
    async_add_entities(entities)
    haier_object.write_ha_state()
    return True


class HaierSelect(SelectEntity):
    _attr_icon = "mdi:format-list-bulleted"

    def __init__(self, device: api.HaierDevice) -> None:
        self._device = weakref.proxy(device)
        self._attr_options = []

        device.add_write_ha_state_callback(self.async_write_ha_state)

    @property
    def device_info(self) -> dict:
        return self._device.device_info

    @property
    def available(self) -> bool:
        return self._device.available

    async def async_select_option(self, option: str) -> None:
        if option not in self.options:
            raise ValueError(f"{option} is not a valid option")
        await self.hass.async_add_executor_job(self.set_option, option)
        self.async_write_ha_state()

    def set_option(self, value) -> None:
        pass


class HaierACEcoSensorSelect(HaierSelect):
    _attr_translation_key = "conditioner_eco_sensor"

    def __init__(self, device: api.HaierAC) -> None:
        super().__init__(device)
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_eco_sensor"
        self._attr_name = f"{device.device_name} Эко-датчик"
        self._attr_options = device.get_eco_sensor_options()

    @property
    def current_option(self) -> str:
        return self._device.eco_sensor

    def set_option(self, value) -> None:
        self._device.set_eco_sensor(value)


class HaierREFFridgeModeSelect(HaierSelect):

    def __init__(self, device: api.HaierREF) -> None:
        super().__init__(device)
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_fridge_mode_select"
        self._attr_name = f"{device.device_name} Режим холодильной камеры"
        self._attr_options = device.get_fridge_mode_options()

    @property
    def current_option(self) -> str:
        return self._device.fridge_mode

    def set_option(self, value) -> None:
        self._device.set_fridge_mode(value)


class HaierREFFreezerModeSelect(HaierSelect):

    def __init__(self, device: api.HaierREF) -> None:
        super().__init__(device)
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_freezer_mode_select"
        self._attr_name = f"{device.device_name} Режим морозильной камеры"
        self._attr_options = device.get_freezer_mode_options()

    @property
    def current_option(self) -> str:
        return self._device.freezer_mode

    def set_option(self, value) -> None:
        self._device.set_freezer_mode(value)
