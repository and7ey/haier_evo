from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from .const import DOMAIN
from .logger import _LOGGER
from . import api


async def async_setup_entry(hass: HomeAssistant, config_entry, async_add_entities) -> bool:
    haier_object = hass.data[DOMAIN][config_entry.entry_id]
    entities = []
    for device in haier_object.devices:
        if device.config['light'] is not None:
            entities.append(HaierACLightSwitch(device))
        if device.config['sound'] is not None:
            entities.append(HaierACSoundSwitch(device))
        if device.config['quiet'] is not None:
            entities.append(HaierACQuietSwitch(device))
        if device.config['turbo'] is not None:
            entities.append(HaierACTurboSwitch(device))
        if device.config['health'] is not None:
            entities.append(HaierACHealthSwitch(device))
        if device.config['comfort'] is not None:
            entities.append(HaierACComfortSwitch(device))
    async_add_entities(entities)
    return True


class HaierACSwitch(SwitchEntity):

    def __init__(self, device: api.HaierAC) -> None:
        self._device = device
        self._attr_is_on = False

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, self._device.device_id)},
            "name": self._device.device_name,
            "sw_version": self._device.sw_version,
            "model": self._device.device_model,
            "manufacturer": "Haier",
        }

    async def async_turn_on(self, **kwargs):
        await self.hass.async_add_executor_job(self.turn_on)
        self._attr_is_on = True
        self.async_write_ha_state()

    def turn_on(self) -> None:
        raise NotImplementedError

    async def async_turn_off(self, **kwargs):
        await self.hass.async_add_executor_job(self.turn_off)
        self._attr_is_on = False
        self.async_write_ha_state()

    def turn_off(self, **kwargs) -> None:
        raise NotImplementedError


class HaierACLightSwitch(HaierACSwitch):

    def __init__(self, device: api.HaierAC) -> None:
        super().__init__(device)
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_light"
        self._attr_name = f"{device.device_name} Подсветка"
        self._attr_icon = "mdi:lightbulb"

    def turn_on(self) -> None:
        self._device.set_light_on(True)

    def turn_off(self) -> None:
        self._device.set_light_on(False)

    async def async_update(self):
        self._attr_is_on = bool(self._device.light_on)


class HaierACSoundSwitch(HaierACSwitch):

    def __init__(self, device: api.HaierAC) -> None:
        super().__init__(device)
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_sound"
        self._attr_name = f"{device.device_name} Звуковой сигнал"
        self._attr_icon = "mdi:toggle-switch"

    def turn_on(self) -> None:
        self._device.set_sound_on(True)

    def turn_off(self) -> None:
        self._device.set_sound_on(False)

    async def async_update(self):
        self._attr_is_on = bool(self._device.sound_on)


class HaierACQuietSwitch(HaierACSwitch):

    def __init__(self, device: api.HaierAC) -> None:
        super().__init__(device)
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_quiet"
        self._attr_name = f"{device.device_name} Тихий"
        self._attr_icon = "mdi:toggle-switch"

    def turn_on(self) -> None:
        self._device.set_quiet_on(True)

    def turn_off(self) -> None:
        self._device.set_quiet_on(False)


class HaierACTurboSwitch(HaierACSwitch):

    def __init__(self, device: api.HaierAC) -> None:
        super().__init__(device)
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_turbo"
        self._attr_name = f"{device.device_name} Турбо"
        self._attr_icon = "mdi:toggle-switch"

    def turn_on(self) -> None:
        self._device.set_turbo_on(True)

    def turn_off(self) -> None:
        self._device.set_turbo_on(False)


class HaierACHealthSwitch(HaierACSwitch):

    def __init__(self, device: api.HaierAC) -> None:
        super().__init__(device)
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_health"
        self._attr_name = f"{device.device_name} Здоровье"
        self._attr_icon = "mdi:toggle-switch"

    def turn_on(self) -> None:
        self._device.set_health_on(True)

    def turn_off(self) -> None:
        self._device.set_health_on(False)


class HaierACComfortSwitch(HaierACSwitch):

    def __init__(self, device: api.HaierAC) -> None:
        super().__init__(device)
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_comfort"
        self._attr_name = f"{device.device_name} Комфорт"
        self._attr_icon = "mdi:toggle-switch"

    def turn_on(self) -> None:
        self._device.set_comfort_on(True)

    def turn_off(self) -> None:
        self._device.set_comfort_on(False)
