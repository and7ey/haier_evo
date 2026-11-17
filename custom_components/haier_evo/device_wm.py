"""Washing machines."""
from __future__ import annotations

import copy
import time
from typing import Any

from . import config as CFG  # noqa: N812
from . import const as C  # noqa: N812
from .device import (
    HaierDevice,
    _described_attributes,
    device_part,
    parsefloat,
)
from .logger import _LOGGER


class HaierWM(HaierDevice):

    # The cloud's REST description of the old format lags behind its pushes: a
    # power-off pushed at 00:06:58, the periodic read at 00:07:20 still had the
    # machine on and ONLINE (CASWD129C1C). A value pushed this recently wins.
    PUSH_WINS_FOR = 900.0

    def __init__(
        self,
        backend_data: dict = None,
        **kwargs
    ) -> None:
        super().__init__(**kwargs)
        self._pushed_at: dict[str, float] = {}
        self._status_pushed_at: float | None = None
        self.status = None
        self.program = None
        self.temperature = None
        self.spin_speed = None
        self.remaining_time = None
        self._get_status(backend_data)

    @property
    def config(self) -> CFG.HaierWMConfig:
        return self._config

    def to_dict(self) -> dict:
        data = super().to_dict()
        data.update({
            "status": self.status,
            "program": self.program,
            "temperature": self.temperature,
            "spin_speed": self.spin_speed,
            "remaining_time": self.remaining_time,
        })
        return data

    def _load_config_from_attributes(self, data: dict) -> None:
        self._config = CFG.HaierWMConfig(self.device_model, self.hass.config.path(C.DOMAIN))
        attrs = _described_attributes(data)
        for attr in attrs:
            self.config.attrs.append(attr)
        self.config.merge_attributes()
        for attr in self.config.attrs:
            self._apply_value(attr.code, attr.current)
            _LOGGER.debug("%s: %s", self.device_name, attr)

    def _handle_status_update(self, received_message: dict) -> None:
        now = time.monotonic()
        for status in (received_message.get("payload") or {}).get("statuses") or []:
            if isinstance(status, dict) and isinstance(status.get("properties"), dict):
                for key, value in status["properties"].items():
                    if value is not None:
                        self._pushed_at[str(key)] = now
        super()._handle_status_update(received_message)

    def _handle_device_status_update(self, received_message: dict) -> None:
        if (received_message.get("payload") or {}).get("status") is not None:
            self._status_pushed_at = time.monotonic()
        super()._handle_device_status_update(received_message)

    def update_from_backend(self, data: Any, *, requested_at: float | None = None) -> None:
        """A REST status, but what was pushed lately stays: the snapshot is older than it."""
        if isinstance(data, dict) and isinstance(self._status_data, dict):
            now = time.monotonic()
            fresh = {code for code, at in self._pushed_at.items() if now - at < self.PUSH_WINS_FOR}
            keep_status = self._status_pushed_at is not None and now - self._status_pushed_at < self.PUSH_WINS_FOR
            if fresh or keep_status:
                data = copy.deepcopy(data)
                part = device_part(data)
                if keep_status and device_part(self._status_data).get("status") is not None:
                    part["status"] = device_part(self._status_data)["status"]
                for attr in part.get("attributes") or []:
                    code = str(attr.get("name")) if isinstance(attr, dict) else None
                    if code in fresh and code in self._raw:
                        attr["currentValue"] = self._raw[code]
        super().update_from_backend(data, requested_at=requested_at)

    def _set_attribute_value(self, code: str, value: str) -> None:
        attr = self.config.get_attr_by_code(code)
        if not (attr and value is not None):
            return
        elif attr.name == "status":
            self.status = attr.get_item_name(value)
        elif attr.name == "program":
            self.program = attr.get_item_name(value)
        elif attr.name == "temperature":
            self.temperature = attr.get_item_name(value)
        elif attr.name == "spin_speed":
            self.spin_speed = attr.get_item_name(value)
        elif attr.name == "remaining_time":
            self.remaining_time = parsefloat(value)

    async def _send_commands(self, commands: list[dict]) -> None:
        # One plain command, as 1.x sends it: never a group, whatever the
        # description says about a group command name.
        if commands:
            await self._send_single_command(commands[0])

    def get_program_options(self) -> list[str]:
        return self.config.get_values('program')

    def get_temperature_options(self) -> list[str]:
        return self.config.get_values('temperature')

    def get_spin_speed_options(self) -> list[str]:
        return self.config.get_values('spin_speed')

    async def set_program(self, value: str) -> None:
        if commands := self.get_commands("program", value):
            await self._send_commands(commands)
            self.program = value

    async def set_temperature(self, value: str) -> None:
        if commands := self.get_commands("temperature", value):
            await self._send_commands(commands)
            self.temperature = value

    async def set_spin_speed(self, value: str) -> None:
        if commands := self.get_commands("spin_speed", value):
            await self._send_commands(commands)
            self.spin_speed = value

    def create_entities_select(self) -> list:
        from . import select
        entities = []
        if self.config['program'] is not None:
            entities.append(select.HaierWMProgramSelect(self))
        if self.config['temperature'] is not None:
            entities.append(select.HaierWMTemperatureSelect(self))
        if self.config['spin_speed'] is not None:
            entities.append(select.HaierWMSpinSpeedSelect(self))
        return entities

    def value_of(self, name: str) -> str | None:
        """The profile's name of an attribute's value ("on", "wash_cotton"), or the
        raw value of an attribute without mappings; None while not known."""
        attr = self.config.get_attr_by_name(name)
        raw = self._raw.get(str(attr.code)) if attr is not None else None
        if attr is None or raw is None:
            return None
        return attr.get_item_name(raw) if attr.list else raw

    def create_entities_sensor(self) -> list:
        from . import sensor
        entities = []
        if self.config['remaining_time'] is not None:
            entities.append(sensor.HaierWMRemainingTimeSensor(self))
        if self.config['status'] is not None:
            entities.append(sensor.HaierWMStatusSensor(self))
        # the washer-dryer of CASWD129C1C.yaml, read only
        entities += [cls(self) for cls in sensor.WM_SENSORS if self.config[cls.feature] is not None]
        return entities

    def create_entities_binary_sensor(self) -> list:
        from . import binary_sensor
        entities = [binary_sensor.HaierProblemSensor(self), binary_sensor.HaierConnectivitySensor(self)]
        if self.config.get_attr_by_name("dry_program") is not None:
            entities.append(binary_sensor.HaierWMDryerFilterSensor(self))
        return entities + [cls(self) for cls in binary_sensor.WM_FLAG_SENSORS if self.config[cls.feature] is not None]
