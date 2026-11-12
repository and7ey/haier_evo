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
from typing import Any

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
LAUNDRY_STATES = ("off", "idle", "running", "paused", "finished", "cancelled")
# the states the app offers each action in
LAUNDRY_ACTION_STATES = {
    "start": ("idle", "finished", "cancelled", "off"),
    "pause": ("running",),
    "resume": ("paused",),
    "cancel": ("running", "paused"),
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
        self.program_title = title.strip() if isinstance(title, str) and title.strip() else None
        status = program.get("status")
        self.program_status = (
            str(status).upper().removeprefix("PROGRAM_") if isinstance(status, str) and status else None
        )

    def update_from_backend(self, data: dict) -> None:
        if isinstance(data, dict):
            self._apply_program(self._program_block(data))
        super().update_from_backend(data)

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
            data = _d(message_dict.get("data"))
            if isinstance(data.get("program"), dict):
                self._apply_program(data["program"])
            elif data.get("status") in ("PROGRAM_STARTED", "PROGRAM_FINISHED"):
                self.program_status = str(data["status"]).removeprefix("PROGRAM_").replace("STARTED", "IN_PROGRESS")
            self.changed_at = time.monotonic()
            self.notify()
        elif event == "alarms":
            alarms = _d(message_dict.get("payload")).get("alarms")
            if isinstance(alarms, list):
                self._set_alarms(alarms)
                self.notify()
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
            raise self._control_error("laundry_unknown_program", program=str(title))
        self.selected_program = title
        self.notify()

    def _check_control(self, action: str, translation_key: str) -> None:
        """translation_key: the error when the state does not allow the action."""
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
        """Start the chosen program with its values: one group command, as the app sends it."""
        self._check_control("start", "laundry_cannot_start")
        program = next((p for p in self.programs if p["title"] == self.selected_program), None)
        if program is None:
            raise self._control_error("laundry_no_program")
        await self._send_control({
            "action": "operation",
            "macAddress": self.device_id,
            "commandName": program["group"],
            "commands": [dict(c) for c in program["commands"]],
        })

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
