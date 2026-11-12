from __future__ import annotations

from typing import Any

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import HVACMode
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import api
from .entity import HaierEntity, async_setup_platform
from .logger import _LOGGER


async def async_setup_entry(
    hass: HomeAssistant, config_entry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_setup_platform(hass, config_entry, async_add_entities, "climate")


# https://developers.home-assistant.io/docs/core/entity/climate
class HaierACEntity(HaierEntity, ClimateEntity):
    _device: api.HaierAC
    _attr_translation_key = "conditioner"
    _attr_icon = "mdi:air-conditioner"
    # HA 2026.11 reads the native_* members, 2026.9-2026.10 the old ones: both
    # are provided, and 2026.11 takes the native ones without a warning when a
    # class has both (homeassistant.helpers.deprecation.migrate_deprecated_entity_members).
    _attr_native_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_temperature_unit = UnitOfTemperature.CELSIUS

    def __init__(self, device: api.HaierAC) -> None:
        super().__init__(device)
        self._attr_unique_id = f"{device.device_id}_{device.device_model}"
        # The main entity of the device: its name is the device name.
        self._attr_name = None
        self._attr_supported_features = device.get_supported_features()
        self._attr_hvac_modes = device.get_hvac_modes()
        self._attr_fan_modes = device.get_fan_modes()
        self._attr_swing_horizontal_modes = device.get_swing_horizontal_modes()
        self._attr_swing_modes = device.get_swing_modes()
        self._attr_preset_modes = device.get_preset_modes()

    @staticmethod
    def _known(value: str | None, options: list[str] | None) -> str | None:
        """Only report values HA can validate; anything else is 'unknown'."""
        if value is None or not options or value not in options:
            return None
        return value

    @property
    def hvac_mode(self) -> HVACMode | None:
        """Return hvac operation ie. heat, cool mode."""
        if self._device.status is None:
            return None
        if self._device.status != 1:
            return HVACMode.OFF
        # #61/#73: a mode code missing from the profile produced the
        # string "None" here and HA raised "'None' is not a valid HVACMode".
        mode = self._device.mode
        if mode in self.hvac_modes:
            return HVACMode(mode)
        _LOGGER.debug("%s: unknown hvac mode %r", self._device.device_name, mode)
        return None

    @staticmethod
    def _with_current(options: list[str] | None, current: str | None) -> list[str] | None:
        """Options plus the current position when the unit sits in one the app does not offer.

        Set with the remote, as 1.7 showed it: "unknown" would hide where the
        louvers actually are.
        """
        if not options or not current or current in options:
            return options
        return [*options, current]

    @property
    def swing_horizontal_modes(self) -> list[str] | None:
        return self._with_current(self._attr_swing_horizontal_modes, self._device.swing_horizontal_mode)

    @property
    def swing_modes(self) -> list[str] | None:
        return self._with_current(self._attr_swing_modes, self._device.swing_mode)

    @property
    def swing_horizontal_mode(self) -> str | None:
        return self._known(self._device.swing_horizontal_mode, self.swing_horizontal_modes)

    @property
    def swing_mode(self) -> str | None:
        return self._known(self._device.swing_mode, self.swing_modes)

    @property
    def preset_mode(self) -> str | None:
        return self._known(self._device.preset_mode, self.preset_modes)

    @property
    def fan_mode(self) -> str | None:
        return self._known(self._device.fan_mode, self.fan_modes)

    @property
    def max_temp(self) -> float:
        return self._device.max_temperature

    @property
    def min_temp(self) -> float:
        return self._device.min_temperature

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
    def target_temperature_step(self) -> float:
        return self._device.target_temperature_step

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set new target temperature (and mode, if given)."""
        if (hvac_mode := kwargs.get("hvac_mode")) is not None:
            await self.async_set_hvac_mode(hvac_mode)
        if (temp := kwargs.get(ATTR_TEMPERATURE)) is None:
            return
        _LOGGER.debug("Setting target temperature to %s", temp)
        await self._device.set_temperature(temp)
        self.async_write_ha_state()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        _LOGGER.debug("Setting HVAC mode to %s", hvac_mode)
        if hvac_mode == HVACMode.OFF:
            await self._device.switch_off()
        else:
            await self._device.switch_on(hvac_mode)
        self.async_write_ha_state()

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        _LOGGER.debug("Setting fan mode to %s", fan_mode)
        await self._device.set_fan_mode(fan_mode)
        self.async_write_ha_state()

    async def async_set_swing_horizontal_mode(self, swing_horizontal_mode: str) -> None:
        _LOGGER.debug("Setting swing horizontal mode to %s", swing_horizontal_mode)
        await self._device.set_swing_horizontal_mode(swing_horizontal_mode)
        self.async_write_ha_state()

    async def async_set_swing_mode(self, swing_mode: str) -> None:
        _LOGGER.debug("Setting swing mode to %s", swing_mode)
        await self._device.set_swing_mode(swing_mode)
        self.async_write_ha_state()

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        _LOGGER.debug("Setting preset mode to %s", preset_mode)
        await self._device.set_preset_mode(preset_mode)
        self.async_write_ha_state()

    async def async_turn_on(self) -> None:
        await self._device.switch_on()
        self.async_write_ha_state()

    async def async_turn_off(self) -> None:
        await self._device.switch_off()
        self.async_write_ha_state()
