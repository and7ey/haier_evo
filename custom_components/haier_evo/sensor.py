from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfEnergy, UnitOfTemperature, UnitOfTime, UnitOfVolume
from homeassistant.core import HomeAssistant

from . import api
from .device import parsefloat
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


class HaierWMSensor(HaierSensor):
    """A value of the washer-dryer (CASWD129C1C.yaml), read only."""

    _device: api.HaierWM
    feature: str
    fallback: str

    def __init__(self, device: api.HaierWM) -> None:
        super().__init__(device)
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_{self.feature}"
        self._attr_name = self.fallback


class HaierWMValueSensor(HaierWMSensor):
    """A setting of the current program: one of the profile's values."""

    _attr_device_class = SensorDeviceClass.ENUM

    def __init__(self, device: api.HaierWM) -> None:
        super().__init__(device)
        self._attr_options = device.config.get_values(self.feature)

    @property
    def native_value(self) -> str | None:
        value = self._device.value_of(self.feature)
        return value if value in self.options else None


class HaierWMWashProgramSensor(HaierWMValueSensor):
    _attr_translation_key = "wm_wash_program"
    _attr_icon = "mdi:washing-machine"
    feature, fallback = "wash_program", "Программа стирки"


class HaierWMWashTemperatureSensor(HaierWMValueSensor):
    _attr_translation_key = "wm_wash_temperature"
    _attr_icon = "mdi:thermometer"
    feature, fallback = "wash_temperature", "Температура стирки"


class HaierWMWashSpinSpeedSensor(HaierWMValueSensor):
    _attr_translation_key = "wm_wash_spin_speed"
    _attr_icon = "mdi:rotate-right"
    feature, fallback = "wash_spin_speed", "Скорость отжима"


class HaierWMDryModeSensor(HaierWMValueSensor):
    _attr_translation_key = "wm_dry_mode"
    _attr_icon = "mdi:tumble-dryer"
    feature, fallback = "dry_mode", "Режим сушки"


class HaierWMDryProgramSensor(HaierWMValueSensor):
    _attr_translation_key = "wm_dry_program"
    _attr_icon = "mdi:tumble-dryer"
    feature, fallback = "dry_program", "Программа сушки"


class HaierWMDryLevelSensor(HaierWMValueSensor):
    _attr_translation_key = "wm_dry_level"
    _attr_icon = "mdi:water-percent"
    feature, fallback = "dry_level", "Степень сушки"


class HaierWMCareProgramSensor(HaierWMValueSensor):
    _attr_translation_key = "wm_care_program"
    _attr_icon = "mdi:hanger"
    feature, fallback = "care_program", "Программа ухода"


class HaierWMWashPhaseSensor(HaierWMValueSensor):
    """Seen on the machine: none, washing, rinsing, spinning, finished; waiting for a delayed start."""

    _attr_translation_key = "wm_wash_phase"
    _attr_icon = "mdi:progress-clock"
    feature, fallback = "wash_phase", "Этап стирки"


class HaierWMTimeSensor(HaierWMSensor):
    """Minutes left; the washing's time comes as hours and minutes."""

    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_icon = "mdi:timer-sand"
    hours: str | None = None

    @property
    def native_value(self) -> int | None:
        minutes = parsefloat(self._device.value_of(self.feature))
        hours = parsefloat(self._device.value_of(self.hours)) if self.hours else 0
        if minutes is None or hours is None:
            return None
        return int(hours) * 60 + int(minutes)


class HaierWMWashRemainingTimeSensor(HaierWMTimeSensor):
    feature, fallback = "wash_remaining_minutes", "Осталось до конца стирки"
    hours = "wash_remaining_hours"


class HaierWMDryRemainingTimeSensor(HaierWMTimeSensor):
    feature, fallback = "dry_remaining_time", "Осталось до конца сушки"


# in the order the entities are created
WM_SENSORS: tuple[type[HaierWMSensor], ...] = (
    HaierWMWashPhaseSensor,
    HaierWMWashProgramSensor,
    HaierWMWashTemperatureSensor,
    HaierWMWashSpinSpeedSensor,
    HaierWMWashRemainingTimeSensor,
    HaierWMDryModeSensor,
    HaierWMDryProgramSensor,
    HaierWMDryLevelSensor,
    HaierWMCareProgramSensor,
    HaierWMDryRemainingTimeSensor,
)


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


class HaierLaundryParameterSensor(HaierSensor):
    """A program parameter (temperature, spin, steam...) named as in the app.
    On/off ones are disabled at first: a machine has a dozen of them."""

    _attr_icon = "mdi:tune-variant"

    def __init__(self, device: api.HaierDevice, code: str, title: str, flag: bool) -> None:
        super().__init__(device)
        self._code = code
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_parameter_{code}"
        self._attr_name = title
        self._attr_entity_registry_enabled_default = not flag

    @property
    def native_value(self):
        return self._device.parameter_value(self._code)


class HaierLaundryMeterSensor(HaierSensor):
    """Energy, water and washes of HW70/HW90 (device_laundry.LAUNDRY_METERS):
    the totals count up (the energy dashboard takes them), the values of a
    wash start again at 0."""

    KINDS = {
        # meter: name, device class, unit, state class, diagnostic
        "energy_total": ("Потреблено энергии", SensorDeviceClass.ENERGY, UnitOfEnergy.KILO_WATT_HOUR,
                         SensorStateClass.TOTAL_INCREASING, False),
        "energy_cycle": ("Энергия за стирку", SensorDeviceClass.ENERGY, UnitOfEnergy.KILO_WATT_HOUR, None, False),
        "water_total": ("Израсходовано воды", SensorDeviceClass.WATER, UnitOfVolume.LITERS,
                        SensorStateClass.TOTAL_INCREASING, False),
        "water_cycle": ("Вода за стирку", SensorDeviceClass.WATER, UnitOfVolume.LITERS, None, False),
        "washes_total": ("Всего стирок", None, None, SensorStateClass.TOTAL_INCREASING, True),
        "washes_since_drum_clean": ("Стирок после очистки барабана", None, None, SensorStateClass.TOTAL, True),
    }

    def __init__(self, device: api.HaierDevice, meter: str) -> None:
        super().__init__(device)
        self._meter = meter
        name, device_class, unit, state_class, diagnostic = self.KINDS[meter]
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_{meter}"
        self._attr_name = name
        self._attr_device_class = device_class
        self._attr_native_unit_of_measurement = unit
        self._attr_state_class = state_class
        if diagnostic:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_icon = "mdi:counter" if device_class is None else None
        if device_class is SensorDeviceClass.ENERGY:
            self._attr_suggested_display_precision = 2

    @property
    def native_value(self):
        value = self._device.meter_value(self._meter)
        if value is None or self._attr_device_class is SensorDeviceClass.ENERGY:
            return value
        return int(value)


class HaierLaundryRemainingTimeSensor(HaierLaundrySensor):
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_icon = "mdi:timer-sand"

    def __init__(self, device: api.HaierDevice) -> None:
        super().__init__(device, "remaining_time", "remaining_time", "Осталось")
