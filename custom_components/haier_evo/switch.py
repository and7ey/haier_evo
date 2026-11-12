from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant

from . import api
from .entity import HaierEntity, async_setup_platform


async def async_setup_entry(hass: HomeAssistant, config_entry, async_add_entities) -> None:
    async_setup_platform(hass, config_entry, async_add_entities, "switch")


class HaierSwitch(HaierEntity, SwitchEntity):
    _attr_icon = "mdi:toggle-switch"

    def __init__(self, device: api.HaierDevice) -> None:
        super().__init__(device)

    async def _async_set(self, value: bool) -> None:
        method = getattr(self._device, f"set_{self._device_attr_name}", None)
        if method is not None:
            await method(value)
            self.async_write_ha_state()

    async def async_turn_on(self, **kwargs) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._async_set(False)

    @property
    def is_on(self) -> bool:
        return bool(getattr(self._device, self._device_attr_name, None))


class HaierACFeatureSwitch(HaierSwitch):
    """An on/off feature of an AC, named as the Haier app names it (1.5).

    feature: the integration's attribute name (config[feature] is the cloud
    code, device.<feature>_on the state, set_<feature>_on the command) and the
    unique_id suffix; fallback: the name when the app has no screen for it.
    """

    feature: str = ""
    fallback: str = ""

    def __init__(self, device: api.HaierAC) -> None:
        super().__init__(device)
        self._device_attr_name = f"{self.feature}_on"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_{self.feature}"
        self._attr_name = device.app_title(device.config[self.feature], self.fallback)


class HaierACLightSwitch(HaierACFeatureSwitch):
    _attr_icon = "mdi:lightbulb"
    feature, fallback = "light", "Подсветка"


class HaierACSoundSwitch(HaierACFeatureSwitch):
    feature, fallback = "sound", "Звуковой сигнал"


class HaierACQuietSwitch(HaierACFeatureSwitch):
    # Duplicates the climate preset "sleep" (same code); kept for automations.
    _attr_entity_registry_enabled_default = False
    feature, fallback = "quiet", "Тихий"


class HaierACTurboSwitch(HaierACFeatureSwitch):
    # Duplicates the climate preset "boost" (same code); kept for automations.
    _attr_entity_registry_enabled_default = False
    feature, fallback = "turbo", "Турбо"


class HaierACHealthSwitch(HaierACFeatureSwitch):
    feature, fallback = "health", "Здоровье"


class HaierACComfortSwitch(HaierACFeatureSwitch):
    feature, fallback = "comfort", "Комфортный сон"


class HaierACCleaningSwitch(HaierACFeatureSwitch):
    # the app: "Стерильная очистка", a heat exchanger cycle at 56 °C
    _attr_icon = "mdi:spray-bottle"
    feature, fallback = "cleaning", "Стерильная очистка"


class HaierACAntiFreezeSwitch(HaierACFeatureSwitch):
    # the app: "10 градусов", keeps the room at +10 °C
    feature, fallback = "antifreeze", "10 градусов"


class HaierACAutoHumiditySwitch(HaierACFeatureSwitch):
    # attribute 31: the cloud's flat list says "Авто влажность", the app's
    # screen (selfCleaningStatus) "Самоочистка", a heat exchanger cleaning cycle
    _attr_icon = "mdi:spray-bottle"
    feature, fallback = "autohumidity", "Самоочистка"


AC_SWITCHES = (
    HaierACLightSwitch,
    HaierACSoundSwitch,
    HaierACQuietSwitch,
    HaierACTurboSwitch,
    HaierACHealthSwitch,
    HaierACComfortSwitch,
    HaierACCleaningSwitch,
    HaierACAntiFreezeSwitch,
    HaierACAutoHumiditySwitch,
)


class HaierREFSuperCoolingSwitch(HaierSwitch):

    def __init__(self, device: api.HaierREF) -> None:
        super().__init__(device)
        self._device_attr_name = "super_cooling"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_super_cooling_switch"
        self._attr_name = "Супер-охлаждение"


class HaierREFSuperFreezeSwitch(HaierSwitch):

    def __init__(self, device: api.HaierREF) -> None:
        super().__init__(device)
        self._device_attr_name = "super_freeze"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_super_freeze_switch"
        self._attr_name = "Супер-заморозка"


class HaierREFVacationSwitch(HaierSwitch):

    def __init__(self, device: api.HaierREF) -> None:
        super().__init__(device)
        self._device_attr_name = "vacation_mode"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_vacation_mode_switch"
        self._attr_name = "Режим Отпуск"


class HaierWHSterilizationSwitch(HaierSwitch):
    """Sterilization (BPS) of a water heater, 1.0."""

    _attr_icon = "mdi:bacteria-outline"

    def __init__(self, device: api.HaierDevice) -> None:
        super().__init__(device)
        self._device_attr_name = "sterilization_on"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_sterilization"
        self._attr_name = "Дезинфекция (BPS)"
