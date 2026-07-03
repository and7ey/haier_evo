from __future__ import annotations

import weakref

from homeassistant.components.water_heater import WaterHeaterEntity, WaterHeaterEntityFeature
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from . import api


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry,
    async_add_entities,
) -> bool:
    haier_object = hass.data[DOMAIN][config_entry.entry_id]
    entities = []
    for device in haier_object.devices:
        entities.extend(device.create_entities_water_heater())
    if entities:
        async_add_entities(entities)
        haier_object.write_ha_state()
    return True


class HaierWHWaterHeaterEntity(WaterHeaterEntity):
    _attr_should_poll = False
    _attr_icon = "mdi:water-boiler"

    def __init__(self, device: api.HaierWH) -> None:
        self._device = weakref.proxy(device)
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_water_heater"
        self._attr_name = device.device_name
        self._attr_supported_features = (
            WaterHeaterEntityFeature.TARGET_TEMPERATURE
            | WaterHeaterEntityFeature.OPERATION_MODE
            | WaterHeaterEntityFeature.ON_OFF
        )
        self._attr_operation_list = device.get_operation_modes()
        device.add_write_ha_state_callback(self.async_write_ha_state)

    @property
    def device_info(self) -> dict:
        return self._device.device_info

    @property
    def available(self) -> bool:
        return self._device.available

    @property
    def temperature_unit(self) -> str:
        return UnitOfTemperature.CELSIUS

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
    def target_temperature_step(self) -> float:
        return 1.0

    @property
    def operation_list(self) -> list[str]:
        return self._device.get_operation_modes()

    @property
    def current_operation(self) -> str | None:
        if not self._device.status:
            return "off"
        return self._device.operation_mode

    async def async_set_temperature(self, **kwargs) -> None:
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return
        await self.hass.async_add_executor_job(
            self._device.set_temperature,
            temperature,
        )

    async def async_set_operation_mode(self, operation_mode: str) -> None:
        await self.hass.async_add_executor_job(
            self._device.set_operation_mode,
            operation_mode,
        )

    async def async_turn_on(self) -> None:
        await self.hass.async_add_executor_job(self._device.switch_on)

    async def async_turn_off(self) -> None:
        await self.hass.async_add_executor_job(self._device.switch_off)
