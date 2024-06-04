from homeassistant.components.climate import ClimateEntity, ClimateEntityDescription
from homeassistant.helpers.entity import Entity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.components.climate.const import (
    HVACMode,
    FAN_AUTO,
    FAN_HIGH,
    FAN_LOW,
    FAN_MEDIUM,
    ClimateEntityFeature
)


from .const import DOMAIN
from . import api
import logging
import time

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, config_entry, async_add_entities):
    haier_object = hass.data[DOMAIN][config_entry.entry_id]
    new_devices = []
    for d in haier_object.devices:
        new_devices.append(Haier_AC(d ,hass))
    async_add_entities(new_devices)


# https://developers.home-assistant.io/docs/core/entity/climate
class Haier_AC(ClimateEntity):
    def __init__(self, module: api.HaierAC, hass: HomeAssistant):

        self._module = module
        self._hass1:HomeAssistant = hass
        self._attr_unique_id = f"{self._module.get_device_id}_{self._module.model_name}"
        self._attr_name = self._module.get_device_name
        self._attr_hvac_modes = [HVACMode.AUTO, HVACMode.COOL, HVACMode.HEAT, HVACMode.FAN_ONLY, HVACMode.DRY, HVACMode.OFF]
        self._attr_supported_features = (ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.TURN_OFF | ClimateEntityFeature.TURN_ON | ClimateEntityFeature.FAN_MODE) # PRESET_MODE, SWING_MODE
        self._attr_fan_modes = [FAN_AUTO, FAN_LOW, FAN_MEDIUM, FAN_HIGH]
        self._enable_turn_on_off_backwards_compatibility = False # https://developers.home-assistant.io/blog/2024/01/24/climate-climateentityfeatures-expanded/
        # todo
        # support Presets: ECO and BOOST (26 - Тихий, 27 - Турбо)

    @property
    def icon(self):
        return "mdi:air-conditioner"

    async def async_set_temperature(self, **kwargs) -> None:
        temp = kwargs.get("temperature", self.target_temperature)
        self.target_temp = temp
        await self._hass1.async_add_executor_job(
            self.set_temperature,temp)

    def set_temperature(self, temp):
        self._module.setTemperature(temp)

    @property
    def target_temperature_step(self) -> float:
        """Return the supported step of target temperature."""
        return 1.0

    def turn_on(self):
        self._module.switchOn()

    def turn_off(self):
        self._module.switchOff()

    @property
    def hvac_mode(self) -> str:
        """Return hvac operation ie. heat, cool mode."""
        if self._module.get_status == 1:
            return self._module.get_mode
        return HVACMode.OFF

    def set_hvac_mode(self, hvac_mode: str) -> None:
        """Set new target hvac mode."""
        _LOGGER.debug(f"Setting HVAC mode to {hvac_mode}")
        if hvac_mode == HVACMode.OFF:
            self._module.switchOff()
        else:
            self._module.switchOn(hvac_mode)

    def set_fan_mode(self, fan_mode):
        """Set new target fan mode."""
        _LOGGER.debug(f"Setting fan mode to {fan_mode}")
        self._module.setFanMode(fan_mode)

    @property
    def fan_mode(self) -> str:
        return self._module.get_fan_mode


    def update(self) -> None:
        self._module.update()

    @property
    def temperature_unit(self) -> str:
        return UnitOfTemperature.CELSIUS

    @property
    def max_temp(self) -> str:
        return self._module.get_max_temperature

    @property
    def min_temp(self) -> str:
        return self._module.get_min_temperature


    @property
    def current_temperature(self) -> float:
        return self._module.get_current_temperature

    @property
    def target_temperature(self) -> float:
        return self._module.get_target_temperature



    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._module.get_device_id)},
            "name": self._module.get_device_name,
            "sw_version": self._module.get_sw_version,
            "model": self._module.model_name,
            "manufacturer": "Haier",
        }

