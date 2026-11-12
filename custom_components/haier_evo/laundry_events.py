"""Laundry notifications from observed cloud state, independent of entity enablement."""
from __future__ import annotations

import re
from collections.abc import Callable
from copy import deepcopy
from typing import TYPE_CHECKING, Any

from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_call_later

from .const import DOMAIN
from .device import device_part

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

    from .device_laundry import HaierLaundry

EVENT_TYPE = "haier_evo_laundry_event"
EVENT_TYPES = ["program_finished", "cycle_ended", "program_cancelled", "care_started", "care_finished", "error", "error_cleared"]
FINISH_CONFIRM_SECONDS = 10


class LaundryEvents:
    """Debounce a completion; never change the native machine state or send commands."""

    def __init__(self, device: HaierLaundry) -> None:
        self.device = device
        self.hass = device.hass
        self.unique_id = f"{device.device_id}_{device.device_model}_laundry"
        self._entry_id: str | None = None
        self._listeners: list[Callable[[dict[str, Any]], None]] = []
        self._latest: dict[str, Any] | None = None
        self._active = False
        self._main_seen = False
        self._ended = False
        self._rearmed = False
        self._care: str | None = None
        self._care_started = False
        self._last_program: str | None = None
        self._last_errors: tuple[str, ...] = ()
        self._pending: str | None = None
        self._cancel_timer: Callable[[], None] | None = None
        self._closed = False
        self.laundry_ready = False

    @callback
    def start(self, entry: ConfigEntry) -> None:
        self._entry_id = entry.entry_id
        self.observe()
        entry.async_on_unload(self.device.add_listener(self.observe))
        entry.async_on_unload(self.close)

    @callback
    def add_listener(self, listener: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
        self._listeners.append(listener)

        def remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return remove

    def _care_name(self) -> str | None:
        from .device_laundry import laundry_screen

        screen = laundry_screen(self.device.status_data)
        for parent in (screen, screen.get("phase")):
            if not isinstance(parent, dict):
                continue
            for name, key in (("freshAirSystem", "freshAirSystemAttrValue"), ("antiCrease", "cyclePhaseValue")):
                block = parent.get(name)
                if (isinstance(block, dict) and block.get(key) is not None
                        and str(block[key]) == self.device.phase_value):
                    title = block.get("title") or block.get("statusTitle")
                    if isinstance(title, str) and title.strip():
                        return title.strip()
        return None

    def _errors(self) -> list[dict[str, Any]]:
        errors = device_part(self.device.status_data).get("errors")
        table = errors.get("alarmToResult") if isinstance(errors, dict) else None
        table = table if isinstance(table, dict) else {}
        result = []
        for alarm, text in zip(self.device.alarms, self.device.alarm_texts, strict=True):
            item = table.get(alarm)
            item = item if isinstance(item, dict) else {}
            code = re.match(r"^\[([^\]]+)\]", text)
            result.append({"key": alarm, "code": code[1] if code else None, "text": text,
                           "description": item.get("description")})
        return result

    @callback
    def observe(self, *, started: bool = False, cancelled: bool = False) -> None:
        if self._closed:
            return
        snapshot: dict[str, Any] = {
            "state": "cancelled" if cancelled else "running" if started else self.device.state,
            "available": self.device.available,
            "program": self.device.program_title,
            "care_name": self._care_name(),
            "fault": self.device.problem,
            "errors": self._errors(),
        }
        if self._pending == "program_finished" and not (started or cancelled):
            from .device_laundry import LAUNDRY_PROGRAM_STATES

            program_state = LAUNDRY_PROGRAM_STATES.get(self.device.program_status or "")
            if program_state in ("running", "paused", "cancelled"):
                if snapshot["state"] != "off" or program_state == "cancelled":
                    snapshot["state"] = program_state
            elif snapshot["state"] not in ("idle", "off"):
                # An empty programme may leave a running phase in the native sensor.
                snapshot["state"] = "finished"
        previous = self._latest
        self._latest = snapshot
        state = snapshot["state"]
        active = state in ("running", "paused")
        errors = tuple(sorted(str(error["key"]) for error in snapshot["errors"]))
        if previous is None:
            self._active = bool(snapshot["available"] and active and not snapshot["fault"])
            self._main_seen = self._active and not snapshot["care_name"]
            self._last_errors = errors
            self._last_program = snapshot["program"]
            return
        program_changed = bool(snapshot["program"] and self._last_program
                               and snapshot["program"] != self._last_program)
        if not snapshot["available"]:
            if snapshot["fault"] or state in ("idle", "off", "cancelled") or program_changed:
                self._main_seen = False
            self._cancel_pending()
            return
        if errors != self._last_errors:
            self._last_errors = errors
            self._fire("error" if errors else "error_cleared")
        if snapshot["fault"]:
            self._active = False
            self._main_seen = False
            self._cancel_pending()
            return
        if self._ended:
            if state in ("idle", "off"):
                self._rearmed = True
            if active and (self._rearmed or program_changed or started and not snapshot["care_name"]):
                self._ended = self._rearmed = self.laundry_ready = False
                self._main_seen = False
                self._care = None
                self._care_started = False
            else:
                self._observe_care()
                return
        if active:
            if program_changed:
                self._cancel_pending()
                self._main_seen = False
            if snapshot["care_name"]:
                if self._main_seen:
                    self._schedule("post_care")
            else:
                self._main_seen = True
                self._cancel_pending()
            self._active = True
            if snapshot["program"]:
                self._last_program = snapshot["program"]
        elif state == "cancelled":
            self._main_seen = False
            self._cancel_pending()
            if self._active:
                self._active = False
                self._ended = True
                self._fire("program_cancelled")
        elif self._active and state == "finished":
            self._schedule("program_finished")
        elif self._active and state in ("idle", "off"):
            self._main_seen = False
            self._schedule("cycle_ended")
        else:
            self._cancel_pending()

    @callback
    def _observe_care(self) -> None:
        latest = self._latest
        if latest is None or not self.laundry_ready:
            return
        name = latest["care_name"] if latest["state"] not in ("idle", "off", "cancelled") else None
        if name and not self._care_started:
            self._care = name
            self._care_started = True
            self._fire("care_started", care_name=name)
        elif self._care and not name:
            self._fire("care_finished", care_name=self._care)
            self._care = None

    @callback
    def _schedule(self, event_type: str) -> None:
        if self._pending in (event_type, "program_finished"):
            return
        self._cancel_pending()
        self._pending = event_type
        self._cancel_timer = async_call_later(self.hass, FINISH_CONFIRM_SECONDS, self._confirm)

    @callback
    def _confirm(self, _now) -> None:
        event_type = self._pending
        self._cancel_timer = self._pending = None
        latest = self._latest
        if (self._closed or event_type is None or latest is None or not latest["available"]
                or not self.device.available or not self._active or self._ended):
            return
        if self.device.problem:
            return
        care_name = latest["care_name"] if latest["state"] in ("running", "paused", "finished") else None
        if event_type == "post_care":
            if not care_name:
                return
            event_type = "program_finished"
        elif latest["state"] not in ("finished", "idle", "off") and not (
            event_type == "program_finished" and care_name
        ):
            return
        self._active = False
        self._main_seen = False
        self._ended = True
        self._rearmed = latest["state"] in ("idle", "off")
        self.laundry_ready = event_type == "program_finished"
        details = {"completion_reason": "program" if self.laundry_ready else "idle"}
        if care_name and self.laundry_ready:
            details.update(completion_reason="post_care", care_name=care_name)
        self._fire(event_type, **details)
        self._observe_care()

    @callback
    def _fire(self, event_type: str, **extra: str) -> None:
        latest = self._latest
        if latest is None:
            return
        devices = dr.async_entries_for_config_entry(dr.async_get(self.hass), self._entry_id) if self._entry_id else ()
        registry_device = next((device for device in devices if (DOMAIN, self.device.device_id) in device.identifiers), None)
        data = {
            "type": event_type,
            "device_id": registry_device.id if registry_device else None,
            "entity_id": er.async_get(self.hass).async_get_entity_id("event", DOMAIN, self.unique_id),
            "name": self.device.device_name,
            "model": self.device.device_model,
            "kind": "dryer" if self.device.is_dryer else "washer",
            "program": latest["program"] or self._last_program,
            "errors": deepcopy(latest["errors"]),
            "laundry_ready": self.laundry_ready,
            **extra,
        }
        self.hass.bus.async_fire(EVENT_TYPE, data)
        for listener in tuple(self._listeners):
            listener(deepcopy(data))

    @callback
    def _cancel_pending(self) -> None:
        if self._cancel_timer is not None:
            self._cancel_timer()
        self._cancel_timer = self._pending = None

    @callback
    def close(self) -> None:
        self._closed = True
        self._cancel_pending()
        self._listeners.clear()
