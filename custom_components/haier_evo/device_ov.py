"""Haier EVO ovens. The app screens identify the attributes of the HOQ-F6QSB4GBU."""
from __future__ import annotations

from typing import Any

from . import config as CFG  # noqa: N812
from . import const as C  # noqa: N812
from .device import HaierCommandLocked, HaierDevice, _norm, device_part


def _field(node: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _integer(value: Any) -> int | None:
    try:
        return int(str(value))
    except ValueError:
        return None


class HaierOV(HaierDevice):
    def __init__(self, backend_data: dict | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self._get_status(backend_data or {})

    @property
    def config(self) -> CFG.HaierDeviceConfig:
        return self._oven_config

    def _load_config_from_attributes(self, data: dict) -> None:
        part = device_part(data)
        # Ovens have no legacy profile IDs. Keep the full model on the device in HA.
        model = _field(part, "info", "model")
        if isinstance(model, str) and model:
            self.device_model = model
        self._oven_config = CFG.HaierDeviceConfig(self.device_model, self.hass.config.path(C.DOMAIN))
        self._modes: dict = {}
        if self.device_model != "HOQ-F6QSB4GBU":
            return
        screen = data.get("device")
        main = _field(screen, "mainControl")
        monitor = _field(screen, "monitoringScreen")
        time = _field(monitor, "time", "remainingTime")
        links = {
            "power": _field(main, "power", "link"),
            "remote": _field(main, "remoteControlMode", "link"),
            "child_lock": _field(main, "lock", "link"),
            "current_temperature": _field(monitor, "temperature", "link"),
            "work_status": _field(monitor, "workStatus"),
            "hours": _field(time, "hours"),
            "minutes": _field(time, "minutes"),
        }
        meanings = {"grCmdOperationMode": "mode", "grCmdTargetTemperature": "target_temperature"}
        for item in _field(screen, "enrichToAttr") or []:
            meaning = _field(item, "enrichName")
            if isinstance(meaning, str) and meaning in meanings:
                links[meanings[meaning]] = _field(item, "readLink")
        titles = _field(screen, "enrichAttributes", "attrs", "grCmdOperationMode", "list", "valueToIcon")
        if isinstance(titles, dict):
            self._modes = {code: title for code, item in titles.items()
                           if isinstance(title := _field(item, "name"), str) and title}
        attrs = {a["name"]: a for a in part.get("attributes") or []
                 if isinstance(a, dict) and isinstance(a.get("name"), str) and a["name"].isdigit()}
        if _norm(_field(main, "power", "activeValue")) != "1":
            links.pop("power")
        for name, link in links.items():
            code = _field(link, "name")
            attr = attrs.get(code) if isinstance(code, str) else None
            if not attr or attr.get("readable") is not True or attr.get("type") != _field(link, "type"):
                continue
            if name in {"power", "remote", "child_lock"}:
                options = _field(attr, "list", "data")
                if not isinstance(options, list) or {_norm(_field(item, "data")) for item in options} != {"0", "1"}:
                    continue
            self.config.attrs.append(CFG.Attribute({**attr, "attrname": name}))
        for code, attr in attrs.items():
            self._apply_value(code, attr.get("currentValue"))

    def update_from_backend(self, data: dict, *, requested_at: float | None = None) -> None:
        if not isinstance(data, dict) or (requested_at is not None and self.changed_at > requested_at):
            return
        # A full snapshot replaces permission flags too, even if a flag is missing.
        self._raw.clear()
        super().update_from_backend(data, requested_at=requested_at)

    def _handle_status_update(self, received_message: dict) -> None:
        if not self.synced:
            # The first live push may be partial. Do not reuse permissions from the cache.
            self._raw.clear()
        super()._handle_status_update(received_message)

    def _value(self, name: str) -> str | None:
        code = self.config[name]
        return self._raw.get(code) if code is not None else None

    @property
    def mode(self) -> str | None:
        return self._modes.get(self._value("mode"))

    def _temperature(self, name: str) -> int | None:
        # In Grill (5) the app displays power levels 1..11 on the temperature channels.
        if self.mode is None or self._value("mode") == "5":
            return None
        return _integer(self._value(name))

    @property
    def current_temperature(self) -> int | None:
        return self._temperature("current_temperature")

    @property
    def target_temperature(self) -> int | None:
        return self._temperature("target_temperature")

    @property
    def remaining_time(self) -> int | None:
        hours, minutes = _integer(self._value("hours")), _integer(self._value("minutes"))
        if hours is None or minutes is None or not (0 <= hours <= 23 and 0 <= minutes <= 59):
            return None
        return hours * 60 + minutes

    def _flag(self, name: str) -> bool | None:
        return {"1": True, "0": False}.get(_norm(self._value(name)))

    @property
    def power(self) -> bool | None:
        return self._flag("power")

    def offers_control(self, name: str) -> bool:
        attr = self.config.get_attr_by_name(name)
        return bool(attr and attr.get("writable") is True and attr.get("operationType") == "I"
                    and self.config["remote"] and self.config["power"] and self.config["child_lock"])

    def can_control(self, name: str) -> bool:
        return (self.offers_control(name) and self.available and self.connected and self._haier.connected
                and self._flag("remote") is True and self._flag("child_lock") is False
                and self.power is not None and self.config[name] not in self._modifier_locks()[0])

    @property
    def temperature_limits(self) -> tuple[int, int, int] | None:
        """The current program's writable Celsius range from the app's mode-specific rule."""
        if self.power is not True or self.target_temperature is None or self._value("work_status") not in {"2", "3", "4"}:
            return None
        for rule in device_part(self.status_data).get("modifiers") or []:
            conditions = _field(rule, "trigger", "conditions", "condition")
            if conditions != [{"mode": self.config["mode"], "values": [self._value("mode")]}]:
                continue
            for action in rule.get("actions") or []:
                if _field(action, "name") != self.config["target_temperature"] or action.get("writable") is not True:
                    continue
                bounds = _field(action, "valueRange", "range")
                low, high, step = (_integer(_field(bounds, key)) for key in ("minValue", "maxValue", "step"))
                if low is not None and high is not None and step is not None and 0 < low <= high <= 300 and step > 0:
                    return low, high, step
        return None

    async def _write(self, name: str, value: str) -> None:
        code = self.config[name]
        if code is None or not self.can_control(name):
            raise HaierCommandLocked(translation_domain=C.DOMAIN, translation_key="oven_control_unavailable")
        await self._send_single_command({"commandName": code, "value": value})

    async def set_power(self, on: bool) -> None:
        await self._write("power", "1" if on else "0")

    async def set_temperature(self, value: float) -> None:
        limits = self.temperature_limits
        if not limits or not (limits[0] <= value <= limits[1]) or (value - limits[0]) % limits[2]:
            raise HaierCommandLocked(translation_domain=C.DOMAIN, translation_key="oven_temperature_unavailable")
        await self._write("target_temperature", str(int(value)))

    def create_entities_sensor(self) -> list:
        from .sensor import HaierOvenMode, HaierOvenRemaining, HaierOvenTarget, HaierOvenTemperature

        return [cls(self) for cls, keys in (
            (HaierOvenTemperature, ("current_temperature",)), (HaierOvenTarget, ("target_temperature",)),
            (HaierOvenMode, ("mode",)), (HaierOvenRemaining, ("hours", "minutes")),
        ) if all(self.config[key] is not None for key in keys)]

    def create_entities_binary_sensor(self) -> list:
        from .binary_sensor import HaierConnectivitySensor, HaierFirmwareUpdateSensor, HaierProblemSensor

        return [HaierConnectivitySensor(self), HaierFirmwareUpdateSensor(self), HaierProblemSensor(self)]

    def create_entities_switch(self) -> list:
        from .switch import HaierOvenPower

        return [HaierOvenPower(self)] if self.offers_control("power") else []

    def create_entities_number(self) -> list:
        from .number import HaierOvenSetpoint

        return [HaierOvenSetpoint(self)] if self.offers_control("target_temperature") else []
