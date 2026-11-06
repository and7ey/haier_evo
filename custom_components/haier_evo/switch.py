import weakref
from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from .const import DOMAIN
from . import api


async def async_setup_entry(hass: HomeAssistant, config_entry, async_add_entities) -> bool:
    haier_object = hass.data[DOMAIN][config_entry.entry_id]
    entities = []
    for device in haier_object.devices:
        entities.extend(device.create_entities_switch())
    _remove_hidden(hass, haier_object.devices)
    async_add_entities(entities)
    haier_object.write_ha_state()
    return True


def _remove_hidden(hass: HomeAssistant, devices) -> None:
    """Drop switches older versions created for features the app does not offer."""
    registry = er.async_get(hass)
    for device in devices:
        for platform, unique_id in getattr(device, "hidden_entities", []):
            if platform == "switch" and (entity_id := registry.async_get_entity_id("switch", DOMAIN, unique_id)):
                registry.async_remove(entity_id)


class HaierSwitch(SwitchEntity):
    _attr_should_poll = False
    _attr_icon = "mdi:toggle-switch"

    def __init__(self, device: api.HaierDevice) -> None:
        self._device = weakref.proxy(device)
        self._device_attr_name = None

        device.add_write_ha_state_callback(self.async_write_ha_state)

    @property
    def device_info(self) -> dict:
        return self._device.device_info

    @property
    def available(self) -> bool:
        return self._device.available

    async def async_turn_on(self, **kwargs):
        await self.hass.async_add_executor_job(self.turn_on)

    def turn_on(self) -> None:
        method = getattr(self._device, f"set_{self._device_attr_name}", None)
        if method is not None:
            method(True)

    async def async_turn_off(self, **kwargs):
        await self.hass.async_add_executor_job(self.turn_off)

    def turn_off(self, **kwargs) -> None:
        method = getattr(self._device, f"set_{self._device_attr_name}", None)
        if method is not None:
            method(False)

    @property
    def is_on(self) -> bool:
        return bool(getattr(self._device, self._device_attr_name, None))


class HaierACFeatureSwitch(HaierSwitch):
    """An on/off feature of an AC, named as the Haier app names it.

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
        title = device.app_title(device.config[self.feature], self.fallback)
        self._attr_name = f"{device.device_name} {title}"


class HaierACLightSwitch(HaierACFeatureSwitch):
    _attr_icon = "mdi:lightbulb"
    feature, fallback = "light", "Подсветка"


class HaierACSoundSwitch(HaierACFeatureSwitch):
    feature, fallback = "sound", "Звуковой сигнал"


class HaierACQuietSwitch(HaierACFeatureSwitch):
    feature, fallback = "quiet", "Тихий"


class HaierACTurboSwitch(HaierACFeatureSwitch):
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
    # screen (selfCleaningStatus) "Самоочистка" - a heat exchanger cleaning cycle
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
        self._attr_name = f"{device.device_name} Супер-охлаждение"


class HaierREFSuperFreezeSwitch(HaierSwitch):

    def __init__(self, device: api.HaierREF) -> None:
        super().__init__(device)
        self._device_attr_name = "super_freeze"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_super_freeze_switch"
        self._attr_name = f"{device.device_name} Супер-заморозка"


class HaierREFVacationSwitch(HaierSwitch):

    def __init__(self, device: api.HaierREF) -> None:
        super().__init__(device)
        self._device_attr_name = "vacation_mode"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_vacation_mode_switch"
        self._attr_name = f"{device.device_name} Режим Отпуск"


class HaierWHSterilizationSwitch(HaierSwitch):
    _attr_icon = "mdi:bacteria-outline"

    def __init__(self, device: api.HaierWH) -> None:
        super().__init__(device)
        self._device_attr_name = "sterilization_on"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_sterilization"
        self._attr_name = f"{device.device_name} Дезинфекция (BPS)"
