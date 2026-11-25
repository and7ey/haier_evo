"""Temperature adjustment during an oven program."""
from __future__ import annotations

from homeassistant.components.number import NumberDeviceClass, NumberEntity
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant

from .device_ov import HaierOV
from .entity import HaierEntity, async_setup_platform


async def async_setup_entry(hass: HomeAssistant, config_entry, async_add_entities) -> None:
    async_setup_platform(hass, config_entry, async_add_entities, "number")


class HaierOvenSetpoint(HaierEntity, NumberEntity):
    _device: HaierOV
    _attr_translation_key = "oven_setpoint"
    _attr_device_class = NumberDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(self, device: HaierOV) -> None:
        super().__init__(device)
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_setpoint"

    @property
    def available(self) -> bool:
        return self._device.can_control("target_temperature") and self._device.temperature_limits is not None

    @property
    def native_value(self) -> float | None:
        return self._device.target_temperature

    @property
    def native_min_value(self) -> float:
        limits = self._device.temperature_limits
        return limits[0] if limits else 0

    @property
    def native_max_value(self) -> float:
        limits = self._device.temperature_limits
        return limits[1] if limits else 300

    @property
    def native_step(self) -> float:
        limits = self._device.temperature_limits
        return limits[2] if limits else 1

    async def async_set_native_value(self, value: float) -> None:
        await self._device.set_temperature(value)
