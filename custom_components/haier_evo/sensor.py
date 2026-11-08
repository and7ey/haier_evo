import weakref
from homeassistant.components.sensor import SensorEntity, SensorDeviceClass, SensorStateClass
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.const import UnitOfTemperature
from homeassistant.const import TEMPERATURE
from .const import DOMAIN
from . import api
from .entity import HaierEntityMixin


# sensors of earlier versions that are gone, by unique_id suffix: the outdoor
# unit of 1.6.0 (code 38 does not change on AS25S2SF2FA)
RETIRED_SENSORS = ("outdoor_temperature",)


async def async_setup_entry(hass: HomeAssistant, config_entry, async_add_entities) -> bool:
    haier_object = hass.data[DOMAIN][config_entry.entry_id]
    entities = []
    for device in haier_object.devices:
        entities.extend(device.create_entities_sensor())
    _remove_retired(hass, haier_object.devices)
    if entities:
        async_add_entities(entities)
        haier_object.write_ha_state()
    return True


def _remove_retired(hass: HomeAssistant, devices) -> None:
    """Drop sensors earlier versions created and this one no longer offers."""
    registry = er.async_get(hass)
    for device in devices:
        for suffix in RETIRED_SENSORS:
            unique_id = f"{device.device_id}_{device.device_model}_{suffix}"
            if entity_id := registry.async_get_entity_id("sensor", DOMAIN, unique_id):
                registry.async_remove(entity_id)


class HaierSensor(HaierEntityMixin, SensorEntity):

    def __init__(self, device: api.HaierDevice):
        self._device = weakref.proxy(device)
        self._device_attr_name = None

    @property
    def device_info(self) -> dict:
        return self._device.device_info

    @property
    def available(self) -> bool:
        return self._device.available

    @property
    def native_value(self) -> float:
        return getattr(self._device, self._device_attr_name, 0.0)


class HaierREFTemperatureSensor(HaierSensor):
    _attr_device_class = TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(self, device: api.HaierREF):
        super().__init__(device)
        self._device_attr_name = "current_temperature"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_temperature"
        self._attr_name = f"{device.device_name} Температура в помещении"


class HaierREFFridgeTemperatureSensor(HaierREFTemperatureSensor):

    def __init__(self, device: api.HaierREF):
        super().__init__(device)
        self._device_attr_name = "current_fridge_temperature"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_fridge_temperature"
        self._attr_name = f"{device.device_name} Температура холодильной камеры"


class HaierREFFreezerTemperatureSensor(HaierREFTemperatureSensor):

    def __init__(self, device: api.HaierREF):
        super().__init__(device)
        self._device_attr_name = "current_freezer_temperature"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_freezer_temperature"
        self._attr_name = f"{device.device_name} Температура морозильной камеры"


class HaierREFFridgeModeSensor(HaierREFTemperatureSensor):

    def __init__(self, device: api.HaierREF):
        super().__init__(device)
        self._device_attr_name = "fridge_mode"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_fridge_mode"
        self._attr_name = f"{device.device_name} Режим холодильной камеры"

    @property
    def native_value(self) -> float:
        return float(getattr(self._device, self._device_attr_name, 0.0))


class HaierREFFreezerModeSensor(HaierREFFridgeModeSensor):

    def __init__(self, device: api.HaierREF):
        super().__init__(device)
        self._device_attr_name = "freezer_mode"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_freezer_mode"
        self._attr_name = f"{device.device_name} Режим морозильной камеры"


class HaierWMRemainingTimeSensor(HaierSensor):

    def __init__(self, device: api.HaierWM):
        super().__init__(device)
        self._device_attr_name = "remaining_time"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_remaining_time"
        self._attr_name = f"{device.device_name} Оставшееся время"
        self._attr_native_unit_of_measurement = "мин"


class HaierWMStatusSensor(HaierSensor):

    def __init__(self, device: api.HaierWM):
        super().__init__(device)
        self._device_attr_name = "status"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_status"
        self._attr_name = f"{device.device_name} Статус"

    @property
    def native_value(self) -> str:
        return str(getattr(self._device, self._device_attr_name, "unknown"))


class HaierWHTemperatureSensor(HaierSensor):
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, device: api.HaierWH):
        super().__init__(device)
        self._device_attr_name = "current_temperature"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_temperature"
        self._attr_name = f"{device.device_name} Температура воды"

    @property
    def native_value(self) -> float | None:
        return self._device.current_temperature


class HaierACTemperatureSensor(HaierSensor):
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, device: api.HaierAC):
        super().__init__(device)
        self._device_attr_name = "current_temperature"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_temperature"
        self._attr_name = f"{device.device_name} Температура в комнате"

    @property
    def native_value(self) -> float | None:
        return self._device.current_temperature


class HaierLaundrySensor(HaierSensor):
    """A washer or dryer value; the device knows it under self._device_attr_name."""

    def __init__(self, device: api.HaierLaundry, attr: str, suffix: str, name: str) -> None:
        super().__init__(device)
        self._laundry_attr = attr
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_{suffix}"
        self._attr_name = f"{device.device_name} {name}"

    @property
    def native_value(self):
        return getattr(self._device, self._laundry_attr, None)


class HaierLaundryStateSensor(HaierLaundrySensor):
    """off / idle / running / paused / finished / cancelled: an automation on
    "finished" tells that the washing is done."""
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(api.LAUNDRY_STATES)
    _attr_translation_key = "laundry_state"
    _attr_icon = "mdi:washing-machine"

    def __init__(self, device: api.HaierLaundry) -> None:
        super().__init__(device, "state", "state", "Состояние")


class HaierLaundryProgramSensor(HaierLaundrySensor):
    _attr_icon = "mdi:format-list-bulleted"

    def __init__(self, device: api.HaierLaundry) -> None:
        super().__init__(device, "program_title", "program", "Программа")


class HaierLaundryPhaseSensor(HaierLaundrySensor):
    _attr_icon = "mdi:progress-clock"

    def __init__(self, device: api.HaierLaundry) -> None:
        super().__init__(device, "phase", "phase", "Этап")


class HaierLaundryRemainingTimeSensor(HaierLaundrySensor):
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = "min"
    _attr_icon = "mdi:timer-sand"

    def __init__(self, device: api.HaierLaundry) -> None:
        super().__init__(device, "remaining_time", "remaining_time", "Осталось")
