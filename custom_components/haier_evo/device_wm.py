"""Washing machines."""
from __future__ import annotations

import copy
import math
import time
from datetime import datetime, timedelta
from typing import Any

from homeassistant.util import dt as dt_util

from . import config as CFG  # noqa: N812
from . import const as C  # noqa: N812
from .device import (
    HaierDevice,
    _described_attributes,
    device_part,
    parsefloat,
)
from .logger import _LOGGER

# Where the saved description keeps when values were pushed (wall clock):
# a restart soon after a push still knows that a lagging REST is older.
PUSHED_AT = "pushedAt"
# Values that make one reading: the washing's time comes as hours and minutes.
READINGS = (("wash_remaining_hours", "wash_remaining_minutes"),)
# What the length of a washing program depends on, as the machine shows it.
WASH_SETTINGS = ("wash_program", "wash_temperature", "wash_spin_speed")


def duration_minutes(hours: Any, minutes: Any) -> int | None:
    """Hours and minutes as the machine gives them, in minutes; None if one is not a number."""
    h, m = parsefloat(hours), parsefloat(minutes)
    if h is None or m is None or not (math.isfinite(h) and math.isfinite(m)):
        return None
    return int(h) * 60 + int(m)


class HaierWM(HaierDevice):

    # The cloud's REST description of the old format lags behind its pushes: a
    # power-off pushed at 00:06:58, the periodic read at 00:07:20 still had the
    # machine on and ONLINE (CASWD129C1C). With rest_lags_behind_push in the
    # profile a value pushed this recently wins.
    PUSH_WINS_FOR = 900.0

    def __init__(
        self,
        backend_data: dict = None,
        **kwargs
    ) -> None:
        super().__init__(**kwargs)
        # When an attribute code, "status" or "alarms" was pushed (time.time()).
        self._pushed_at: dict[str, float] = {}
        # Minutes of the washing program as the machine shows it before a start
        # (with the settings they are for), and the start it is waiting for: the
        # machine reports only the time to the end.
        self._wash_duration: tuple[tuple, int] | None = None
        self._wash_start: datetime | None = None
        self.status = None
        self.program = None
        self.temperature = None
        self.spin_speed = None
        self.remaining_time = None
        self._get_status(backend_data)
        saved = device_part(self._status_data).get(PUSHED_AT) if isinstance(self._status_data, dict) else None
        if self.config.rest_lags_behind_push and isinstance(saved, dict):
            self._pushed_at = {str(k): float(v) for k, v in saved.items() if isinstance(v, (int, float))}
        self._note_wash_start()

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
        if self.config is not None and self.config.rest_lags_behind_push:
            now = time.time()
            for status in (received_message.get("payload") or {}).get("statuses") or []:
                if not isinstance(status, dict):
                    continue
                if isinstance(status.get("properties"), dict):
                    for key, value in status["properties"].items():
                        if value is not None:
                            self._pushed_at[str(key)] = now
                if "alarms" in status:  # an empty list too: the machine cleared its alarm
                    self._pushed_at["alarms"] = now
        super()._handle_status_update(received_message)
        self._save_pushed_at()

    def _handle_device_status_update(self, received_message: dict) -> None:
        if (received_message.get("payload") or {}).get("status") is not None and self.config.rest_lags_behind_push:
            self._pushed_at["status"] = time.time()
        super()._handle_device_status_update(received_message)
        self._save_pushed_at()

    def _fresh(self) -> set[str]:
        """What was pushed less than PUSH_WINS_FOR ago: codes, "status", "alarms"."""
        now = time.time()
        fresh = {key for key, at in self._pushed_at.items() if 0 <= now - at < self.PUSH_WINS_FOR}
        for names in READINGS:
            codes = {str(attr.code) for name in names if (attr := self.config.get_attr_by_name(name)) is not None}
            if fresh & codes:  # one reading: never the hours of one and the minutes of another
                fresh |= codes
        return fresh

    def _save_pushed_at(self) -> None:
        """Into the description the cache saves, the fresh ones only."""
        if self._pushed_at and isinstance(self._status_data, dict):
            fresh = self._fresh()
            self._pushed_at = {key: at for key, at in self._pushed_at.items() if key in fresh}
            device_part(self._status_data)[PUSHED_AT] = dict(self._pushed_at)

    def update_from_backend(self, data: Any, *, requested_at: float | None = None) -> None:
        """A REST status, but what was pushed lately stays: the snapshot is older than it.

        Only with rest_lags_behind_push in the profile: for the other machines a
        read-back of a command is what the machine has.
        """
        if (
            isinstance(data, dict) and isinstance(self._status_data, dict)
            and self.config is not None and self.config.rest_lags_behind_push
            and (fresh := self._fresh())
        ):
            data = copy.deepcopy(data)
            part = device_part(data)
            saved = device_part(self._status_data)
            if "status" in fresh and saved.get("status") is not None:
                part["status"] = saved["status"]
            if "alarms" in fresh:
                part["alarms"] = copy.deepcopy(saved.get("alarms") or [])
            for attr in part.get("attributes") or []:
                code = str(attr.get("name")) if isinstance(attr, dict) else None
                if code in fresh and code in self._raw:
                    attr["currentValue"] = self._raw[code]
        super().update_from_backend(data, requested_at=requested_at)
        self._save_pushed_at()

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

    def notify(self) -> None:
        # after a whole message or REST status, whatever the order of its values
        self._note_wash_start()
        super().notify()

    def wash_minutes(self) -> int | None:
        """Minutes to the end of the washing (the machine gives hours and minutes)."""
        return duration_minutes(self.value_of("wash_remaining_hours"), self.value_of("wash_remaining_minutes"))

    def _note_wash_start(self) -> None:
        """The start of a delayed washing, estimated: the end minus the program's length.

        A delayed start sets when the program ends (the manual: "the preset time
        is the end of the program"), and the machine reports the time to that end
        only. The program's length is what it shows while a program is chosen and
        not started, the delay not set yet: it counts for those settings of this
        cycle only, a missed choice of another program leaves the start unknown.
        The start is fixed once, so that it does not move with every minute of
        the countdown.
        """
        if self.config is None or self.config.get_attr_by_name("wash_phase") is None:
            return
        phase = self.value_of("wash_phase")
        minutes = self.wash_minutes()
        settings = tuple(self.value_of(name) for name in WASH_SETTINGS)
        if phase == "delayed_start":
            duration = self._wash_duration[1] if self._wash_duration and self._wash_duration[0] == settings else None
            if self._wash_start is None and duration and minutes and minutes >= duration:
                start = dt_util.utcnow() + timedelta(minutes=minutes - duration)
                self._wash_start = start.replace(second=0, microsecond=0)
            return
        self._wash_start = None
        if phase == "none":
            if self.value_of("delayed_start") == "off" and minutes:
                self._wash_duration = (settings, minutes)
            # with the delay being set the time shown is to its end: the length stays
        elif phase is not None:
            # the cycle runs or has ended: its length is not the next one's
            self._wash_duration = None

    @property
    def wash_start(self) -> datetime | None:
        return self._wash_start

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
