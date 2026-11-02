"""Bounded, read-only recovery of laundry snapshots."""
import asyncio
import math
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import time

from homeassistant.core import callback
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from requests.exceptions import HTTPError, RequestException

from .logger import _LOGGER
from .api import InvalidAuth


class HaierLaundryCoordinator(DataUpdateCoordinator[None]):
    def __init__(self, hass, entry, device):
        self.device = device
        self.cloud = device._haier
        self._failures = 0
        self._closed = False
        self._retry_until = 0.0
        super().__init__(
            hass, _LOGGER, config_entry=entry, name=f"Haier {device.device_name}",
            update_interval=self.normal_interval,
            request_refresh_debouncer=Debouncer(hass, _LOGGER, cooldown=15, immediate=True),
        )
        device.recovery = self

    @property
    def normal_interval(self):
        active = self.device.program_status in ("running", "paused")
        required = {str(key) for key in (self.device.phase_key, self.device.remote_key) if key is not None}
        incomplete = not required.issubset(self.device.values)
        return timedelta(seconds=60 if active or incomplete or not self.device.available else 300)

    @callback
    def request_refresh(self):
        self.hass.loop.call_soon_threadsafe(self._request_refresh_from_thread)

    @callback
    def _request_refresh_from_thread(self):
        if not self._closed:
            self.hass.async_create_task(self.async_request_refresh())

    @callback
    def push_updated(self):
        self.hass.loop.call_soon_threadsafe(self._accept_push)

    @callback
    def _accept_push(self):
        if self._closed or max(self._retry_until, self.cloud._laundry_retry_until) > time.monotonic():
            return
        interval = self.normal_interval
        if interval != self.update_interval:
            self.update_interval = interval
            if self._listeners:
                self._schedule_refresh()

    async def _async_update_data(self):
        required = {str(key) for key in (self.device.phase_key, self.device.remote_key) if key is not None}
        if (self.device.available and self.device.program_status in ("running", "paused")
                and required.issubset(self.device.values)
                and time.monotonic() - self.device._last_observed < 180):
            self.update_interval = self.normal_interval
            return
        async with self.cloud._laundry_poll_lock:
            if self._closed or getattr(self.cloud, "disconnect_requested", False):
                return
            retry_after = max(self.cloud._laundry_retry_until, self._retry_until) - time.monotonic()
            if retry_after > 0:
                raise UpdateFailed("Haier is temporarily rate limited", retry_after=retry_after)
            delay = self.cloud._laundry_next_request - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            if self._closed:
                return
            revision = self.device._state_revision
            try:
                self.cloud._laundry_next_request = time.monotonic() + 15
                data = await self.hass.async_add_executor_job(
                    self.cloud.read_device_snapshot, self.device.device_mac
                )
                if self._closed:
                    return
                self.device.apply_snapshot(data, revision)
            except (RequestException, ValueError, InvalidAuth) as err:
                self._failures += 1
                delay = min(60 * 2 ** min(self._failures - 1, 4), 900)
                if isinstance(err, HTTPError) and err.response is not None:
                    value = err.response.headers.get("Retry-After", "")
                    try:
                        server_delay = float(value)
                    except (ValueError, TypeError):
                        try:
                            server_delay = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
                        except (ValueError, TypeError, OverflowError):
                            server_delay = 0
                    if math.isfinite(server_delay):
                        delay = max(delay, server_delay)
                    if err.response.status_code in (429, 503):
                        self.cloud._laundry_retry_until = time.monotonic() + delay
                self._retry_until = time.monotonic() + delay
                self.update_interval = timedelta(seconds=delay)
                if time.monotonic() - self.device._last_observed > 300:
                    self.device.mark_stale()
                reason = str(err) if isinstance(err, ValueError) else type(err).__name__
                raise UpdateFailed(f"Unable to refresh Haier laundry state: {reason}", retry_after=delay) from err
            self._failures = 0
            self._retry_until = 0.0
            self.update_interval = self.normal_interval

    async def async_shutdown(self):
        self._closed = True
        self.device.cancel_pending_finish()
        self.device.recovery = None
        await super().async_shutdown()
