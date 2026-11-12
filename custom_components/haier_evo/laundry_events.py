"""Debounced laundry events shared by push updates and background snapshots."""
from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from typing import Any

from homeassistant.core import callback
from homeassistant.helpers.event import async_call_later

EVENT_TYPE = "haier_evo_laundry_event"
FINISH_CONFIRM_SECONDS = 10


class LaundryEvents:
    """Observe confirmed device state; never send a command to the appliance."""

    def __init__(self, device: Any) -> None:
        self.device = device
        self.hass = device.hass
        self.completion_reason: str | None = None
        self._latest: dict | None = None
        self._active = False
        self._idle_since_completion = False
        self._last_program: str | None = None
        self._last_errors: tuple[str, ...] = ()
        self._pending: str | None = None
        self._cancel_timer: Callable[[], None] | None = None
        self._closed = False

    @property
    def laundry_ready(self) -> bool:
        return bool(self.device.in_postcare or self.completion_reason in ("program", "post_care"))

    def observe(self, *, started: bool = False) -> None:
        # Capture in the receiving thread: a FINISHED/STARTED pair can arrive before the loop runs.
        snapshot = {
            "state": self.device.state,
            "available": self.device.available,
            "program": self.device.program_title,
            "remaining": self.device.remaining_time,
            "postcare": self.device.in_postcare,
            "care_name": self.device.postcare_name,
            "errors": deepcopy(self.device.laundry_errors),
            "started": started,
        }
        self.hass.loop.call_soon_threadsafe(self._accept, snapshot)

    @callback
    def _accept(self, snapshot: dict) -> None:
        if self._closed:
            return
        previous = self._latest
        self._latest = snapshot
        if not snapshot["available"]:
            self._cancel_pending()
            return
        state = snapshot["state"]
        active = state in ("running", "paused") and not snapshot["postcare"]
        errors = tuple(sorted(str(e.get("key") or e.get("code")) for e in snapshot["errors"]))
        if previous is None:
            self._active = active
            self._last_errors = errors
            self._last_program = snapshot["program"]
            if snapshot["postcare"]:
                self.completion_reason = "post_care"
            return
        if errors != self._last_errors:
            self._last_errors = errors
            self._fire("error" if errors else "error_cleared")
        if self.completion_reason is not None:
            if state in ("idle", "off"):
                self._idle_since_completion = True
            new_program = bool(snapshot["program"] and self._last_program
                               and snapshot["program"] != self._last_program)
            remaining = snapshot["remaining"]
            previous_remaining = previous["remaining"]
            restarted = (new_program and remaining is not None and remaining > 1
                         and previous_remaining in (0, 1))
            if not (active and (snapshot["started"] or self._idle_since_completion or restarted)):
                return
            self.completion_reason = None
            self._idle_since_completion = False
            self.device.notify()
        if active:
            self._active = True
            if snapshot["program"]:
                self._last_program = snapshot["program"]
            self._cancel_pending()
        elif state in ("cancelled", "off"):
            self._active = False
            self._cancel_pending()
        elif self._active and (snapshot["postcare"] or state == "finished"):
            self._schedule("program_finished")
        elif self._active and state == "idle":
            self._schedule("cycle_ended")
        else:
            self._cancel_pending()

    @callback
    def _schedule(self, event_type: str) -> None:
        if self._pending == event_type or self._pending == "program_finished":
            return
        self._cancel_pending()
        self._pending = event_type
        self._cancel_timer = async_call_later(self.hass, FINISH_CONFIRM_SECONDS, self._confirm)

    @callback
    def _confirm(self, _now) -> None:
        event_type = self._pending
        self._pending = self._cancel_timer = None
        latest = self._latest
        if (event_type is None or self._closed or latest is None or not latest["available"] or not self.device.available
                or not self._active or self.completion_reason is not None):
            return
        postcare = latest["postcare"]
        if not (postcare or latest["state"] in ("finished", "idle")):
            return
        self.completion_reason = "post_care" if postcare else "program" if event_type == "program_finished" else "idle"
        self._active = False
        self._idle_since_completion = latest["state"] == "idle"
        self._fire(event_type)
        self.device.notify()

    @callback
    def _fire(self, event_type: str) -> None:
        latest = self._latest
        entity_id = getattr(self.device, "machine_entity_id", None)
        if latest is None or not entity_id:
            return
        data = {
            "type": event_type,
            "entity_id": entity_id,
            "name": self.device.device_name,
            "model": self.device.device_model,
            "kind": "dryer" if self.device.is_dryer else "washer",
            "program": latest["program"] or self._last_program,
            "errors": deepcopy(latest["errors"]),
        }
        if event_type in ("program_finished", "cycle_ended"):
            data["completion_reason"] = self.completion_reason
            if latest["postcare"]:
                data["care_name"] = latest["care_name"]
        self.hass.bus.async_fire(EVENT_TYPE, data)

    @callback
    def _cancel_pending(self) -> None:
        if self._cancel_timer is not None:
            self._cancel_timer()
        self._cancel_timer = self._pending = None

    def close(self) -> None:
        self._closed = True
        self.hass.loop.call_soon_threadsafe(self._cancel_pending)
