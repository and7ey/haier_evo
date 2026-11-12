"""Washing machines, washer-dryers and dryers in the cloud's current format.

1.6.4 (reading) and 1.7.0 (control, beta), on asyncio. The
description of such a machine nests its attributes, without names, in
smartDeviceControl; what a code means is read from the app's screens next to
it, so models and kinds with other codes work alike.
"""
from __future__ import annotations

import asyncio
import re
import time
import uuid
from typing import TYPE_CHECKING, Any

from homeassistant.exceptions import ServiceValidationError

from .laundry_events import LaundryEvents
from .programs import active_program_details, build_catalogue, program_settings

if TYPE_CHECKING:
    from .coordinator import HaierLaundryCoordinator

from . import config as CFG  # noqa: N812
from . import const as C  # noqa: N812
from .device import HaierCommandLocked, HaierDevice, _norm, device_part, parsefloat

# washing machines, washer-dryers and dryers
LAUNDRY_TYPES = ("WM", "WD", "DRY", "TD")
# the state of a program as the cloud names it (control.currentProgram / program / events)
LAUNDRY_PROGRAM_STATES = {
    "IN_PROGRESS": "running", "STARTED": "running", "PAUSED": "paused",
    "FINISHED": "finished", "CANCELED": "cancelled", "CANCELLED": "cancelled",
}
LAUNDRY_STATES = ("off", "idle", "running", "paused", "finished", "cancelled", "care")
# the states the app offers each action in
LAUNDRY_ACTION_STATES = {
    "start": ("idle", "finished", "cancelled", "off"),
    "pause": ("running", "care"),
    "resume": ("paused",),
    "cancel": ("running", "paused", "care"),
}
LAUNDRY_ACTIONS = ("start", "pause", "resume", "cancel")
_MEANING = re.compile(r"[A-Za-z][A-Za-z0-9]{2,40}")


