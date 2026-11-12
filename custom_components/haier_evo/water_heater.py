"""Water heaters (1.0): temperature, Eco / Max, on and off."""
from __future__ import annotations

from typing import Any

from homeassistant.components.water_heater import (
    STATE_OFF,
    WaterHeaterEntity,
    WaterHeaterEntityFeature,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant

from .device_wh import HaierWH
from .entity import HaierEntity, async_setup_platform


async def async_setup_entry(hass: HomeAssistant, config_entry, async_add_entities) -> None:
    async_setup_platform(hass, config_entry, async_add_entities, "water_heater")


# https://developers.home-assistant.io/docs/core/entity/water-heater
class HaierWHEntity(HaierEntity, WaterHeaterEntity):
    _attr_translation_key = "water_heater"
    _attr_icon = "mdi:water-boiler"
    # native_* for HA 2026.11, the old members for 2026.9-2026.10 (see climate.py)
    _attr_native_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = 1
    _attr_supported_features = (
        WaterHeaterEntityFeature.TARGET_TEMPERATURE
        | WaterHeaterEntityFeature.OPERATION_MODE
        | WaterHeaterEntityFeature.ON_OFF
    )

    def __init__(self, device: HaierWH) -> None:
        super().__init__(device)
        self._attr_unique_id = f"{device.device_id}_{device.device_model}"
        # The main entity of the device: its name is the device name.
        self._attr_name = None
        self._attr_operation_list = [STATE_OFF, *device.get_heating_modes()]

    @property
    def current_operation(self) -> str | None:
        if not self._device.status:
            return STATE_OFF
        return self._device.heating_mode

    @property
    def native_current_temperature(self) -> float | None:
        return self._device.current_temperature

    @property
    def native_target_temperature(self) -> float | None:
        return self._device.target_temperature

    # Home Assistant 2026.9-2026.10
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
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"heating": self._device.heating, "sterilization": self._device.sterilization_on}

    async def async_set_temperature(self, **kwargs: Any) -> None:
        if (temperature := kwargs.get(ATTR_TEMPERATURE)) is None:
            return
        await self._device.set_temperature(temperature)
        self.async_write_ha_state()

    async def async_set_operation_mode(self, operation_mode: str) -> None:
        if operation_mode == STATE_OFF:
            await self._device.switch_off()
        else:
            await self._device.set_heating_mode(operation_mode)
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._device.switch_on()
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._device.switch_off()
        self.async_write_ha_state()
