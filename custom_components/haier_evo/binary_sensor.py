from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant

from . import api
from .entity import HaierEntity, async_setup_platform


async def async_setup_entry(hass: HomeAssistant, config_entry, async_add_entities) -> None:
    async_setup_platform(hass, config_entry, async_add_entities, "binary_sensor")


class HaierBinarySensor(HaierEntity, BinarySensorEntity):

    def __init__(self, device: api.HaierDevice) -> None:
        super().__init__(device)

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
        self._attr_name = "Дверь"


class HaierREFVacationSensor(HaierREFBinarySensor):

    def __init__(self, device: api.HaierREF) -> None:
        super().__init__(device)
        self._device_attr_name = "vacation_mode"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_vacation"
        self._attr_name = "Режим Отпуск"


class HaierREFSuperFreezeSensor(HaierREFBinarySensor):

    def __init__(self, device: api.HaierREF) -> None:
        super().__init__(device)
        self._device_attr_name = "super_freeze"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_super_freeze"
        self._attr_name = "Супер-заморозка"


class HaierREFSuperCoolingSensor(HaierREFBinarySensor):

    def __init__(self, device: api.HaierREF) -> None:
        super().__init__(device)
        self._device_attr_name = "super_cooling"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_super_cooling"
        self._attr_name = "Супер-охлаждение"


class HaierProblemSensor(HaierBinarySensor):
    """On while the device reports a fault; texts come from the cloud's error table.

    A reminder to clean the filter is no fault: it has a sensor of its own.
    """

    # No entity category, as in 1.7: one would take the sensor out of actions
    # aimed at an area or a device and out of voice assistants.
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, device: api.HaierDevice) -> None:
        super().__init__(device)
        self._device_attr_name = "problem"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_problem"
        self._attr_name = "Неисправность"  # as in 1.x

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"alarms": self._device.alarms, "errors": self._device.alarm_texts}


# The old name: kept for anything that imports it.
HaierACProblemSensor = HaierProblemSensor


class HaierFilterSensor(HaierBinarySensor):
    """The cloud's filterCleaningAlarm: time to clean the filter (1.3)."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_icon = "mdi:air-filter"

    def __init__(self, device: api.HaierDevice) -> None:
        super().__init__(device)
        self._device_attr_name = "filter_cleaning"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_filter"
        self._attr_name = "Чистка фильтра"


class HaierConnectivitySensor(HaierBinarySensor):
    """ONLINE / OFFLINE as the cloud reports it (1.3).

    Always available: it is the one entity that has to show "offline". The
    other entities of an OFFLINE device are unavailable.
    """

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, device: api.HaierDevice) -> None:
        super().__init__(device)
        self._device_attr_name = "connected"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_connected"
        self._attr_name = "Связь с облаком"

    @property
    def available(self) -> bool:
        return True

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"link": self._device.link_status}


class HaierFirmwareUpdateSensor(HaierBinarySensor):
    """The cloud's "a newer firmware is waiting" flag; the app installs it (1.6).

    The cloud does not say which version waits, so this is a binary sensor
    and not an update entity.
    """

    _attr_device_class = BinarySensorDeviceClass.UPDATE
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, device: api.HaierDevice) -> None:
        super().__init__(device)
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_firmware_update"
        self._attr_name = "Обновление прошивки"

    @property
    def is_on(self) -> bool | None:
        return self._device.firmware_update

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "installed_version": self._device.sw_version,
            "auto_update": self._device.firmware_auto_update,
        }


class HaierWHHeatingSensor(HaierBinarySensor):
    """The water heater heats (not keeps warm), 1.0."""

    _attr_device_class = BinarySensorDeviceClass.RUNNING
    _attr_icon = "mdi:heating-coil"

    def __init__(self, device: api.HaierDevice) -> None:
        super().__init__(device)
        self._device_attr_name = "heating"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_heating"
        self._attr_name = "Нагрев"


class HaierLaundryRemoteControlSensor(HaierBinarySensor):
    """Remote control is on: the machine takes commands from the app (1.6.4)."""

    _attr_icon = "mdi:remote"

    def __init__(self, device: api.HaierDevice) -> None:
        super().__init__(device)
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_remote_control"
        self._attr_name = "Удалённое управление"

    @property
    def is_on(self) -> bool:
        return bool(self._device.remote_control)


class HaierLaundryChildLockSensor(HaierBinarySensor):
    """On while the child lock holds the buttons (no LOCK class: its "on" means unlocked)."""

    _attr_icon = "mdi:lock"

    def __init__(self, device: api.HaierDevice) -> None:
        super().__init__(device)
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_child_lock"
        self._attr_name = "Блокировка от детей"

    @property
    def is_on(self) -> bool:
        return bool(self._device.child_lock)