def _d(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def laundry_codes(data: Any) -> dict[str, str]:
    """{meaning: attribute code} from the app's screens in a description.

    The attributes of a washer or a dryer come without names, and the codes
    differ between models (remaining hours: 31 on HW60, 32 on HW90); the app's
    screens link to them as {"name": code, "description": "remainingTimeHH"}.
    The first link of a meaning wins.
    """
    known = {str(a.get("name")) for a in device_part(data).get("attributes") or [] if isinstance(a, dict)}
    found: dict[str, str] = {}

    def walk(node: Any, depth: int = 0) -> None:
        if depth > 12:
            return
        if isinstance(node, dict):
            code, meaning = node.get("name"), node.get("description")
            if (isinstance(code, str) and code in known and isinstance(meaning, str)
                    and _MEANING.fullmatch(meaning) and meaning != "not found"):
                found.setdefault(meaning, code)
            for key, value in node.items():
                if key not in ("smartDeviceControl", "attributes"):
                    walk(value, depth + 1)
        elif isinstance(node, list):
            for value in node:
                walk(value, depth + 1)

    walk(_d(data))
    return found


def laundry_screen(data: Any) -> dict:
    """The monitoring screen: a washer has it in allProgram, a dryer in device."""
    data = _d(data)
    for parent in (data.get("device"), data.get("allProgram")):
        screen = parent.get("monitoringScreen") if isinstance(parent, dict) else None
        if isinstance(screen, dict):
            return screen
    return {}


def laundry_phase_titles(data: Any) -> dict[str, str]:
    """{value of the cycle phase: the app's title}."""
    screen = laundry_screen(data)
    phase = _d(screen.get("phase"))
    titles = phase.get("cyclePhaseValueToTitle")
    out = (
        {str(k): str(v) for k, v in titles.items() if isinstance(v, str) and v.strip()}
        if isinstance(titles, dict) else {}
    )
    for block, key in (("weighing", "weightAttrValue"), ("finishing", "finishingAttrValue"),
                       ("finishing", "cyclePhaseValue"), ("freshAirSystem", "freshAirSystemAttrValue")):
        for parent in (screen, phase):
            part = parent.get(block)
            if isinstance(part, dict) and part.get(key) not in (None, "") and isinstance(part.get("title"), str):
                out.setdefault(str(part[key]), part["title"])
    return out


def _value_allowed(field: dict, value: Any) -> bool:
    """A selected value is one the program's definition allows (its list or range)."""
    value_range = _d(field.get("valueRange"))
    options = _d(value_range.get("list")).get("data")
    if isinstance(options, list):
        return str(value) in {str(o.get("data")) for o in options if isinstance(o, dict)}
    bounds = _d(value_range.get("range")).get("data")
    if isinstance(bounds, dict):
        number = parsefloat(value)
        low, high = parsefloat(bounds.get("minValue")), parsefloat(bounds.get("maxValue"))
        return number is not None and (low is None or number >= low) and (high is None or number <= high)
    return True


def laundry_programs(data: Any) -> list[dict]:
    """The programs a washer or dryer is started with, in the app's order:
    [{"title", "link", "group", "commands"}].

    The app's catalogue (allProgram of a washer, device.programDetails of a
    dryer) links every program to its definition in
    smartDeviceControl.businessAttributes: a group command with a value for
    each of its attributes, the program's selected values over the defaults.
    Sending it starts the program. An attribute without a value (the delayed
    start's status: the machine sets it) is not sent, as the fork
    JampireX/haier_evo does on HW60. A program without a definition is not
    offered; of two with one title, the first.
    """
    data = _d(data)
    definitions: dict[str, dict] = {}
    for item in device_part(data).get("businessAttributes") or []:
        params = _d(_d(item).get("commandParameters"))
        if (isinstance(_d(item).get("name"), str) and params.get("name") not in (None, "")
                and isinstance(params.get("attrNameList"), list)):
            definitions[item["name"]] = params
    catalogue = []
    for block in _d(data.get("allProgram")).get("blocks") or []:
        for program in _d(block).get("programs") or []:
            catalogue.append((_d(_d(program).get("preview")).get("name"), _d(program).get("programConfig")))
    details = _d(_d(data.get("device")).get("programDetails")).get("details")
    for program in details if isinstance(details, list) else []:
        catalogue.append((_d(_d(program).get("detail")).get("name"), _d(program).get("programConfig")))
    out: list[dict] = []
    titles: set[str] = set()
    for title, config in catalogue:
        link = _d(_d(config).get("link")).get("name")
        definition = definitions.get(link) if isinstance(link, str) else None
        if definition is None or not isinstance(title, str) or not title.strip() or title.strip() in titles:
            continue
        selected = {}
        for item in _d(config).get("selectedValues") or []:
            if isinstance(item, dict) and item.get("attrName") is not None and item.get("attrValue") is not None:
                selected[str(item["attrName"])] = item["attrValue"]
        commands = []
        for field in definition["attrNameList"]:
            if not isinstance(field, dict) or field.get("name") is None:
                continue
            value = selected.get(str(field["name"]))
            if value is None or not _value_allowed(field, value):
                value = field.get("defaultValue")
            if value is None or isinstance(value, dict | list):
                continue  # the machine sets it
            text = str(value).lower() if isinstance(value, bool) else str(value)
            commands.append({"commandName": str(field["name"]), "value": text})
        if commands:
            titles.add(title.strip())
            out.append({"title": title.strip(), "link": link, "group": str(definition["name"]), "commands": commands})
    return out


def laundry_actions(data: Any) -> dict[str, dict]:
    """{"pause" | "resume" | "cancel": {"commandName", "value"}} from the app's
    control screens: controlBlock of a washer, device.mainControl of a dryer.
    An action whose attribute the machine does not have is left out."""
    data = _d(data)
    known = {str(a.get("name")) for a in device_part(data).get("attributes") or [] if isinstance(a, dict)}
    washer, dryer = _d(data.get("controlBlock")), _d(_d(data.get("device")).get("mainControl"))
    pause, resume, cancel = _d(washer.get("pause")), _d(washer.get("resumeProgram")), _d(washer.get("cancel"))
    candidates = {
        "pause": (_d(pause.get("link")).get("name"), pause.get("pauseValue")),
        "resume": (_d(resume.get("link")).get("name") or _d(pause.get("link")).get("name"), resume.get("resumeValue")),
        "cancel": (_d(cancel.get("link")).get("name"), cancel.get("cancelValue")),
    }
    if dryer:
        dryer_pause = _d(_d(dryer.get("resumePause")).get("pause"))
        button = _d(dryer_pause.get("mainBtn"))
        code = _d(button.get("link")).get("name") or _d(dryer_pause.get("link")).get("name")
        dryer_cancel = _d(dryer.get("cancel"))
        candidates.update({
            "pause": (code, _d(button.get("onButton")).get("commandValue")),
            "resume": (code, _d(button.get("offButton")).get("commandValue")),
            "cancel": (_d(dryer_cancel.get("link")).get("name"), dryer_cancel.get("cancelValue")),
        })
    out = {}
    for action, (code, value) in candidates.items():
        if (code is not None and str(code) in known and isinstance(value, str | int | float)
                and not isinstance(value, bool) and str(value) != ""):
            out[action] = {"commandName": str(code), "value": str(value)}
    return out


class HaierLaundry(HaierDevice):
    """A washer, washer-dryer or dryer: what runs, the phase, the time left,
    remote control, the child lock and the alarms.

    Control is a beta, only with the entry's option "laundry_control" on: a
    program is chosen (nothing is sent) and started, paused, resumed or
    cancelled with the commands the cloud's description gives, and only when
    the machine allows remote control and is in a state the app offers the
    action in. It has not been checked on a real machine.
    """

    # Start, pause, cancel are actions: an unconfirmed "start" is not re-sent
    # once anything newer (a cancel) went to the machine.
    absolute_commands = False

    def __init__(self, backend_data: dict | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.codes: dict[str, str] = {}
        self.phase_titles: dict[str, str] = {}
        self.program_title: str | None = None
        self.program_status: str | None = None  # IN_PROGRESS, PAUSED, FINISHED... as the cloud says
        self.is_dryer = False
        self.programs: list[dict] = []
        self.actions: dict[str, dict] = {}
        self.selected_program: str | None = None  # what the start button runs
        self._control_lock = asyncio.Lock()  # one command at a time
        self.machine_entity_id: str | None = None
        self.recovery: HaierLaundryCoordinator | None = None
        self._last_status_observed = time.monotonic()
        self._phase_idle = False
        self._program_parameters: dict = {}
        self._start_pending = False
        self._active_program_values: dict = {}
        self.events = LaundryEvents(self)
        self._get_status(backend_data or {})

    @staticmethod
    def describes(data: Any) -> bool:
        return isinstance(data, dict) and isinstance(data.get("smartDeviceControl"), dict)

    def _load_config_from_attributes(self, data: dict) -> None:
        config = CFG.HaierDeviceConfig(self.device_model, self.hass.config.path(C.DOMAIN))
        config.attrs = [CFG.Attribute(a) for a in device_part(data).get("attributes") or [] if isinstance(a, dict)]
        self._config = config
        for attr in config.attrs:
            self._apply_value(attr.code, attr.current)
        self.codes = laundry_codes(data)
        self.phase_titles = laundry_phase_titles(data)
        self.is_dryer = isinstance(data.get("device"), dict)
        self._apply_program(self._program_block(data))
        self.programs = laundry_programs(data)
        self.actions = laundry_actions(data)
        if self.selected_program not in {p["title"] for p in self.programs}:
            # the last program of the machine, as the app suggests it
            link = _d(_d(self._program_block(data)).get("config")).get("link")
            self.selected_program = next(
                (p["title"] for p in self.programs if p["link"] == _d(link).get("name")), None
            )

    @staticmethod
    def _program_block(data: Any) -> dict | None:
        data = _d(data)
        if isinstance(data.get("program"), dict):
            return data["program"]
        current = _d(data.get("control")).get("currentProgram")
        return current if isinstance(current, dict) else None

    def _apply_program(self, program: dict | None) -> None:
        if program is None:
            self.program_title = self.program_status = None
            return
        title = program.get("title")
        new_program = False
        if isinstance(title, str) and title.strip():
            if title.strip() != self.program_title:
                self._active_program_values = {}
                new_program = True
            self.program_title = title.strip()
        status = program.get("status")
        if isinstance(status, str) and status:
            reported = status.upper().removeprefix("PROGRAM_")
            self.program_status = reported.replace("STARTED", "IN_PROGRESS")
            if reported == "STARTED" or new_program and self.program_status in ("IN_PROGRESS", "PAUSED"):
                self._phase_idle = False
        selected = _d(program.get("config")).get("selectedValues")
        if not isinstance(selected, list):
            selected = program.get("selectedValues")
        if isinstance(selected, list):
            self._active_program_values = {str(v["attrName"]): v["attrValue"] for v in selected
                                           if isinstance(v, dict) and v.get("attrName") is not None and v.get("attrValue") is not None}


    def update_from_backend(self, data: dict) -> None:
        if isinstance(data, dict):
            previous = self.phase_value
            self._apply_program(self._program_block(data))
            self._observe_phase(previous, device_part(data).get("attributes") or [],
                                reported=bool(_d(self._program_block(data)).get("status")))
        self._last_status_observed = time.monotonic()
        super().update_from_backend(data)
        if self.state in ("running", "paused", "care"):
            self._start_pending = False
        self.events.observe()
        if self.recovery is not None:
            self.recovery.push_updated()


    def _value(self, meaning: str) -> str | None:
        code = self.codes.get(meaning)
        return None if code is None else self._raw.get(code)

    def _flag(self, meaning: str) -> bool | None:
        raw = self._value(meaning)
        return None if raw is None else _norm(raw) == "1"

    def _number(self, meaning: str) -> int | None:
        number = parsefloat(self._value(meaning))
        return int(number) if number is not None and number == number and abs(number) != float("inf") else None

    @property
    def power(self) -> bool | None:
        return self._flag("onOffStatus")

    @property
    def remote_control(self) -> bool | None:
        return self._flag("remoteCtrValid")

    @property
    def child_lock(self) -> bool | None:
        return self._flag("childLockStatus")

    @property
    def phase_value(self) -> str | None:
        return self._value("cyclePhase")

    @property
    def phase(self) -> str | None:
        value = self.phase_value
        if value is None:
            return None
        return self.phase_titles.get(value) or f"Этап {value}"

    @property
    def remaining_time(self) -> int | None:
        """Minutes left; 0 for a machine that is off (it keeps the last value)."""
        if self.power is False:
            return 0
        if "remainingDryTime" in self.codes:
            return self._number("remainingDryTime")
        hours, minutes = self._number("remainingTimeHH"), self._number("remainingTimeMM")
        if hours is None and minutes is None:
            return None
        return (hours or 0) * 60 + (minutes or 0)

    @property
    def state(self) -> str | None:
        if self.power is False:
            return "off"
        if self.in_postcare:
            return "paused" if self.program_status == "PAUSED" else "care"
        if self._phase_idle and self.program_status == "IN_PROGRESS":
            return "idle"
        state = LAUNDRY_PROGRAM_STATES.get(self.program_status or "")
        if state is not None:
            return state
        if self.phase_value is None:
            return None if self.power is None else "idle"
        return "idle" if self.phase_value == "0" else "running"


    def to_dict(self) -> dict:
        data = super().to_dict()
        data.update({
            "state": self.state,
            "program": self.program_title,
            "program_status": self.program_status,
            "phase": self.phase,
            "remaining_time": self.remaining_time,
            "remote_control": self.remote_control,
            "child_lock": self.child_lock,
            "codes": dict(self.codes),
            "control": {
                "enabled": self.control_enabled,
                "programs": [p["title"] for p in self.programs],
                "actions": {k: dict(v) for k, v in self.actions.items()},
                "selected_program": self.selected_program,
            },
        })
        return data

    def on_message(self, message_dict: dict) -> None:
        event = message_dict.get("event")
        if event in ("ProgramWasherBaseEvent", "ProgramDryerBaseEvent"):
            # the program changed: started, paused, finished, cancelled
            data = message_dict.get("data")
            data = data if isinstance(data, dict) else {}
            program = data.get("program")
            if isinstance(program, dict):
                self._apply_program(program)
            if data.get("status") in ("PROGRAM_STARTED", "PROGRAM_FINISHED"):
                self.program_status = str(data["status"]).removeprefix("PROGRAM_").replace("STARTED", "IN_PROGRESS")
            if data.get("status") == "PROGRAM_STARTED":
                self._phase_idle = False
            self.changed_at = self._last_status_observed = time.monotonic()
            reported = data.get("status") or (program.get("status") if isinstance(program, dict) else None)
            if (self.state in ("running", "paused", "care") and isinstance(reported, str)
                    and reported.upper().removeprefix("PROGRAM_") in ("IN_PROGRESS", "STARTED", "PAUSED")):
                self._start_pending = False
            self.notify()
            self.events.observe(started=data.get("status") == "PROGRAM_STARTED")
            if self.recovery is not None:
                self.recovery.push_updated()
        elif event == "alarms":
            payload = message_dict.get("payload")
            alarms = payload.get("alarms") if isinstance(payload, dict) else None
            if isinstance(alarms, list):
                self._set_alarms(alarms)
                self.notify()
                self.events.observe()
        else:
            super().on_message(message_dict)


    def create_entities_sensor(self) -> list:
        from . import sensor
        entities = [sensor.HaierLaundryStateSensor(self), sensor.HaierLaundryProgramSensor(self)]
        if "cyclePhase" in self.codes:
            entities.append(sensor.HaierLaundryPhaseSensor(self))
        if {"remainingDryTime", "remainingTimeHH", "remainingTimeMM"} & set(self.codes):
            entities.append(sensor.HaierLaundryRemainingTimeSensor(self))
        return entities

    def create_entities_binary_sensor(self) -> list:
        from . import binary_sensor
        entities = [binary_sensor.HaierProblemSensor(self), binary_sensor.HaierConnectivitySensor(self)]
        if "remoteCtrValid" in self.codes:
            entities.append(binary_sensor.HaierLaundryRemoteControlSensor(self))
        if "childLockStatus" in self.codes:
            entities.append(binary_sensor.HaierLaundryChildLockSensor(self))
        return entities

    def retired_unique_ids(self, platform: str) -> list[str]:
        """With the control turned off, its entities leave the registry (no orphans)."""
        retired = super().retired_unique_ids(platform)
        if not self.control_enabled:
            prefix = f"{self.device_id}_{self.device_model}"
            if platform == "button":
                retired += [f"{prefix}_laundry_{action}" for action in LAUNDRY_ACTIONS]
            elif platform == "select":
                retired.append(f"{prefix}_laundry_program")
        return retired

    # ---- control (beta)

    @property
    def control_enabled(self) -> bool:
        return bool(getattr(self._haier, "laundry_control", False))

    def _control_error(self, translation_key: str, **extra: str) -> HaierCommandLocked:
        return HaierCommandLocked(
            translation_domain=C.DOMAIN,
            translation_key=translation_key,
            translation_placeholders={"device": str(self.device_name), **extra},
        )

    def set_selected_program(self, title: str) -> None:
        """Choose what the start button runs; nothing is sent."""
        if title not in {p["title"] for p in self.programs}:
            raise self._control_error(translation_key="laundry_unknown_program", program=str(title))
        self.selected_program = title
        self._program_parameters = {}
        self.notify()


    def _check_control(self, action: str, translation_key: str) -> None:
        """translation_key: the error when the state does not allow the action."""
        if self._start_pending:
            if action == "start":
                raise self._control_error("laundry_start_pending")
            raise self._control_error("laundry_busy")
        if not self.control_enabled:
            raise self._control_error("laundry_control_off")
        if self.remote_control is not True:
            # off on the machine, or the description does not say: the app sends nothing either
            raise self._control_error("laundry_remote_off")
        if self.state not in LAUNDRY_ACTION_STATES[action] or (action != "start" and action not in self.actions):
            raise self._control_error(translation_key, state=str(self.state))

    async def _send_control(self, message: dict) -> None:
        if self._control_lock.locked():
            raise self._control_error("laundry_busy")  # a second press while the first one is on its way
        async with self._control_lock:
            await self._send_message({**message, "trace": str(uuid.uuid4())})

    async def start(self) -> None:
        self._check_control("start", "laundry_cannot_start")
        program = next((p for p in self.programs if p["title"] == self.selected_program), None)
        if program is None:
            raise self._control_error("laundry_no_program")
        self._active_program_values = {c["commandName"]: c["value"] for c in self._start_commands(program)}
        self._start_pending = True
        try:
            await self._send_control({
                "action": "operation", "macAddress": self.device_id, "commandName": program["group"],
                "commands": [{"commandName": k, "value": v} for k, v in self._active_program_values.items()],
            })
        except Exception:
            self._start_pending = False
            self.notify()
            raise
        self.notify()
        if self.recovery is not None:
            self.recovery.request_refresh()

    async def _act(self, action: str, translation_key: str) -> None:
        self._check_control(action, translation_key)
        await self._send_control({"action": "command", "macAddress": self.device_id, "command": dict(self.actions[action])})

    async def pause(self) -> None:
        await self._act("pause", "laundry_cannot_pause")

    async def resume(self) -> None:
        await self._act("resume", "laundry_cannot_resume")

    async def cancel(self) -> None:
        await self._act("cancel", "laundry_cannot_cancel")

    def create_entities_select(self) -> list:
        if not (self.control_enabled and self.programs):
            return []
        from . import select
        return [select.HaierLaundryProgramSelect(self)]

    def create_entities_button(self) -> list:
        if not self.control_enabled:
            return []
        from . import button
        actions = (["start"] if self.programs else []) + [a for a in ("pause", "resume", "cancel") if a in self.actions]
        return [button.HaierLaundryButton(self, action) for action in actions]


    def _observe_phase(self, previous, attributes, *, reported=False):
        code = self.codes.get("cyclePhase")
        attributes = attributes if isinstance(attributes, list) else []
        phases = [str(item["currentValue"]) for item in attributes
                  if isinstance(item, dict) and str(item.get("name")) == code and item.get("currentValue") is not None]
        current = phases[-1] if phases else None
        if current is not None:
            if current != "0":
                self._phase_idle = False
                if current != previous:
                    self._start_pending = False
                    if not reported and self.program_status in ("FINISHED", "CANCELED", "CANCELLED"):
                        self.program_status = None
            elif previous not in (None, "0"):
                self._phase_idle = True


    def _handle_status_update(self, message: dict) -> None:
        previous = self.phase_value
        statuses = _d(message.get("payload")).get("statuses")
        statuses = statuses if isinstance(statuses, list) else []
        attributes = [{"name": k, "currentValue": v} for status in statuses if isinstance(status, dict)
                      for k, v in _d(status.get("properties")).items() if v is not None]
        self._observe_phase(previous, attributes)
        meaningful = {self.codes.get(k) for k in ("cyclePhase", "remainingTimeHH", "remainingTimeMM", "remainingDryTime")}
        if any(str(item["name"]) in meaningful for item in attributes):
            self._last_status_observed = time.monotonic()
        super()._handle_status_update(message)
        self.events.observe()
        if self.recovery is not None:
            self.recovery.push_updated()


    def _handle_device_status_update(self, message: dict) -> None:
        super()._handle_device_status_update(message)
        self.events.observe()
        if self.recovery is not None:
            self.recovery.request_refresh()


    @property
    def in_postcare(self) -> bool:
        screen = laundry_screen(self.status_data)
        phase = _d(screen.get("phase"))
        care = _d(screen.get("freshAirSystem")) or _d(phase.get("antiCrease"))
        value = care.get("freshAirSystemAttrValue", care.get("cyclePhaseValue"))
        return (self.power is not False and self.program_status not in ("CANCELED", "CANCELLED")
                and value is not None and self.phase_value == str(value))


    @property
    def postcare_name(self) -> str | None:
        screen = laundry_screen(self.status_data)
        care = _d(screen.get("freshAirSystem")) or _d(_d(screen.get("phase")).get("antiCrease"))
        name = care.get("title") or care.get("statusTitle")
        return name if isinstance(name, str) else None


    @property
    def laundry_ready(self) -> bool:
        return self.events.laundry_ready


    @property
    def laundry_errors(self) -> list[dict]:
        table = _d(_d(device_part(self.status_data).get("errors")).get("alarmToResult"))
        result = []
        for key, text in zip(self.alarms, self.alarm_texts, strict=True):
            definition = _d(table.get(key))
            code = text[1:].split("]", 1)[0] if text.startswith("[") and "]" in text else key
            result.append({"key": key, "code": code, "text": text,
                           "description": definition.get("description") if isinstance(definition.get("description"), str) else ""})
        return result


    @property
    def _settings_programs(self) -> dict:
        return {p["title"]: {"commandName": p["group"], "commands": p["commands"]} for p in self.programs}


    @property
    def program_catalog(self) -> list:
        return build_catalogue(self.status_data, self._settings_programs, plain_titles=True)


    @property
    def program_settings(self) -> dict:
        if self.selected_program not in self._settings_programs:
            return {}
        try:
            settings = program_settings(self.status_data, self.selected_program,
                                        self._settings_programs[self.selected_program], self._program_parameters,
                                        self._raw, self.alarms)
        except ValueError as err:
            return {"program": self.selected_program, "fields": [], "error": str(err)}
        return {k: v for k, v in settings.items() if k != "commands"}


    @property
    def active_program_details(self) -> dict:
        if not self.available or not self.program_title:
            return {}
        result = active_program_details(self.status_data, self.program_title, self._settings_programs,
                                        self._raw, self._active_program_values)
        result["remaining_minutes"] = self.remaining_time
        return result


    def configure_program(self, program: str, parameters: dict) -> None:
        if program != self.selected_program or program not in self._settings_programs:
            raise ServiceValidationError("Выбранная программа изменилась; откройте параметры заново")
        if self._start_pending or self.state in ("running", "paused", "care"):
            raise ServiceValidationError("Параметры можно изменить до запуска программы")
        editable = {f["key"] for f in self.program_settings.get("fields", []) if not f.get("disabled")}
        if not isinstance(parameters, dict) or not parameters or not set(parameters) <= editable:
            raise ServiceValidationError("Параметр недоступен для выбранной программы")
        updated = {**self._program_parameters, **parameters}
        try:
            program_settings(self.status_data, program, self._settings_programs[program], updated, self._raw, self.alarms)
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err
        self._program_parameters = updated
        self.notify()


    def _start_commands(self, program: dict) -> list:
        if not self._program_parameters:
            return [dict(c) for c in program["commands"]]
        try:
            return program_settings(self.status_data, self.selected_program, self._settings_programs[self.selected_program],
                                    self._program_parameters, self._raw, self.alarms)["commands"]
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err


    @property
    def available_actions(self) -> list[str]:
        if self._start_pending or not (self.control_enabled and self.available and self.remote_control is True):
            return []
        return [action for action, states in LAUNDRY_ACTION_STATES.items()
                if self.state in states and (action == "start" and self.selected_program is not None or action in self.actions)]


    async def send_laundry_action(self, action: str) -> None:
        if action not in LAUNDRY_ACTION_STATES:
            raise ServiceValidationError("Неизвестная команда")
        await getattr(self, action)()
