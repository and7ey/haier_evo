import weakref
from homeassistant.components.climate import ClimateEntity
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.components.climate.const import HVACMode
from .const import DOMAIN
from .logger import _LOGGER
from . import api


async def async_setup_entry(hass: HomeAssistant, config_entry, async_add_entities) -> bool:
    haier_object = hass.data[DOMAIN][config_entry.entry_id]
    entities = []
    for device in haier_object.devices:
        climate = HaierACEntity(device)
        entities.append(climate)
    async_add_entities(entities)
    haier_object.write_ha_state()
    return True


# https://developers.home-assistant.io/docs/core/entity/climate
class HaierACEntity(ClimateEntity):

    _attr_translation_key = "conditioner"
    _attr_should_poll = False

    def __init__(self, device: api.HaierAC) -> None:
        self._device = weakref.proxy(device)
        self._attr_unique_id = f"{device.device_id}_{device.device_model}"
        self._attr_name = device.device_name
        self._attr_supported_features = device.get_supported_features()
        self._attr_hvac_modes = device.get_hvac_modes()
        self._attr_fan_modes = device.get_fan_modes()
        self._attr_swing_horizontal_modes = device.get_swing_horizontal_modes()
        self._attr_swing_modes = device.get_swing_modes()
        self._attr_preset_modes = device.get_preset_modes()
        # https://developers.home-assistant.io/blog/2024/01/24/climate-climateentityfeatures-expanded/
        self._enable_turn_on_off_backwards_compatibility = False

        device.add_write_ha_state_callback(self.async_write_ha_state)

    @property
    def icon(self) -> str:
        return "mdi:air-conditioner"

    @property
    def hvac_mode(self) -> str:
        """Return hvac operation ie. heat, cool mode."""
        if self._device.status == 1:
            return self._device.mode or HVACMode.OFF
        return HVACMode.OFF

    @property
    def swing_horizontal_mode(self) -> str:
        return self._device.swing_horizontal_mode

    @property
    def swing_mode(self) -> str:
        return self._device.swing_mode

    @property
    def preset_mode(self) -> str:
        return self._device.preset_mode

    @property
    def fan_mode(self) -> str:
        return self._device.fan_mode

    @property
    def temperature_unit(self) -> str:
        return UnitOfTemperature.CELSIUS

    @property
    def max_temp(self) -> float:
        return self._device.max_temperature

    @property
    def min_temp(self) -> float:
        return self._device.min_temperature

    @property
    def current_temperature(self) -> float:
        return self._device.current_temperature

    @property
    def target_temperature(self) -> float:
        return self._device.target_temperature

    @property
    def target_temperature_step(self) -> float:
        return 1.0

    @property
    def available(self) -> bool:
        return self._device.available

    @property
    def device_info(self) -> dict:
        return self._device.device_info

    async def async_set_temperature(self, **kwargs) -> None:
        """Set new target temperature."""
        temp = kwargs.get("temperature", self.target_temperature)
        await self.hass.async_add_executor_job(self.set_temperature, temp)

    def set_temperature(self, temp: float) -> None:
        """Set new target temperature."""
        _LOGGER.debug(f"Setting target temperature to {temp}")
        self._device.set_temperature(temp)

    def set_hvac_mode(self, hvac_mode: str) -> None:
        """Set new target hvac mode."""
        _LOGGER.debug(f"Setting HVAC mode to {hvac_mode}")
        if hvac_mode == HVACMode.OFF:
            self._device.switch_off()
        else:
            self._device.switch_on(hvac_mode)

    def set_fan_mode(self, fan_mode: str) -> None:
        """Set new target fan mode."""
        _LOGGER.debug(f"Setting fan mode to {fan_mode}")
        self._device.set_fan_mode(fan_mode)

    def set_swing_horizontal_mode(self, swing_mode: str) -> None:
        """Set new target swing horizontal mode."""
        _LOGGER.debug(f"Setting swing horizontal mode to {swing_mode}")
        self._device.set_swing_horizontal_mode(swing_mode)

    def set_swing_mode(self, swing_mode: str) -> None:
        """Set new target swing operation."""
        _LOGGER.debug(f"Setting swing mode to {swing_mode}")
        self._device.set_swing_mode(swing_mode)

    def set_preset_mode(self, preset_mode: str) -> None:
        """Set new preset mode."""
        _LOGGER.debug(f"Setting preset mode to {preset_mode}")
        self._device.set_preset_mode(preset_mode)

    def turn_on(self) -> None:
        self._device.switch_on()

    def turn_off(self) -> None:
        self._device.switch_off()
