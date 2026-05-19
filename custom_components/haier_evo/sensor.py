import weakref
from homeassistant.components.sensor import SensorEntity, SensorDeviceClass, SensorStateClass
from homeassistant.core import HomeAssistant
from homeassistant.const import UnitOfTemperature, UnitOfTime
from homeassistant.const import TEMPERATURE
from .const import DOMAIN
from . import api


async def async_setup_entry(hass: HomeAssistant, config_entry, async_add_entities) -> bool:
    haier_object = hass.data[DOMAIN][config_entry.entry_id]
    entities = []
    for device in haier_object.devices:
        entities.extend(device.create_entities_sensor())
    if entities:
        async_add_entities(entities)
        haier_object.write_ha_state()
    return True


class HaierSensor(SensorEntity):

    def __init__(self, device: api.HaierDevice):
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


_WM_ATTR_NAMES = {
    'care_program': 'Программа ухода',
    'dry_mode': 'Режим сушки',
    'dry_program': 'Программа сушки',
    'wash_temperature': 'Температура стирки',
    'dry_level': 'Степень сушки',
    'wash_program': 'Программа стирки',
    'wash_spin_speed': 'Скорость отжима',
    'dry_remaining_time': 'Время до окончания сушки',
}

_WM_ATTR_TRANSLATION_KEYS = {
    'care_program': 'wm_care_program',
    'dry_mode': 'wm_dry_mode',
    'dry_program': 'wm_dry_program',
    'dry_level': 'wm_dry_level',
    'wash_program': 'wm_program',
    'wash_spin_speed': 'wm_wash_spin_speed',
    'wash_temperature': 'wm_wash_temperature',
}

_WM_NUMERIC_CONFIG = {
    'dry_remaining_time': (
        UnitOfTime.MINUTES,
        SensorDeviceClass.DURATION,
        SensorStateClass.MEASUREMENT,
    ),
}


class HaierWMAttributeSensor(HaierSensor):

    def __init__(self, device: api.HaierWM, attr_name: str) -> None:
        super().__init__(device)
        self._attr_name_key = attr_name
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_{attr_name}"
        display = _WM_ATTR_NAMES.get(attr_name, attr_name.replace('_', ' '))
        self._attr_name = f"{device.device_name} {display}"
        tkey = _WM_ATTR_TRANSLATION_KEYS.get(attr_name)
        if tkey:
            self._attr_translation_key = tkey

    @property
    def native_value(self):
        attr = self._device.config.get_attr_by_name(self._attr_name_key)
        if attr is None or attr.current is None:
            return None
        current = str(attr.current)  # API sends ints; normalize for get_item_name
        if attr.list:
            mapped = attr.get_item_name(current)
            if mapped not in ("None", "unknown"):
                return mapped
        return current


class HaierWMRemainingTimeSensor(HaierSensor):
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, device: api.HaierWM):
        super().__init__(device)
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_wash_remaining_time"
        self._attr_name = f"{device.device_name} Время до окончания стирки"

    @property
    def native_value(self):
        hours_attr = self._device.config.get_attr_by_name('wash_remaining_hours')
        minutes_attr = self._device.config.get_attr_by_name('wash_remaining_minutes')
        if hours_attr is None or minutes_attr is None:
            return None
        try:
            hours = int(hours_attr.current or 0)
            minutes = int(minutes_attr.current or 0)
        except (TypeError, ValueError):
            return None
        return hours * 60 + minutes


class HaierWMNumericSensor(HaierSensor):

    def __init__(self, device: api.HaierWM, attr_name: str) -> None:
        super().__init__(device)
        self._attr_name_key = attr_name
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_{attr_name}"
        display = _WM_ATTR_NAMES.get(attr_name, attr_name.replace('_', ' '))
        self._attr_name = f"{device.device_name} {display}"
        unit, device_class, state_class = _WM_NUMERIC_CONFIG[attr_name]
        self._attr_native_unit_of_measurement = unit
        self._attr_device_class = device_class
        self._attr_state_class = state_class

    @property
    def native_value(self):
        attr = self._device.config.get_attr_by_name(self._attr_name_key)
        if attr is None or attr.current is None:
            return None
        try:
            return int(attr.current)
        except (ValueError, TypeError):
            return None


class HaierWMEnumSensor(HaierSensor):
    _attr_device_class = SensorDeviceClass.ENUM

    def __init__(self, device: api.HaierWM, attr_name: str) -> None:
        super().__init__(device)
        self._attr_name_key = attr_name
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_{attr_name}"
        display = _WM_ATTR_NAMES.get(attr_name, attr_name.replace('_', ' '))
        self._attr_name = f"{device.device_name} {display}"
        tkey = _WM_ATTR_TRANSLATION_KEYS.get(attr_name)
        if tkey:
            self._attr_translation_key = tkey
        attr = device.config.get_attr_by_name(attr_name)
        self._attr_options = [item.name for item in attr.list] if attr and attr.list else []

    @property
    def native_value(self):
        attr = self._device.config.get_attr_by_name(self._attr_name_key)
        if attr is None or attr.current is None:
            return None
        if attr.list:
            mapped = attr.get_item_name(str(attr.current))
            if mapped not in ("None", "unknown"):
                return mapped
        return None
