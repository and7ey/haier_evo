"""Read-only recovery for laundry when push stops carrying useful state."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import timedelta

from homeassistant.core import callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import HaierAuthError, HaierError, HaierRateLimitError
from .logger import _LOGGER


@dataclass
class LaundryPollingState:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    next_request: float = 0.0
    retry_until: float = 0.0


class HaierLaundryCoordinator(DataUpdateCoordinator[None]):
    def __init__(self, hass, entry, device, account: LaundryPollingState):
        self.device = device
        self.cloud = device._haier
        self.entry = entry
        self.account = account
        self._failures = 0
        self._retry_until = 0.0
        self._closed = False
        super().__init__(hass, _LOGGER, config_entry=entry, name=f"Haier {device.device_name}",
                         update_interval=self.normal_interval)
        device.recovery = self

    @property
    def normal_interval(self):
        seconds = 60 if not self.device.connected else 30 if self.device.state in ("running", "paused", "care") else 120
        return timedelta(seconds=seconds)

    @callback
    def request_refresh(self):
        if not self._closed:
            self.entry.async_create_background_task(
                self.hass, self.async_request_refresh(),
                name=f"Haier {self.device.device_name} refresh", eager_start=True,
            )

    @callback
    def push_updated(self):
        if self._closed or self.account.retry_until > time.monotonic():
            return
        if time.monotonic() - self.device._last_status_observed < 45:
            self._failures = 0
            self._retry_until = 0.0
        interval = self.normal_interval
        if interval != self.update_interval:
            self.update_interval = interval
            if self._listeners:
                self._schedule_refresh()

    async def _async_update_data(self):
        if (self.device.connected and self.device.synced and not self.device._start_pending
                and self.device.state in ("running", "paused", "care")
                and time.monotonic() - self.device._last_status_observed < 45):
            self.update_interval = self.normal_interval
            return
        async with self.account.lock:
            if self._closed or self.cloud._stopping or self.cloud._reauth_started:
                return
            retry = max(self._retry_until, self.account.retry_until) - time.monotonic()
            if retry > 0:
                raise UpdateFailed("Haier cloud retry is pending", retry_after=retry)
            delay = self.account.next_request - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            if self._closed:
                return
            started = time.monotonic()
            self.account.next_request = started + 15
            try:
                data = await self.cloud.client.async_get_device_status(self.device.device_id)
                if self._closed:
                    return
                if self.device.synced and self.device.changed_at > started:
                    return
                if self.device.described_differently(data):
                    self.cloud._schedule_rebuild("the cloud describes the laundry device differently")
                self.device.update_from_backend(data)
                self.device.notify()
                if self.cloud.on_refreshed is not None:
                    self.cloud.on_refreshed()
            except HaierAuthError:
                self.cloud._start_reauth()
                raise
            except (HaierError, ValueError) as err:
                self._failures += 1
                retry = min(60 * 2 ** min(self._failures - 1, 4), 900)
                if isinstance(err, HaierRateLimitError):
                    retry = max(retry, err.retry_after)
                    self.account.retry_until = time.monotonic() + retry
                self._retry_until = time.monotonic() + retry
                self.update_interval = timedelta(seconds=retry)
                raise UpdateFailed(f"Unable to refresh laundry: {type(err).__name__}", retry_after=retry) from err
            self._failures = 0
            self._retry_until = 0.0
            self.update_interval = self.normal_interval

    async def async_shutdown(self):
        self._closed = True
        self.device.recovery = None
        await super().async_shutdown()
