from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfTemperature, UnitOfTime
from homeassistant.core import HomeAssistant

from . import api
from .device_laundry import LAUNDRY_STATES
from .entity import HaierEntity, async_setup_platform


async def async_setup_entry(hass: HomeAssistant, config_entry, async_add_entities) -> None:
    async_setup_platform(hass, config_entry, async_add_entities, "sensor")


class HaierSensor(HaierEntity, SensorEntity):

    def __init__(self, device: api.HaierDevice):
        super().__init__(device)

    @property
    def native_value(self):
        return getattr(self._device, self._device_attr_name, None)


class HaierREFTemperatureSensor(HaierSensor):
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(self, device: api.HaierREF):
        super().__init__(device)
        self._device_attr_name = "current_temperature"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_temperature"
        self._attr_name = "Температура в помещении"


class HaierREFFridgeTemperatureSensor(HaierREFTemperatureSensor):

    def __init__(self, device: api.HaierREF):
        super().__init__(device)
        self._device_attr_name = "current_fridge_temperature"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_fridge_temperature"
        self._attr_name = "Температура холодильной камеры"


class HaierREFFreezerTemperatureSensor(HaierREFTemperatureSensor):

    def __init__(self, device: api.HaierREF):
        super().__init__(device)
        self._device_attr_name = "current_freezer_temperature"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_freezer_temperature"
        self._attr_name = "Температура морозильной камеры"


class HaierREFFridgeModeSensor(HaierREFTemperatureSensor):

    def __init__(self, device: api.HaierREF):
        super().__init__(device)
        self._device_attr_name = "fridge_mode"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_fridge_mode"
        self._attr_name = "Режим холодильной камеры"

    @property
    def native_value(self) -> float | None:
        return api.parsefloat(getattr(self._device, self._device_attr_name, None))


class HaierREFFreezerModeSensor(HaierREFFridgeModeSensor):

    def __init__(self, device: api.HaierREF):
        super().__init__(device)
        self._device_attr_name = "freezer_mode"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_freezer_mode"
        self._attr_name = "Режим морозильной камеры"


class HaierWMRemainingTimeSensor(HaierSensor):

    def __init__(self, device: api.HaierWM):
        super().__init__(device)
        self._device_attr_name = "remaining_time"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_remaining_time"
        self._attr_name = "Оставшееся время"
        self._attr_native_unit_of_measurement = "мин"


class HaierWMStatusSensor(HaierSensor):

    def __init__(self, device: api.HaierWM):
        super().__init__(device)
        self._device_attr_name = "status"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_status"
        self._attr_name = "Статус"

    @property
    def native_value(self) -> str | None:
        value = getattr(self._device, self._device_attr_name, None)
        return None if value is None else str(value)


class HaierACTemperatureSensor(HaierSensor):
    """The room temperature (sensors.indoorTemperature) as a sensor of its own (1.6)."""

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(self, device: api.HaierAC):
        super().__init__(device)
        self._device_attr_name = "current_temperature"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_temperature"
        self._attr_name = "Температура в комнате"  # as in 1.x


class HaierWHTemperatureSensor(HaierSensor):
    """The water temperature of a water heater (1.0)."""

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(self, device: api.HaierDevice):
        super().__init__(device)
        self._device_attr_name = "current_temperature"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_temperature"
        self._attr_name = "Температура воды"


class HaierLaundrySensor(HaierSensor):
    """A value of a washer or dryer (1.6.4)."""

    def __init__(self, device: api.HaierDevice, attr: str, suffix: str, name: str) -> None:
        super().__init__(device)
        self._device_attr_name = attr
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_{suffix}"
        self._attr_name = name


class HaierLaundryStateSensor(HaierLaundrySensor):
    """off / idle / running / paused / finished / cancelled: an automation on
    "finished" tells that the washing is done."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(LAUNDRY_STATES)
    _attr_translation_key = "laundry_state"
    _attr_icon = "mdi:washing-machine"

    def __init__(self, device: api.HaierDevice) -> None:
        super().__init__(device, "state", "state", "Состояние")


class HaierLaundryProgramSensor(HaierLaundrySensor):
    _attr_icon = "mdi:format-list-bulleted"

    def __init__(self, device: api.HaierDevice) -> None:
        super().__init__(device, "program_title", "program", "Программа")


class HaierLaundryPhaseSensor(HaierLaundrySensor):
    _attr_icon = "mdi:progress-clock"

    def __init__(self, device: api.HaierDevice) -> None:
        super().__init__(device, "phase", "phase", "Этап")


class HaierLaundryRemainingTimeSensor(HaierLaundrySensor):
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_icon = "mdi:timer-sand"

    def __init__(self, device: api.HaierDevice) -> None:
        super().__init__(device, "remaining_time", "remaining_time", "Осталось")
