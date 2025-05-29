import weakref
from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant
from .const import DOMAIN
from . import api


async def async_setup_entry(hass: HomeAssistant, config_entry, async_add_entities) -> bool:
    haier_object = hass.data[DOMAIN][config_entry.entry_id]
    entities = []
    for device in haier_object.devices:
        entities.extend(device.create_entities_binary_sensor())
    if entities:
        async_add_entities(entities)
        haier_object.write_ha_state()
    return True


class HaierBinarySensor(BinarySensorEntity):
    # _attr_should_poll = False

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

    @property
    def is_on(self) -> bool:
        return getattr(self._device, self._device_attr_name, False)


class HaierREFBinarySensor(HaierBinarySensor):
    _attr_icon = "mdi:fridge-outline"


class HaierREFDoorSensor(HaierREFBinarySensor):

    def __init__(self, device: api.HaierREF) -> None:
        super().__init__(device)
        self._device_attr_name = "door_open"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_door_open"
        self._attr_name = f"{device.device_name} Дверь"


class HaierREFVacationSensor(HaierREFBinarySensor):

    def __init__(self, device: api.HaierREF) -> None:
        super().__init__(device)
        self._device_attr_name = "vacation_mode"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_vacation"
        self._attr_name = f"{device.device_name} Режим Отпуск"


class HaierREFSuperFreezeSensor(HaierREFBinarySensor):

    def __init__(self, device: api.HaierREF) -> None:
        super().__init__(device)
        self._device_attr_name = "super_freeze"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_super_freeze"
        self._attr_name = f"{device.device_name} Супер-заморозка"


class HaierREFSuperCoolingSensor(HaierREFBinarySensor):

    def __init__(self, device: api.HaierREF) -> None:
        super().__init__(device)
        self._device_attr_name = "super_cooling"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_super_cooling"
        self._attr_name = f"{device.device_name} Супер-охлаждение"
