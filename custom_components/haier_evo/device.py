"""Base class of a Haier Evo device and the helpers its subclasses share."""
from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, ClassVar

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo

from . import config as CFG  # noqa: N812
from . import const as C  # noqa: N812
from .logger import _LOGGER

if TYPE_CHECKING:
    from .api import Haier


class HaierCommandError(HomeAssistantError):
    """Command could not be delivered to the Haier Evo cloud."""


class HaierCommandNotSent(HaierCommandError):
    """No connection became available, so the command was never written."""


# Alarms that ask for maintenance, not report a fault.
FILTER_ALARM = "filterCleaningAlarm"
MAINTENANCE_ALARMS = frozenset({FILTER_ALARM})
# Haier's "alarms cleared" marker, not an alarm (1.6).
ALARM_CANCEL = "alarmCancel"


def device_part(data: Any) -> dict:
    """The part of a description with info, settings and attributes.

    Washers and dryers in the cloud's current format nest it in
    smartDeviceControl, next to the app's screens; other devices have it at
    the top (1.6.4).
    """
    data = data if isinstance(data, dict) else {}
    inner = data.get("smartDeviceControl")
    return inner if isinstance(inner, dict) else data


def _priority(rule: dict) -> int:
    """The order of a cloud rule; a priority that is no number counts as 0 (1.6.3)."""
    try:
        return int(rule.get("priority") or 0)
    except (TypeError, ValueError):
        return 0


def firmware_text(value: Any) -> str | None:
    """A firmware version as the device registry takes it: text or None (1.6.3)."""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, int | float) and not isinstance(value, bool) and value == value and abs(value) != float("inf"):
        return str(value)
    return None


class HaierCommandRejected(HaierCommandError):
    """The cloud answered a command with a non-zero errNo, and the unit does not have the values."""


class HaierCommandLocked(HomeAssistantError):
    """The cloud marks the attribute read-only in the current state (modifiers)."""



def _same_value(current: Any, wanted: Any) -> bool:
    a, b = _norm(current), _norm(wanted)
    try:
        return float(a) == float(b)  # "24.00" == "24"
    except ValueError:
        return a == b



