"""Water heaters (device type "WH"), e.g. ES80V-F7 (1.0, on asyncio)."""
from __future__ import annotations

from . import config as CFG  # noqa: N812
from . import const as C  # noqa: N812
from .device import HaierCommandLocked, HaierDevice, _described_attributes, _norm, parsebool, parsefloat
from .logger import _LOGGER


class HaierWH(HaierDevice):
    """Current and target water temperature, power, heating mode (Eco: half of
    the tank, Max: the whole tank), heating or keeping warm, sterilization (BPS).
    """

    def __init__(self, backend_data: dict | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.current_temperature: float | None = None
        self.target_temperature: float | None = None
        self.min_temperature = 35.0
        self.max_temperature = 75.0
        self.status = False
        self.heating_mode: str | None = None
        self.heating = False
        self.sterilization_on = False
        self._get_status(backend_data)

    @property
    def config(self) -> CFG.HaierWHConfig:
        return self._config

    def to_dict(self) -> dict:
        data = super().to_dict()
        data.update({
            "current_temperature": self.current_temperature,
            "target_temperature": self.target_temperature,
            "min_temperature": self.min_temperature,
            "max_temperature": self.max_temperature,
            "status": self.status,
            "heating_mode": self.heating_mode,
            "heating": self.heating,
            "sterilization_on": self.sterilization_on,
        })
        return data

    def _load_config_from_attributes(self, data: dict) -> None:
        self._config = CFG.HaierWHConfig(self.device_model, self.hass.config.path(C.DOMAIN))
        for attr in _described_attributes(data):
            self.config.attrs.append(attr)
        self.config.merge_attributes()
        for attr in self.config.attrs:
            self._apply_value(attr.code, attr.current)
            if attr.name == "target_temperature" and attr.range:
                self.min_temperature = parsefloat(attr.range.min_value, self.min_temperature)
                self.max_temperature = parsefloat(attr.range.max_value, self.max_temperature)
            _LOGGER.debug("%s: %s", self.device_name, attr)
        self.constraint.extend(
            item for item in data.get("constraint") or [] if isinstance(item, dict)
        )

    def _set_attribute_value(self, code: str, value: str) -> None:
        attr = self.config.get_attr_by_code(code) if self.config else None
        if attr is None or value is None:
            return
        # booleans come as "true"/"false" in the lists and as "1"/"0" in values
        value = _norm(value)
        if attr.name == "current_temperature":
            self.current_temperature = parsefloat(value, self.current_temperature)
        elif attr.name == "target_temperature":
            self.target_temperature = parsefloat(value, self.target_temperature)
        elif attr.name == "status":
            self.status = parsebool(value)
        elif attr.name == "heating_mode":
            # "none" comes while sterilization runs; afterwards the heater
            # returns to the previous mode: keep showing that one
            if (mode := attr.get_item_name(value)) not in (None, "none"):
                self.heating_mode = mode
        elif attr.name == "heating_status":
            self.heating = attr.get_item_name(value) == "heating"
        elif attr.name == "sterilization":
            self.sterilization_on = parsebool(value)

    async def _apply(self, commands: list[dict]) -> None:
        """Send commands and show the expected state until the heater echoes it."""
        if not commands:
            return
        await self._send_commands(commands)
        for command in commands:
            self._apply_value(command.get("commandName"), command.get("value"))
        self.notify()

    def get_heating_modes(self) -> list[str]:
        return [m for m in self.config.get_values("heating_mode") if m != "none"]

    async def set_temperature(self, value: float) -> None:
        self._check_writable("target_temperature")
        value = round(min(max(float(value), self.min_temperature), self.max_temperature))
        await self._apply([{"commandName": self.config["target_temperature"], "value": str(value)}])

    async def switch_on(self) -> None:
        self._check_writable("status")
        await self._apply(self.get_commands("status", "on"))

    async def switch_off(self) -> None:
        self._check_writable("status")
        await self._apply(self.get_commands("status", "off"))

    async def set_heating_mode(self, value: str) -> None:
        if value not in self.get_heating_modes():
            attr = self.config.get_attr_by_name("heating_mode")
            title = self.app_title(attr.code, attr.description or "heating_mode") if attr else None
            raise HaierCommandLocked(
                translation_domain=C.DOMAIN,
                translation_key="value_not_allowed",
                translation_placeholders={
                    "device": str(self.device_name),
                    "attribute": str(title or "heating_mode"),
                    "value": str(value),
                    "allowed": ", ".join(self.get_heating_modes()) or "—",
                },
            )
        commands = []
        if not self.status:
            # the mode cannot be changed while the heater is off: turn it on first
            self._check_writable("status")
            commands += self.get_commands("status", "on")
        self._check_writable("heating_mode", value, assume={self.config["status"]: "1"})
        commands += self.get_commands("heating_mode", value)
        await self._apply(commands)

    async def set_sterilization_on(self, value: bool) -> None:
        self._check_writable("sterilization")
        await self._apply(self.get_commands("sterilization", value))

    def create_entities_water_heater(self) -> list:
        from . import water_heater
        return [water_heater.HaierWHEntity(self)]

    def create_entities_switch(self) -> list:
        from . import switch
        return [switch.HaierWHSterilizationSwitch(self)] if self.config["sterilization"] is not None else []

    def create_entities_sensor(self) -> list:
        from . import sensor
        return [sensor.HaierWHTemperatureSensor(self)] if self.config["current_temperature"] is not None else []

    def create_entities_binary_sensor(self) -> list:
        from . import binary_sensor
        entities = [
            binary_sensor.HaierProblemSensor(self),
            binary_sensor.HaierConnectivitySensor(self),
            binary_sensor.HaierFirmwareUpdateSensor(self),
        ]
        if self.config["heating_status"] is not None:
            entities.append(binary_sensor.HaierWHHeatingSensor(self))
        return entities
