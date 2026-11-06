import weakref
from homeassistant.components.binary_sensor import BinarySensorEntity, BinarySensorDeviceClass
from homeassistant.const import EntityCategory
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
    _attr_should_poll = False

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


class HaierWHHeatingSensor(HaierBinarySensor):
    _attr_device_class = BinarySensorDeviceClass.RUNNING
    _attr_icon = "mdi:heating-coil"

    def __init__(self, device: api.HaierWH) -> None:
        super().__init__(device)
        self._device_attr_name = "heating"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_heating"
        self._attr_name = f"{device.device_name} Нагрев"


class HaierProblemSensor(HaierBinarySensor):
    """On while the device reports a fault (E3/E6/H0 on water heaters, AC unit errors).

    Codes and the cloud's own texts are in the attributes; maintenance
    reminders (filter cleaning) have their own sensor.
    """
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, device: api.HaierDevice) -> None:
        super().__init__(device)
        self._device_attr_name = "problem"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_problem"
        self._attr_name = f"{device.device_name} Неисправность"

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "alarms": self._device.alarms,
            "errors": self._device.alarm_texts,
        }


class HaierFilterSensor(HaierBinarySensor):
    """The cloud's filterCleaningAlarm: time to clean the AC filter."""
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_icon = "mdi:air-filter"

    def __init__(self, device: api.HaierDevice) -> None:
        super().__init__(device)
        self._device_attr_name = "filter_cleaning"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_filter"
        self._attr_name = f"{device.device_name} Чистка фильтра"


class HaierConnectivitySensor(HaierBinarySensor):
    """Online/offline as the cloud reports it (ONLINE / OFFLINE)."""
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, device: api.HaierDevice) -> None:
        super().__init__(device)
        self._device_attr_name = "connected"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_connected"
        self._attr_name = f"{device.device_name} Связь с облаком"

    @property
    def available(self) -> bool:
        return True

    @property
    def extra_state_attributes(self) -> dict:
        return {"link": self._device.link_status}


class HaierFirmwareUpdateSensor(HaierBinarySensor):
    """The cloud's "a newer firmware is waiting" flag; the app installs it.

    The cloud does not say which version is waiting, so this is a binary
    sensor and not an update entity.
    """
    _attr_device_class = BinarySensorDeviceClass.UPDATE
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, device: api.HaierDevice) -> None:
        super().__init__(device)
        self._device_attr_name = "firmware_update"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_firmware_update"
        self._attr_name = f"{device.device_name} Обновление прошивки"

    @property
    def is_on(self) -> bool | None:
        return self._device.firmware_update

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "installed_version": self._device.sw_version,
            "auto_update": self._device.firmware_auto_update,
        }
