from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from .const import DOMAIN, API_HTTP_ROUTE
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
    entities.append(HttpSwitch(haier_object))
    async_add_entities(entities)
    haier_object.write_ha_state()
    return True


class HaierACSwitch(SwitchEntity):

    # _attr_should_poll = False

    def __init__(self, device: api.HaierAC) -> None:
        self._device = device
        self._device_attr_name = None
        self._attr_is_on = False

        def write_ha_state_callback():
            self.update_state()
            self.async_write_ha_state()
        device.add_write_ha_state_callback(write_ha_state_callback)

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, self._device.device_id)},
            "name": self._device.device_name,
            "sw_version": self._device.sw_version,
            "model": self._device.device_model,
            "manufacturer": "Haier",
        }

    @property
    def available(self) -> bool:
        return self._device.available

    async def async_turn_on(self, **kwargs):
        await self.hass.async_add_executor_job(self.turn_on)
        self._attr_is_on = True
        self.async_write_ha_state()

    def turn_on(self) -> None:
        method = getattr(self._device, f"set_{self._device_attr_name}", None)
        if method is not None:
            method(True)

    async def async_turn_off(self, **kwargs):
        await self.hass.async_add_executor_job(self.turn_off)
        self._attr_is_on = False
        self.async_write_ha_state()

    def turn_off(self, **kwargs) -> None:
        method = getattr(self._device, f"set_{self._device_attr_name}", None)
        if method is not None:
            method(False)

    def update_state(self) -> None:
        self._attr_is_on = bool(getattr(self._device, self._device_attr_name, None))

    async def async_update(self):
        self.update_state()


class HaierACLightSwitch(HaierACSwitch):

    def __init__(self, device: api.HaierAC) -> None:
        super().__init__(device)
        self._device_attr_name = "light_on"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_light"
        self._attr_name = f"{device.device_name} Подсветка"
        self._attr_icon = "mdi:lightbulb"


class HaierACSoundSwitch(HaierACSwitch):

    def __init__(self, device: api.HaierAC) -> None:
        super().__init__(device)
        self._device_attr_name = "sound_on"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_sound"
        self._attr_name = f"{device.device_name} Звуковой сигнал"
        self._attr_icon = "mdi:toggle-switch"


class HaierACQuietSwitch(HaierACSwitch):

    def __init__(self, device: api.HaierAC) -> None:
        super().__init__(device)
        self._device_attr_name = "quiet_on"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_quiet"
        self._attr_name = f"{device.device_name} Тихий"
        self._attr_icon = "mdi:toggle-switch"


class HaierACTurboSwitch(HaierACSwitch):

    def __init__(self, device: api.HaierAC) -> None:
        super().__init__(device)
        self._device_attr_name = "turbo_on"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_turbo"
        self._attr_name = f"{device.device_name} Турбо"
        self._attr_icon = "mdi:toggle-switch"


class HaierACHealthSwitch(HaierACSwitch):

    def __init__(self, device: api.HaierAC) -> None:
        super().__init__(device)
        self._device_attr_name = "health_on"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_health"
        self._attr_name = f"{device.device_name} Здоровье"
        self._attr_icon = "mdi:toggle-switch"


class HaierACComfortSwitch(HaierACSwitch):

    def __init__(self, device: api.HaierAC) -> None:
        super().__init__(device)
        self._device_attr_name = "comfort_on"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_comfort"
        self._attr_name = f"{device.device_name} Комфорт"
        self._attr_icon = "mdi:toggle-switch"


class HttpSwitch(SwitchEntity):

    def __init__(self, haier):
        self._haier = haier
        self._attr_unique_id = f"{DOMAIN}_http_switch"
        self._attr_name = "Haier Evo HTTP"
        self._attr_is_on = API_HTTP_ROUTE
        self._attr_icon = "mdi:toggle-switch"

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, self.unique_id)},
            "name": self.name,
            "manufacturer": "Haier"
        }

    async def async_turn_on(self, **kwargs):
        self._haier.allow_http = True
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs):
        self._haier.allow_http = False
        self._attr_is_on = False
        self.async_write_ha_state()

    async def async_update(self):
        self._attr_is_on = self._haier.allow_http