class HaierDevice:
    # unique_id suffixes, by platform, of entities earlier versions created and
    # no version offers any more: removed from the entity registry at setup.
    RETIRED: ClassVar[dict[str, tuple[str, ...]]] = {}
    # Commands set absolute values (a setpoint, a mode): repeating one is
    # harmless, unless a newer one went to the same attribute since.
    absolute_commands: ClassVar[bool] = True

    def __init__(
        self,
        haier: Haier,
        device_mac: str,
        device_serial: str | None = None,
        device_title: str | None = None,
        backend_data: dict | None = None,
    ) -> None:
        self._haier = haier
        self.device_id = device_mac
        self.device_serial = device_serial
        self.device_name = device_title
        self.device_model = "UNKNOWN"
        self.device_type = "UNKNOWN"
        self.sw_version = None
        self._listeners: list[Callable[[], None]] = []
        self._online = True
        # False while the values come from the saved descriptions of an earlier
        # start, not from the cloud (1.6): they are shown as unavailable
        # rather than as live, often hours old (the start of 2026-11-04).
        self.synced = True
        self._config = None
        self._status_data = backend_data
        self._signature: tuple = ()
        self.alarms: list[str] = []
        # deviceConnectionStatusEvent: the module's link to the cloud ("GOOD")
        self.link_status: str | None = None
        # Last push or command (monotonic): a REST snapshot older than this is stale.
        self.changed_at = 0.0
        # (platform, unique_id) of entities this device does not offer, though
        # earlier versions created them (filled by create_entities_*).
        self.hidden_entities: list[tuple[str, str]] = []
        # Raw attribute values by code, as the cloud sends them: modifiers are
        # written against these, not against the parsed state.
        self._raw: dict[str, str] = {}

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}("
            f"name={self.device_name!r},"
            f"model={self.device_model!r},"
            f"config={self.config!r}"
            f")"
        )

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(C.DOMAIN, self.device_id)},
            name=self.device_name,
            sw_version=self.sw_version,
            model=self.device_model,
            manufacturer="Haier",
        )

    @property
    def device_mac(self) -> str:
        return self.device_id

    @property
    def available(self) -> bool:
        """Its state is live and the cloud connection is alive.

        The cloud's ONLINE/OFFLINE flaps for units that work (1.4): it is shown
        by the connectivity sensor only. Unavailable entities would leave holes
        in the history, and Home Assistant skips them in service calls, so an
        automation would not reach a working unit the cloud calls OFFLINE.
        """
        return self.synced and self._haier.cloud_available

    @available.setter
    def available(self, value: bool | str) -> None:
        if isinstance(value, bool):
            self._online = value
        else:
            self._online = str(value).upper() != "OFFLINE"

    @property
    def status_data(self) -> dict:
        return self._status_data

    @property
    def hass(self) -> HomeAssistant:
        return self._haier.hass

    @property
    def config(self) -> CFG.HaierDeviceConfig:
        return self._config

    @property
    def constraint(self) -> CFG.Constraint:
        return self.config.constraint

    def to_dict(self) -> dict:
        return {
            "available": self.available,
            "synced": self.synced,
            "connected": self.connected,
            "link_status": self.link_status,
            "device_id": self.device_id,
            "device_mac": self.device_mac,
            "device_name": self.device_name,
            "device_serial": self.device_serial,
            "device_model": self.device_model,
            "sw_version": self.sw_version,
            "config": self.config.to_dict() if self.config else None,
            "alarms": self.alarms,
            "backend_data": self.status_data,
        }

    def _set_alarms(self, alarms: Any) -> None:
        # Live units send an empty list; entries are taken as alarm names
        # ("indoorFanErr") or dicts carrying one, since no live alarm was seen yet.
        names = []
        for alarm in alarms if isinstance(alarms, list) else []:
            if isinstance(alarm, dict):
                alarm = alarm.get("name") or alarm.get("alarmName") or alarm.get("code")
            if alarm and str(alarm) != ALARM_CANCEL and str(alarm) not in names:
                names.append(str(alarm))
        self.alarms = names

    @property
    def connected(self) -> bool:
        """The cloud reports the device ONLINE (the connectivity sensor)."""
        return self._online

    @property
    def problem(self) -> bool:
        """A fault; a reminder to clean the filter has a sensor of its own."""
        return any(alarm not in MAINTENANCE_ALARMS for alarm in self.alarms)

    @property
    def filter_cleaning(self) -> bool:
        return FILTER_ALARM in self.alarms

    @property
    def _firmware(self) -> dict:
        settings = device_part(self.status_data).get("settings")
        firmware = settings.get("firmware") if isinstance(settings, dict) else None
        return firmware if isinstance(firmware, dict) else {}

    @property
    def firmware_update(self) -> bool | None:
        """The cloud says a newer firmware is waiting (the app installs it)."""
        value = self._firmware.get("updateRequired")
        return value if isinstance(value, bool) else None

    @property
    def firmware_auto_update(self) -> bool | None:
        auto = self._firmware.get("autoUpdate")
        value = auto.get("enabled") if isinstance(auto, dict) else None
        return value if isinstance(value, bool) else None

    def _cloud_attribute(self, code: Any) -> dict:
        return next((
            attr for attr in device_part(self.status_data).get("attributes") or []
            if isinstance(attr, dict) and str(attr.get("name")) == str(code)
        ), {})

    def app_title(self, code: Any, fallback: str | None = None) -> str | None:
        """What the Haier app calls an attribute (1.5).

        Without a screen for it: the cloud's flat-list name, else the fallback.
        The flat list names some attributes wrongly (31 "Авто влажность" is
        "Самоочистка" in the app), but entity names of 1.7 come from it: a
        change would rename the entities of every unit without app screens.
        """
        if code is None:
            return fallback
        if title := CFG.app_screens(self.status_data).get(str(code)):
            return title
        if (title := str(self._cloud_attribute(code).get("description") or "").strip()):
            return title
        return fallback

    def offered_by_app(self, code: Any) -> bool:
        """False when the app has screens for this device, but none for the attribute."""
        screens = CFG.app_screens(self.status_data)
        return not screens or str(code) in screens

    def cloud_value_name(self, code: Any, value: Any) -> str:
        """The cloud's own (Russian) name of a list value, e.g. 2/"6" -> "Вентилятор"."""
        values = self._cloud_attribute(code).get("list")
        for item in (values.get("data") if isinstance(values, dict) else None) or []:
            if isinstance(item, dict) and _norm(item.get("data")) == _norm(value):
                return str(item.get("name") or value)
        return str(value)

    def _modifier_locks(
        self, assume: dict[str, str] | None = None
    ) -> tuple[dict[str, str], dict[str, set[str]]]:
        """Read-only codes (code -> reason) and value whitelists right now.

        The cloud ships "modifiers": rules that make attributes read-only while
        the unit is off, cleaning, in a given mode or reporting an alarm. The
        unit silently ignores such commands, so they are refused up front.
        assume: raw values (code -> value) the same command sets first, e.g.
        power on together with a new mode (1.1).
        """
        locked: dict[str, str] = {}
        allowed: dict[str, set[str]] = {}
        # Rules go by priority, and the first firing rule that rewrites a field
        # decides it, "writable": true included (live units use priority 0 for
        # the "unit is off" rule, which has to win).
        decided: set[str] = set()
        raw = {**self._raw, **{str(k): str(v) for k, v in (assume or {}).items()}}
        modifiers = device_part(self.status_data).get("modifiers")
        rules = [r for r in modifiers if isinstance(r, dict)] if isinstance(modifiers, list) else []
        for rule in sorted(rules, key=_priority):
            trigger = rule.get("trigger") if isinstance(rule.get("trigger"), dict) else {}
            reason = self._modifier_reason(trigger, raw)
            if reason is None:
                continue
            for action in rule.get("actions") or []:
                if not isinstance(action, dict):
                    continue
                code = str(action.get("name"))
                fields = str(action.get("rewriteFields") or "")
                if "W" in fields and code not in decided:
                    decided.add(code)
                    if action.get("writable") is False:
                        locked[code] = reason
                if "V" in fields and code not in allowed:
                    data = ((action.get("valueRange") or {}).get("list") or {}).get("data") or []
                    if values := {_norm(i.get("data")) for i in data if isinstance(i, dict)}:
                        allowed[code] = values
        return locked, allowed

    def _modifier_reason(self, trigger: dict, raw: dict[str, str] | None = None) -> str | None:
        """Why a rule fires, or None if it does not.

        "off" (the unit is off), "alarm", "mode" (the operation mode), or
        "by:<code>" for another attribute (a feature that excludes this one).
        """
        raw = self._raw if raw is None else raw
        status_code = str(self.config["status"])
        mode_code = str(self.config["mode"])
        results = []
        for cond in (trigger.get("conditions") or {}).get("condition") or []:
            code = str(cond.get("mode"))
            values = {_norm(v) for v in cond.get("values") or []}
            matched = code in raw and _norm(raw[code]) in values
            reason = "off" if code == status_code else "mode" if code == mode_code else f"by:{code}"
            results.append((matched, reason))
        if alarms := set(trigger.get("alarms") or []):
            # A reminder to clean the filter is no fault: the app does not lock
            # anything for it, and neither does this (1.3).
            results.append((bool(alarms & (set(self.alarms) - MAINTENANCE_ALARMS)), "alarm"))
        if not results:
            return None
        if str(trigger.get("operator") or "AND").upper() == "OR":
            return next((reason for matched, reason in results if matched), None)
        if all(matched for matched, _ in results):
            return results[0][1]
        return None

    def _check_writable(self, name: str, value: str | None = None, assume: dict | None = None) -> None:
        attr = self.config.get_attr_by_name(name)
        if attr is None:
            return
        code = str(attr.code)
        locked, allowed = self._modifier_locks(assume)
        placeholders = {
            "device": str(self.device_name),
            "attribute": str(self.app_title(code, attr.description or name)),
        }
        if code in locked:
            reason = locked[code]
            extra: dict[str, str] = {}
            if reason == "mode":
                mode_code = str(self.config["mode"])
                current = {**self._raw, **{str(k): str(v) for k, v in (assume or {}).items()}}
                extra["mode"] = self.cloud_value_name(mode_code, current.get(mode_code, ""))
            elif reason.startswith("by:"):
                cause = reason[3:]
                reason, extra["cause"] = "by", str(self.app_title(cause, cause))
            raise HaierCommandLocked(
                translation_domain=C.DOMAIN,
                translation_key=f"locked_{reason}",
                translation_placeholders={**placeholders, **extra},
            )
        if value is not None and code in allowed:
            item = attr.get_item_code(str(value))
            if item is not None and _norm(item) not in allowed[code]:
                names = [i.name for i in attr.list if _norm(i.value) in allowed[code] and not i.hidden]
                raise HaierCommandLocked(
                    translation_domain=C.DOMAIN,
                    translation_key="value_not_allowed",
                    translation_placeholders={
                        **placeholders, "value": str(value), "allowed": ", ".join(names) or "—",
                    },
                )

    @property
    def alarm_texts(self) -> list[str]:
        """Human-readable alarms from the cloud's own table (errors.alarmToResult)."""
        def part(value: Any, key: str) -> dict:
            value = value.get(key) if isinstance(value, dict) else None
            return value if isinstance(value, dict) else {}

        errors = part(device_part(self.status_data), "errors")
        table = part(errors, "alarmToResult")
        unknown = part(errors, "undefinedAlarm").get("text") or "Unknown error"
        return [str(part(table, alarm).get("text") or f"{unknown} ({alarm})") for alarm in self.alarms]

    def _get_status(self, data: dict) -> dict:
        self._status_data = data = (data or {})
        part = device_part(data)
        # "status" and "settings" may come as null: read them as "not there".
        # Not "info": the model in it is a part of every unique_id,
        # and a unit described without it would come up a second time as
        # "<mac>_AC", next to the entities it already has. Such a description
        # is unreadable, and the unit is skipped.
        info = part.get("info", {})
        if not isinstance(info, dict):
            raise ValueError("the description has no device info")
        self.device_serial = info.get("serialNumber") or self.device_serial
        # As 1.7 (info.setdefault("model", "AC")): "AC" only when the key is
        # missing; an empty model stays empty, or every unique_id "<mac>__..."
        # of such a unit would turn into "<mac>_AC_...".
        device_model = info.get("model", "AC")
        device_model = "AC" if device_model is None else str(device_model)
        device_model = device_model.replace('-', '').replace('/', '')[:11]
        self.device_model = device_model
        self.available = part.get("status") or "ONLINE"
        settings = part.get("settings") or {}
        self.device_name = (settings.get("name") or {}).get("name") or self.device_name
        self.sw_version = firmware_text((settings.get("firmware") or {}).get("value"))
        self._set_alarms(part.get("alarms"))
        self._signature = self.description_signature(data)
        # read config and current values
        self._load_config_from_attributes(data)
        return data

    @staticmethod
    def description_signature(data: Any) -> tuple:
        """What the entities are built from: model, firmware, attribute codes and rules.

        Values and alarms are not a part of it, they change all the time.
        """
        if not isinstance(data, dict):
            return ()
        data = device_part(data)
        info = data.get("info") if isinstance(data.get("info"), dict) else {}
        settings = data.get("settings") if isinstance(data.get("settings"), dict) else {}
        firmware = settings.get("firmware") if isinstance(settings.get("firmware"), dict) else {}
        attributes = sorted(
            str(attr.get("name")) for attr in data.get("attributes") or [] if isinstance(attr, dict)
        )
        modifiers = len(data.get("modifiers") or []) if isinstance(data.get("modifiers"), list) else 0
        return (str(info.get("model")), str(firmware.get("value")), tuple(attributes), modifiers)

    def described_differently(self, data: Any) -> bool:
        """A fresh REST description no longer matches the one the entities came from."""
        signature = self.description_signature(data)
        return bool(signature) and bool(self._signature) and signature != self._signature

    def update_from_backend(self, data: dict, *, requested_at: float | None = None) -> None:
        """Apply a fresh REST status without rebuilding the config."""
        if not isinstance(data, dict) or self.config is None:
            return
        self._status_data = data
        part = device_part(data)
        self.available = part.get("status") or "ONLINE"
        self._set_alarms(part.get("alarms"))
        for attr in part.get("attributes") or []:
            if isinstance(attr, dict) and attr.get("currentValue") is not None:
                self._apply_value(str(attr.get("name")), attr.get("currentValue"))
        self.synced = True

    def _load_config_from_attributes(self, data: dict) -> None:
        pass

    def _set_attribute_value(self, code: str, value: str) -> None:
        pass

    def _apply_value(self, code: Any, value: Any) -> None:
        """A value from the cloud: parsed by the device, and kept raw for the rules."""
        if value is None:
            return
        self._set_attribute_value(str(code), value)
        self._raw[str(code)] = str(value)

    def _remember_pushed(self, properties: dict, alarms: Any = None) -> None:
        """Write pushed values into the status snapshot the cache saves.

        REST comes every 15 minutes; without this a start from the cache showed
        the state of the last snapshot, not what the entities already showed.
        """
        if not isinstance(self._status_data, dict):
            return
        data = device_part(self._status_data)
        by_name = {
            str(attr.get("name")): attr
            for attr in data.get("attributes") or []
            if isinstance(attr, dict)
        }
        for key, value in properties.items():
            if value is not None and (attr := by_name.get(str(key))) is not None:
                attr["currentValue"] = value
        if alarms is not None:
            data["alarms"] = alarms

    def _handle_status_update(self, received_message: dict) -> None:
        statuses = (received_message.get("payload") or {}).get("statuses") or []
        self.changed_at = time.monotonic()
        try:
            for status in statuses:
                if not isinstance(status, dict):
                    continue
                properties = status.get("properties")
                properties = properties if isinstance(properties, dict) else {}
                for key, value in properties.items():
                    self._apply_value(key, value)
                if properties:
                    # The cloud pushes the whole state of a unit right after a
                    # connection opens: that is live.
                    self.synced = True
                if "alarms" in status:
                    self._set_alarms(status["alarms"])
                self._remember_pushed(properties, status.get("alarms"))
            # Availability is deviceStatusEvent's (and REST's) business: a status
            # packet trailing an OFFLINE must not bring the unit back.
        finally:
            # Whatever was applied before a malformed entry still reaches the UI.
            self.notify()

    def _handle_device_status_update(self, received_message: dict) -> None:
        status = (received_message.get("payload") or {}).get("status")
        # A REST snapshot requested before this event is older than it.
        self.changed_at = time.monotonic()
        if status is not None:
            self.available = status
            if isinstance(self._status_data, dict):
                device_part(self._status_data)["status"] = str(status)
        self.notify()

    def _handle_info(self, received_message: dict) -> None:
        payload = received_message.get("payload") or {}
        self.sw_version = payload.get("swVersion") or self.sw_version

    async def _send_message(self, message: dict) -> None:
        await self._haier.async_send(message)
        self.changed_at = time.monotonic()

    async def _send_commands(self, commands: list[dict]) -> None:
        await self._send_group_command(commands)

    async def _send_group_command(self, commands: list[dict]) -> None:
        if not commands:
            return
        if not self.config.command_name:
            for command in commands:
                await self._send_single_command(command)
            return
        await self._send_message({
            "action": "operation",
            "macAddress": self.device_id,
            "commandName": self.config.command_name,
            "commands": commands,
            "trace": str(uuid.uuid4()),
        })

    async def _send_single_command(self, command: dict) -> None:
        await self._send_message({
            "action": "command",
            "macAddress": self.device_id,
            "command": command,
            "trace": str(uuid.uuid4()),
        })

    def get_commands(self, name: str, value: str | bool) -> list[dict]:
        value = str({True: "on", False: "off", None: "off"}.get(value, value))
        if custom := self.config.get_command_by_name(f"{name}_{value}"):
            return custom
        attr = self.config.get_attr_by_name(name)
        code = attr.get_item_code(value) if attr else None
        if code is None:
            # Used to go out as the string "None".
            _LOGGER.warning("%s: no command for %s=%s", self.device_name, name, value)
            return []
        return self.constraint.apply([{
            "commandName": str(attr.code),
            "value": code,
        }])

    def on_message(self, message_dict: dict) -> None:
        message_type = message_dict.get("event", "")
        if message_type == "status":
            self._handle_status_update(message_dict)
        elif message_type == "command_response":
            pass
        elif message_type == "info":
            self._handle_info(message_dict)
        elif message_type == "deviceStatusEvent":
            self._handle_device_status_update(message_dict)
        elif message_type == "deviceConnectionStatusEvent":
            # The module's link to the cloud ("GOOD" seen so far), sent on connect.
            payload = message_dict.get("payload")
            if isinstance(payload, dict):
                self.link_status = payload.get("status")
                self.notify()
        else:
            _LOGGER.debug("Unknown message type %r", message_type)

    def notify(self) -> None:
        """Push the current state to entities. Event loop only."""
        for listener in list(self._listeners):
            listener()

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(listener)

        def remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return remove

    def retired_unique_ids(self, platform: str) -> list[str]:
        """unique_ids of the entities on a platform that are to be removed."""
        retired = [f"{self.device_id}_{self.device_model}_{suffix}" for suffix in self.RETIRED.get(platform, ())]
        return retired + [unique_id for where, unique_id in self.hidden_entities if where == platform]

    # noinspection PyMethodMayBeStatic
    def create_entities_climate(self) -> list:
        return []

    # noinspection PyMethodMayBeStatic
    def create_entities_switch(self) -> list:
        return []

    # noinspection PyMethodMayBeStatic
    def create_entities_select(self) -> list:
        return []

    # noinspection PyMethodMayBeStatic
    def create_entities_sensor(self) -> list:
        return []

    # noinspection PyMethodMayBeStatic
    def create_entities_binary_sensor(self) -> list:
        return []

    # noinspection PyMethodMayBeStatic
    def create_entities_water_heater(self) -> list:
        return []

    # noinspection PyMethodMayBeStatic
    def create_entities_button(self) -> list:
        return []

    @classmethod
    def create(
        cls,
        haier: Haier,
        device_type: str,
        device_mac: str,
        device_serial: str | None = None,
        device_title: str | None = None,
        backend_data: dict | None = None,
    ) -> HaierDevice:
        from .device_ac import HaierAC
        from .device_laundry import LAUNDRY_TYPES, HaierLaundry
        from .device_ref import HaierREF
        from .device_wh import HaierWH
        from .device_wm import HaierWM

        device_cls: type[HaierDevice]
        if device_type in LAUNDRY_TYPES and HaierLaundry.describes(backend_data):
            device_cls = HaierLaundry  # the current format of washers and dryers
        else:
            device_cls = {
                "AC": HaierAC,
                "REF": HaierREF,
                "WM": HaierWM,
                "WH": HaierWH,
            }.get(device_type, cls)
        if device_cls is cls:
            _LOGGER.warning("Unknown device type: %s", device_type)
        return device_cls(
            haier=haier,
            device_mac=device_mac,
            device_serial=device_serial,
            device_title=device_title,
            backend_data=backend_data,
        )


def _described_attributes(data: dict) -> list[CFG.Attribute]:
    """The attributes of a status, by code; "attributes": null and junk entries are skipped."""
    return sorted(
        (CFG.Attribute(item) for item in device_part(data).get("attributes") or [] if isinstance(item, dict)),
        key=lambda attr: attr.code,
    )


def parsebool(value) -> bool:
    if value in ("on", 1, True, "true", "enable", "1"):
        return True
    return False


_norm = CFG.norm_value


def parsefloat(value, default: float | None = None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def format_number(value: float | int | str) -> str:
    """24.0 -> "24", 24.5 -> "24.5" (the app sends integers as such)."""
    number = float(value)
    return str(int(number)) if number.is_integer() else str(number)
