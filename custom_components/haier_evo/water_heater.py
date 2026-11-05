import weakref
from homeassistant.components.water_heater import (
    WaterHeaterEntity,
    WaterHeaterEntityFeature,
    STATE_OFF,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from .const import DOMAIN
from .logger import _LOGGER
from . import api


async def async_setup_entry(hass: HomeAssistant, config_entry, async_add_entities) -> bool:
    haier_object = hass.data[DOMAIN][config_entry.entry_id]
    entities = []
    for device in haier_object.devices:
        entities.extend(device.create_entities_water_heater())
    if entities:
        async_add_entities(entities)
        haier_object.write_ha_state()
    return True


# https://developers.home-assistant.io/docs/core/entity/water-heater
class HaierWHEntity(WaterHeaterEntity):
    _attr_translation_key = "water_heater"
    _attr_should_poll = False
    _attr_icon = "mdi:water-boiler"
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = 1
    _attr_supported_features = (
        WaterHeaterEntityFeature.TARGET_TEMPERATURE |
        WaterHeaterEntityFeature.OPERATION_MODE |
        WaterHeaterEntityFeature.ON_OFF
    )

    def __init__(self, device: api.HaierWH) -> None:
        self._device = weakref.proxy(device)
        self._attr_unique_id = f"{device.device_id}_{device.device_model}"
        self._attr_name = device.device_name
        self._attr_operation_list = [STATE_OFF] + device.get_heating_modes()

        device.add_write_ha_state_callback(self.async_write_ha_state)

    @property
    def device_info(self) -> dict:
        return self._device.device_info

    @property
    def available(self) -> bool:
        return self._device.available

    @property
    def current_operation(self) -> str | None:
        if not self._device.status:
            return STATE_OFF
        return self._device.heating_mode

    @property
    def current_temperature(self) -> float | None:
        return self._device.current_temperature

    @property
    def target_temperature(self) -> float | None:
        return self._device.target_temperature

    @property
    def min_temp(self) -> float:
        return self._device.min_temperature

    @property
    def max_temp(self) -> float:
        return self._device.max_temperature

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "heating": self._device.heating,
            "sterilization": self._device.sterilization_on,
        }

    def set_temperature(self, **kwargs) -> None:
        if (temperature := kwargs.get(ATTR_TEMPERATURE)) is None:
            return
        _LOGGER.debug(f"Setting target temperature to {temperature}")
        self._device.set_temperature(temperature)

    def set_operation_mode(self, operation_mode: str) -> None:
        _LOGGER.debug(f"Setting operation mode to {operation_mode}")
        if operation_mode == STATE_OFF:
            self._device.switch_off()
        else:
            self._device.set_heating_mode(operation_mode)

    def turn_on(self, **kwargs) -> None:
        self._device.switch_on()

    def turn_off(self, **kwargs) -> None:
        self._device.switch_off()
