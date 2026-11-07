import weakref
from homeassistant.components.sensor import SensorEntity, SensorDeviceClass, SensorStateClass
from homeassistant.core import HomeAssistant
from homeassistant.const import EntityCategory, UnitOfTemperature
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


class HaierACOutdoorTemperatureSensor(HaierSensor):
    """The outdoor unit's own sensor (code 38, "Температура в комнате" in the cloud).

    Not the outdoor air: off by several degrees, colder after heating and
    warmer after cooling, hence a diagnostic entity.
    """
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:hvac"

    def __init__(self, device: api.HaierAC):
        super().__init__(device)
        self._device_attr_name = "outdoor_temperature"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_outdoor_temperature"
        self._attr_name = f"{device.device_name} Температура наружного блока"

    @property
    def native_value(self) -> float | None:
        return self._device.outdoor_temperature
