"""Air conditioners."""
from __future__ import annotations

from homeassistant.components.climate.const import (
    PRESET_NONE,
    SWING_OFF,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.exceptions import HomeAssistantError

from . import config as CFG  # noqa: N812
from . import const as C  # noqa: N812
from .device import (
    HaierCommandLocked,
    HaierCommandRejected,
    HaierDevice,
    _described_attributes,
    _norm,
    _same_value,
    format_number,
    parsebool,
    parsefloat,
)
from .logger import _LOGGER


class HaierAC(HaierDevice):
    # Code 38, "Температура наружного блока" of 1.6.0 and of the fork 0.4-0.5:
    # on AS25S2SF2FA it stands still for days (1.6.1), so it is gone in both.
    RETIRED = {"sensor": ("outdoor_temperature",)}

    def __init__(
        self,
        backend_data: dict = None,
        **kwargs
    ) -> None:
        super().__init__(**kwargs)
        self.current_temperature: float | None = None
        self.target_temperature: float | None = None
        self.target_temperature_step: float = 1.0
        self.status = None
        self.mode = None
        self.fan_mode = None
        self.swing_horizontal_mode = None
        self.swing_mode = None
        self._preset_mode = None
        self.min_temperature = 7
        self.max_temperature = 35
        self.light_on = True
        self.sound_on = True
        self.quiet_on = False
        self.turbo_on = False
        self.health_on = False
        self.comfort_on = False
        self.cleaning_on = False
        self.antifreeze_on = False
        self.autohumidity_on = False
        self.eco_sensor = None
        # Set on an off unit: the cloud refuses it until the unit runs.
        self._deferred_temperature: float | None = None
        self._get_status(backend_data)

    @property
    def config(self) -> CFG.HaierACConfig:
        return self._config

    @property
    def preset_mode(self) -> str:
        if self._preset_mode not in ("none", "sleep", "boost"):
            return self._preset_mode
        elif self.quiet_on:
            return "sleep"
        elif self.turbo_on:
            return "boost"
        return "none"

    @preset_mode.setter
    def preset_mode(self, preset_mode: str) -> None:
        self._preset_mode = preset_mode

    def to_dict(self) -> dict:
        data = super().to_dict()
        data.update({
            "current_temperature": self.current_temperature,
            "target_temperature": self.target_temperature,
            "max_temperature": self.max_temperature,
            "min_temperature": self.min_temperature,
            "status": self.status,
            "mode": self.mode,
            "fan_mode": self.fan_mode,
            "swing_horizontal_mode": self.swing_horizontal_mode,
            "swing_mode": self.swing_mode,
            "preset_mode": self.preset_mode,
            "light_on": self.light_on,
            "sound_on": self.sound_on,
            "quiet_on": self.quiet_on,
            "turbo_on": self.turbo_on,
            "health_on": self.health_on,
            "comfort_on": self.comfort_on,
            "cleaning_on": self.cleaning_on,
            "antifreeze_on": self.antifreeze_on,
            "autohumidity_on": self.autohumidity_on,
            "eco_sensor": self.eco_sensor,
        })
        return data

    def _set_attribute_value(self, code: str, value: str) -> None:
        # "Тихий"/"Турбо" exist twice with one code (preset_mode_sleep + quiet,
        # preset_mode_boost + turbo). Only the first one used to be applied, so
        # the switches never followed the unit (#67).
        if value is None:
            return
        code = str(code)
        previous = self._raw.get(code)
        self._raw[code] = str(value)
        is_setpoint = code == str(self.config["target_temperature"])
        if self._deferred_temperature is not None and (
            (code == str(self.config["status"]) and _norm(value) == "1")
            or (is_setpoint and (previous is None or not _same_value(value, previous)))
        ):
            # Turned on elsewhere, or the cloud reports a setpoint of its own:
            # the value kept for our next turn-on is stale and must not pop up
            # later. A repeat of the setpoint the unit already had (a status
            # packet, a REST resync) is neither.
            self._deferred_temperature = None
        for attr in self.config.get_attrs_by_code(code):
            self._apply_attribute(attr, value)
        if is_setpoint and self._deferred_temperature is not None:
            # Keep showing the setpoint that goes out on turn-on.
            self.target_temperature = self._deferred_temperature

    def _apply_attribute(self, attr: CFG.Attribute, value: str) -> None:
        if attr.name == "current_temperature":
            self.current_temperature = parsefloat(value, self.current_temperature)
        elif attr.name == "status":
            self.status = 1 if parsebool(str(value)) else 0
        elif attr.name == "target_temperature":
            self.target_temperature = parsefloat(value, self.target_temperature)
        elif attr.name == "mode":
            self.mode = attr.get_item_name(value)
        elif attr.name == "fan_mode":
            self.fan_mode = attr.get_item_name(value)
        elif attr.name == "swing_horizontal_mode":
            self.swing_horizontal_mode = attr.get_item_name(value)
        elif attr.name == "swing_mode":
            self.swing_mode = attr.get_item_name(value)
        elif attr.name == "light":
            self.light_on = parsebool(attr.get_item_name(value))
        elif attr.name == "sound":
            self.sound_on = parsebool(attr.get_item_name(value))
        elif attr.name in ("quiet", "preset_mode_sleep"):
            self.quiet_on = parsebool(attr.get_item_name(value))
        elif attr.name in ("turbo", "preset_mode_boost"):
            self.turbo_on = parsebool(attr.get_item_name(value))
        elif attr.name == "health":
            self.health_on = parsebool(attr.get_item_name(value))
        elif attr.name == "comfort":
            self.comfort_on = parsebool(attr.get_item_name(value))
        elif attr.name == "cleaning":
            self.cleaning_on = parsebool(attr.get_item_name(value))
        elif attr.name == "antifreeze":
            self.antifreeze_on = parsebool(attr.get_item_name(value))
        elif attr.name == "autohumidity":
            self.autohumidity_on = parsebool(attr.get_item_name(value))
        elif attr.name == "eco_sensor":
            self.eco_sensor = attr.get_item_name(value)

    def _load_config_from_attributes(self, data: dict) -> None:
        self._config = CFG.HaierACConfig(self.device_model, self.hass.config.path(C.DOMAIN))
        sensors = (data.get("sensors") or {}).get("items") or []
        sensor_curr_temp = next(filter(lambda i: (
            isinstance(i, dict)
            and isinstance(i.get("value"), dict)
            and i.get("value", {}).get("description") == "indoorTemperature"
        ), sensors), {}).get("value", {}).get("name")
        attrs = _described_attributes(data)
        room_seen = False
        for attr in attrs:
            if attr.name == "current_temperature":
                if sensor_curr_temp is None:
                    # No indoorTemperature in sensors: the first one is the room
                    # temperature. Others are dropped rather than guessed at, or
                    # the room temperature would jump between them.
                    if room_seen:
                        continue
                    room_seen = True
                elif str(attr.code) != sensor_curr_temp:
                    # The other "room temperature" (code 38 on S2SF and PHP3) is
                    # not used: on AS25S2SF2FA it never changes (1.6.1).
                    # The indoor one is named in sensors.
                    continue
            self.config.attrs.append(attr)
        self.config.merge_attributes()
        self.config.apply_louver_layout(data)
        for attr in self.config.attrs:
            self._apply_value(attr.code, attr.current)
            if attr.name == "target_temperature" and attr.range:
                self.min_temperature = parsefloat(attr.range.min_value, self.min_temperature)
                self.max_temperature = parsefloat(attr.range.max_value, self.max_temperature)
                self.target_temperature_step = parsefloat(attr.range.step, 1.0) or 1.0
            _LOGGER.debug("%s: %s", self.device_name, attr)
        self.constraint.extend(
            item for item in data.get("constraint") or [] if isinstance(item, dict)
        )

    def _get_status(self, data: dict) -> dict:
        data = super()._get_status(data)
        if self.swing_horizontal_mode is None:
            self.swing_horizontal_mode = SWING_OFF
        if self.swing_mode is None:
            self.swing_mode = SWING_OFF
        if self.preset_mode is None:
            self.preset_mode = PRESET_NONE
        return data

    async def _send_commands(self, commands: list[dict]) -> None:
        await super()._send_commands(commands)
        # Keep modifiers honest until the unit echoes the new state.
        for command in commands:
            if command.get("commandName") is not None:
                self._raw[str(command["commandName"])] = str(command.get("value"))

    def _status_commands(self, value: str) -> list[dict]:
        if custom := self.config.get_command_by_name(f"status_{value}"):
            return custom
        attr = self.config.get_attr_by_name("status")
        if attr is None:
            return []
        # Profiles without a status mapping: on/off is 1/0 on every known unit.
        code = attr.get_item_code(value) or ("1" if value == "on" else "0")
        return self.constraint.apply([{"commandName": str(attr.code), "value": code}])

    def get_commands(self, name: str, value: str | bool) -> list[dict]:
        if name != "preset_mode":
            return super().get_commands(name, value)
        func = getattr(self, f"get_preset_mode_{value}", None)
        if func is not None:
            return func()
        return self.get_preset_mode_command(value)

    def get_preset_mode_none(self) -> list[dict]:
        if custom := self.config.get_command_by_name('preset_mode_none'):
            return custom
        # with the additional commands the cloud wants along, as for the other
        # presets and the "Тихий" switch
        return self.constraint.apply([{
            "commandName": str(attr.code),
            "value": attr.get_item_code("off", "0"),
        } for attr in filter(
            lambda a: a.name.startswith("preset_mode"),
            self.config.attrs
        )])

    def get_preset_mode_command(self, mode: str) -> list[dict]:
        if custom := self.config.get_command_by_name(f'preset_mode_{mode}'):
            return custom
        attr = self.config.get_attr_by_name(f"preset_mode_{mode}")
        return self.constraint.apply([{
            "commandName": str(attr.code),
            "value": attr.get_item_code("on", "1")
        }] if attr else [])

    def get_supported_features(self) -> ClimateEntityFeature:
        value = (
            ClimateEntityFeature.TARGET_TEMPERATURE |
            ClimateEntityFeature.TURN_OFF |
            ClimateEntityFeature.TURN_ON |
            ClimateEntityFeature.FAN_MODE
        )
        if self.config['swing_horizontal_mode'] is not None:
            value = value | ClimateEntityFeature.SWING_HORIZONTAL_MODE
        if self.config['swing_mode'] is not None:
            value = value | ClimateEntityFeature.SWING_MODE
        if self.config.preset_mode is True:
            value = value | ClimateEntityFeature.PRESET_MODE
        return ClimateEntityFeature(value)

    def get_hvac_modes(self) -> list[HVACMode]:
        modes = []
        for mode in self.config.get_values('mode'):
            try:
                modes.append(HVACMode(mode))
            except ValueError:
                pass
        return modes + [HVACMode.OFF]

    def get_fan_modes(self) -> list[str]:
        return self.config.get_values('fan_mode')

    def get_swing_horizontal_modes(self) -> list[str]:
        return self.config.get_values('swing_horizontal_mode')

    def get_swing_modes(self) -> list[str]:
        # "special" (code 9) is "только для специальной модели" per the cloud.
        return [m for m in self.config.get_values('swing_mode') if m != "special"]

    def get_preset_modes(self) -> list[str]:
        return ["none"] + self.config.get_preset_modes()

    def get_eco_sensor_options(self) -> list[str]:
        return self.config.get_values('eco_sensor')

    async def set_temperature(self, value: float) -> None:
        code = self.config['target_temperature']
        if code is None:
            raise HomeAssistantError(f"{self.device_name}: target temperature is not supported")
        try:
            self._check_writable("target_temperature")
        except HaierCommandLocked as err:
            if err.translation_key != "locked_off":
                raise
            # Alice and thermostat cards set the temperature of an off unit
            # ("включи на 24" may come temperature first). Keep it for switch_on.
            self._deferred_temperature = float(value)
            self.target_temperature = float(value)
            return
        await self._send_commands([
            {
                "commandName": code,
                "value": format_number(value),
            }
        ])
        self._deferred_temperature = None
        self.target_temperature = float(value)

    async def switch_on(self, value: str | None = None) -> None:
        # Without a mode asked for, the unit keeps the one it has. A mode this
        # profile does not know (self.mode is None) is left alone: sending
        # "auto" in its place changed the mode of the unit on a plain turn-on.
        mode = str(value) if value else self.mode
        status_code = str(self.config["status"])
        # The rules are read as they will be once the unit runs in that mode:
        # power on goes out together with the mode (1.1).
        assume = {status_code: "1"}
        mode_attr = self.config.get_attr_by_name("mode")
        if mode is not None and mode_attr is not None:
            assume[str(mode_attr.code)] = str(mode_attr.get_item_code(mode, mode))
        if mode is not None and (not self.status or mode != self.mode):
            # Turning on is never locked; the mode may be (a running unit, or
            # a value the rules do not allow).
            self._check_writable("mode", mode, assume=assume)
        mode_commands = self.get_commands("mode", mode) if mode is not None else []
        believed_on = bool(self.status)
        power_on = self._status_commands("on")
        if believed_on:
            # Only the power command itself: the cloud's power-on extras stop
            # the cleaning cycles, which a mode change must not do (1.6).
            power_on = [c for c in power_on if str(c.get("commandName")) == status_code]
        # A setpoint set while off goes out in the same group: sent after the
        # turn-on it was lost whenever the echo of "on" came first, which drops
        # a deferred value as stale (as 1.7 sends it).
        setpoint: list[dict] = []
        if (deferred := self._deferred_temperature) is not None:
            try:
                self._check_writable("target_temperature", assume=assume)
            except HaierCommandLocked as err:
                # The new mode does not take a setpoint (e.g. fan only).
                _LOGGER.debug("Dropping the deferred temperature: %s", err.translation_key)
                self._deferred_temperature = None
                self._show_unit_setpoint()
            else:
                setpoint = [{"commandName": self.config["target_temperature"], "value": format_number(deferred)}]
        # Always send status=on: our idea of "already on" may be stale, and
        # then a mode change alone does not start the unit (MaxSezin fork).
        try:
            await self._send_commands([
                *power_on,
                *mode_commands,
                *setpoint,
            ])
        except HaierCommandRejected:
            if not (believed_on and (mode_commands or setpoint)):
                raise
            # The cloud may refuse "power on" for a unit that runs already
            # (errNo 22), and the rest of the group with it: without it.
            _LOGGER.debug("%s: power on refused for a running unit, sending the rest alone", self.device_name)
            await self._send_commands([*mode_commands, *setpoint])
        self.status = 1
        if mode_commands:
            self.mode = mode
        if setpoint:
            # Kept until the group went out: a turn-on that failed sends it next time.
            self._deferred_temperature = None
            self.target_temperature = deferred

    def _show_unit_setpoint(self) -> None:
        """Show the setpoint the unit itself reported last."""
        raw = self._raw.get(str(self.config["target_temperature"]))
        if (value := parsefloat(raw)) is not None:
            self.target_temperature = value

    async def switch_off(self) -> None:
        await self._send_commands([
            *self._status_commands("off"),
        ])
        self.status = 0

    async def set_fan_mode(self, value: str) -> None:
        self._check_writable("fan_mode", value)
        if commands := self.get_commands("fan_mode", value):
            await self._send_commands(commands)
            self.fan_mode = value

    async def set_swing_horizontal_mode(self, value: str) -> None:
        self._check_writable("swing_horizontal_mode", value)
        if commands := self.get_commands("swing_horizontal_mode", value):
            await self._send_commands(commands)
            self.swing_horizontal_mode = value

    async def set_swing_mode(self, value: str) -> None:
        self._check_writable("swing_mode", value)
        if commands := self.get_commands("swing_mode", value):
            await self._send_commands(commands)
            self.swing_mode = value

    async def set_preset_mode(self, value: str) -> None:
        if value != PRESET_NONE:
            self._check_writable(f"preset_mode_{value}")
        if commands := self.get_commands("preset_mode", value):
            await self._send_commands(commands)
            self.preset_mode = value
            if value in (PRESET_NONE, "sleep", "boost"):
                # The getter reads these two until the unit echoes them.
                self.quiet_on = value == "sleep"
                self.turbo_on = value == "boost"

    async def set_light_on(self, value: bool) -> None:
        self._check_writable("light")
        if commands := self.get_commands("light", value):
            await self._send_commands(commands)
            self.light_on = value

    async def set_sound_on(self, value: bool) -> None:
        self._check_writable("sound")
        if commands := self.get_commands("sound", value):
            await self._send_commands(commands)
            self.sound_on = value

    async def set_quiet_on(self, value: bool) -> None:
        self._check_writable("quiet")
        if commands := self.get_commands("quiet", value):
            await self._send_commands(commands)
            self.quiet_on = value

    async def set_health_on(self, value: bool) -> None:
        self._check_writable("health")
        if commands := self.get_commands("health", value):
            await self._send_commands(commands)
            self.health_on = value

    async def set_turbo_on(self, value: bool) -> None:
        self._check_writable("turbo")
        if commands := self.get_commands("turbo", value):
            await self._send_commands(commands)
            self.turbo_on = value

    async def set_comfort_on(self, value: bool) -> None:
        self._check_writable("comfort")
        if commands := self.get_commands("comfort", value):
            await self._send_commands(commands)
            self.comfort_on = value

    async def set_cleaning_on(self, value: bool) -> None:
        self._check_writable("cleaning")
        if commands := self.get_commands("cleaning", value):
            await self._send_commands(commands)
            self.cleaning_on = value

    async def set_antifreeze_on(self, value: bool) -> None:
        self._check_writable("antifreeze")
        if commands := self.get_commands("antifreeze", value):
            await self._send_commands(commands)
            self.antifreeze_on = value

    async def set_autohumidity_on(self, value: bool) -> None:
        self._check_writable("autohumidity")
        if commands := self.get_commands("autohumidity", value):
            await self._send_commands(commands)
            self.autohumidity_on = value

    async def set_eco_sensor(self, value: str) -> None:
        self._check_writable("eco_sensor", value)
        if commands := self.get_commands("eco_sensor", value):
            await self._send_commands(commands)
            self.eco_sensor = value

    def create_entities_climate(self) -> list:
        from . import climate
        return [climate.HaierACEntity(self)]

    def create_entities_switch(self) -> list:
        """Switches for what the app offers (1.5).

        The rest (e.g. "Звуковой сигнал", which AS25S2SF2FA refuses) goes to
        hidden_entities, so earlier versions' entities leave the registry.
        Without screens in the description everything is offered.
        """
        from . import switch
        entities, self.hidden_entities = [], []
        for cls in switch.AC_SWITCHES:
            code = self.config[cls.feature]
            if code is None:
                continue
            if self.offered_by_app(code):
                entities.append(cls(self))
            else:
                self.hidden_entities.append(("switch", f"{self.device_id}_{self.device_model}_{cls.feature}"))
        return entities

    def create_entities_select(self) -> list:
        from . import select
        entities = []
        if self.config['eco_sensor'] is not None:
            entities.append(select.HaierACEcoSensorSelect(self))
        return entities

    def create_entities_sensor(self) -> list:
        from . import sensor
        entities = []
        if self.config['current_temperature'] is not None:
            # the room temperature as a sensor of its own (1.6): history, statistics
            entities.append(sensor.HaierACTemperatureSensor(self))
        return entities

    def create_entities_binary_sensor(self) -> list:
        from . import binary_sensor
        return [
            binary_sensor.HaierProblemSensor(self),
            binary_sensor.HaierFilterSensor(self),
            binary_sensor.HaierConnectivitySensor(self),
            binary_sensor.HaierFirmwareUpdateSensor(self),
        ]
