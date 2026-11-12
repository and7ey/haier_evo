from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Callable
from contextlib import suppress
from functools import partial
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

from aiohttp import (
    ClientError,
    ClientSession,
    ClientWebSocketResponse,
    WSMsgType,
    WSServerHandshakeError,
)
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_call_later, async_track_time_interval

from . import const as C  # noqa: N812
from .client import (
    HaierApiError,
    HaierAuthError,
    HaierClient,
    HaierConnectionError,
    HaierError,
    HaierRateLimitError,
)

# Devices live in device*.py; the hub, the platforms and the tests know them as api.*
from .device import (  # noqa: F401
    HaierCommandError,
    HaierCommandLocked,
    HaierCommandRejected,
    HaierDevice,
    _same_value,
    device_part,
    parsefloat,
)
from .device_ac import HaierAC  # noqa: F401
from .device_ref import HaierREF  # noqa: F401
from .device_wm import HaierWM  # noqa: F401
from .logger import _LOGGER

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

DEVICE_LIST_COMPONENT_ID = "72a6d224-cb66-4e6d-b427-2e4609252684"
# Entries whose next start reads the cloud, not the saved descriptions: the
# reload is there because those are out of date (a unit added or removed in
# the app, a unit described differently).
RELOAD_FROM_CLOUD: set[str] = set()
_MAC = re.compile(r"^[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}$")


class HaierEmptyDeviceList(HaierError):
    """The cloud answered with no devices (#105)."""


def parse_device_list(data: dict) -> list[dict[str, str]]:
    """Extract devices from the smartHome sduiRawPaginated page."""
    presentation = data.get("presentation") if isinstance(data, dict) else None
    layout = presentation.get("layout") if isinstance(presentation, dict) else None
    items = layout.get("scrollContainer") if isinstance(layout, dict) else None
    candidates = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        tracking = item.get("trackingData")
        component = tracking.get("component") if isinstance(tracking, dict) else None
        if not isinstance(component, dict) or component.get("componentName") != "deviceList":
            continue
        # Prefer the known container, but do not depend on its id.
        candidates.append((component.get("componentId") != DEVICE_LIST_COMPONENT_ID, item))
    candidates.sort(key=lambda c: c[0])
    devices: list[dict[str, str]] = []
    seen: set[str] = set()
    for _, item in candidates:
        state = item.get("state") or "{}"
        if isinstance(state, str):
            try:
                state = json.loads(state)
            except ValueError:
                continue
        entries = state.get("items") if isinstance(state, dict) else None
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict):
                continue
            action = entry.get("action")
            link = urlparse(str((action.get("link") if isinstance(action, dict) else "") or ""))
            if link.netloc == "non-iot" or link.path.startswith("/non-iot"):
                # An appliance added in the app without Wi-Fi (a dishwasher, a
                # hood): a block of its own, UUID ids, nothing to control (1.5).
                continue
            query = parse_qs(link.query)
            mac = (query.get("deviceId") or [""])[0].replace("%3A", ":")
            if not _MAC.match(mac):
                if mac:
                    _LOGGER.debug("Skipping a device list item with an unexpected id")
                continue
            if mac.lower() in seen:
                continue
            seen.add(mac.lower())
            devices.append({
                "mac": mac,
                "type": (query.get("type") or ["UNKNOWN"])[0],
                "serial": (query.get("serialNum") or [""])[0],
                "title": entry.get("title") if isinstance(entry.get("title"), str) else "",
            })
    return devices


def saved_missing(cached: Any) -> set[str]:
    """MACs a setup gave up waiting for, as kept with the saved descriptions."""
    items = cached.get("missing") if isinstance(cached, dict) else None
    if not isinstance(items, list):
        return set()
    return {item.lower() for item in items if isinstance(item, str)}



# Parts of a description the integration never reads: app analytics, pairing
# details, scenario links. Links (presigned, a week long) are dropped too (1.7).
_UNCACHED_KEYS = frozenset({"analyticsEvents", "analyticsEvent", "trackingData", "pairingInfo", "scenarios"})


def cacheable_description(data: Any) -> dict:
    """A copy of a device description that is worth saving between starts."""
    def clean(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: clean(v) for k, v in value.items() if k not in _UNCACHED_KEYS}
        if isinstance(value, list):
            return [clean(v) for v in value]
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return ""
        return value
    return clean(data if isinstance(data, dict) else {})


def command_names(payload: dict) -> set[str]:
    """The attribute codes a command message sets.

    Not the name of a group command: every group command of an AC carries the
    same one, whatever attributes it sets.
    """
    commands = list(payload.get("commands") or []) + ([payload["command"]] if payload.get("command") else [])
    names = {str(c.get("commandName")) for c in commands if isinstance(c, dict) and c.get("commandName")}
    return names or ({str(payload["commandName"])} if payload.get("commandName") else set())


def _rejected(err: Any) -> bool:
    """A non-zero errNo of a command_response."""
    return err not in (None, 0, "0")


def command_applied(payload: dict, status: dict) -> bool:
    """True if a REST status already holds every value the command asks for.

    The cloud answers a command for a value the unit already has with a
    non-zero errNo too (-1 for a setpoint or a mode, 22 for power, as seen in
    the AGrigorii fork): that is no rejection.
    """
    current = {
        str(attr.get("name")): attr.get("currentValue")
        for attr in device_part(status).get("attributes") or []
        if isinstance(attr, dict)
    }
    commands = payload.get("commands") or ([payload["command"]] if payload.get("command") else [])
    return bool(commands) and all(
        isinstance(command, dict)
        and current.get(str(command.get("commandName"))) is not None
        and _same_value(current[str(command.get("commandName"))], command.get("value"))
        for command in commands
    )


class Haier:
    """One Evo account: device list, WebSocket and command delivery."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: HaierClient,
        session: ClientSession,
    ) -> None:
        self.hass = hass
        self.client = client
        self._session = session
        self.devices: list[HaierDevice] = []
        self.backend_data: dict | None = None
        # The parsed device list ({mac, type, serial, title}) of the last setup.
        self.device_list: list[dict[str, str]] = []
        # Built from the saved descriptions: the cloud did not answer at start.
        self.from_cache = False
        # The start read the cloud and fell back to the saved descriptions: the
        # background read waits CACHE_RETRY_FIRST s first. Without the pause a
        # cloud that fails such a start and answers the check right after it
        # reloads the entry again and again (a unit added in the app).
        self.sync_delayed = False
        # MACs (lower case) of units Home Assistant knows and the cloud no
        # longer returns, which a setup has already stopped waiting for. Kept
        # with the saved descriptions, so the next start does not wait again.
        self.missing: set[str] = set()
        # MACs (lower case) of units the cloud does return, but whose
        # description this code could not read. They are not "missing": no
        # setup waits for them and they must not be deleted as gone.
        self.unreadable: set[str] = set()
        # Called after a full REST refresh and after a push: the statuses are
        # worth saving.
        self.on_refreshed: Callable[[], None] | None = None
        # Called once a start from the saved descriptions has read every unit
        # from the cloud (the cloud took the saved tokens).
        self.on_synced: Callable[[], None] | None = None
        # Causes of failed connection attempts already reported in this
        # outage: a repeat of one goes to the debug log.
        self._outage_causes: set[str] = set()
        # Store of the saved descriptions (set by the entry setup).
        self.cache_store: Any = None
        self._silent_sessions = 0
        self._entry: ConfigEntry | None = None
        self._ws: ClientWebSocketResponse | None = None
        self._connected = asyncio.Event()
        self._wakeup = asyncio.Event()
        self._stopping = False
        self._task: asyncio.Task | None = None
        self._ever_connected = False
        self._disconnected_since: float | None = self._now()
        self._pending: dict[str, dict] = {}  # trace -> command waiting for command_response
        # Order of the commands sent: trace -> its number, and the number of
        # the last command per (mac, attribute) and per mac. A re-send of an
        # unconfirmed command must not undo a newer one (see _superseded).
        self._sent_count = 0
        self._sent_number: dict[str, int] = {}
        self._last_sent: dict[tuple[str, str], int] = {}
        # trace -> errNo of its command_response, for a service call still waiting
        self._waiters: dict[str, asyncio.Future] = {}
        self._unavailable_timer: CALLBACK_TYPE | None = None
        self.reconnects = 0
        self.lost_commands = 0
        self._rebuild_scheduled = False
        # The saved sign-in was rejected and the reauth flow started: once per
        # start, the next start comes with the new sign-in (1.7 stopped as well).
        self._reauth_started = False
        # Entry option: washers and dryers take commands (beta, 1.7).
        self.laundry_control = False

    @staticmethod
    def _now() -> float:
        return time.monotonic()

    def to_dict(self) -> dict:
        return {
            "connected": self.connected,
            "reconnects": self.reconnects,
            "lost_commands": self.lost_commands,
            # The raw page embeds MACs inside JSON strings: keep only the summary.
            "from_cache": self.from_cache,
            "device_list": [
                {"type": d["type"], "title": d["title"]} for d in self.device_list
            ],
            "devices": [device.to_dict() for device in self.devices],
        }

    @property
    def connected(self) -> bool:
        return self._connected.is_set() and self._ws is not None and not self._ws.closed

    @property
    def cloud_available(self) -> bool:
        """False once the WebSocket has been down for too long (#103)."""
        if self.connected:
            return True
        since = self._disconnected_since
        return since is None or self._now() - since < C.WS_UNAVAILABLE_AFTER

    def get_device_by_id(self, id_: str) -> HaierDevice | None:
        id_ = str(id_).lower()
        return next((d for d in self.devices if str(d.device_id).lower() == id_), None)

    # --- setup / teardown ---

    async def async_setup(self) -> None:
        """Load devices (the tokens are loaded by the entry setup). Raises HaierError subclasses."""
        self.backend_data = await self.client.async_get_devices_page()
        found = parse_device_list(self.backend_data)
        if not found:
            raise HaierEmptyDeviceList("Device list is empty")
        described: list[tuple[dict, dict]] = []
        for info in found:
            try:
                status = await self.client.async_get_device_status(info["mac"])
            except HaierApiError as err:
                _LOGGER.warning("Skipping device %s: %s", info["title"] or info["mac"], err)
                continue
            described.append((info, status))
        if not described:
            raise HaierEmptyDeviceList("No device status could be read")
        # Devices are kept only once all of them are read: a failed setup
        # leaves nothing behind for the fallback to the saved descriptions.
        self.devices = await self._async_build_devices(described)
        self.device_list = found

    async def async_setup_from_cache(self, cached: Any) -> None:
        """Build the devices from the descriptions of the last good start.

        Raises ValueError when the saved data is not usable.
        """
        items = cached.get("devices") if isinstance(cached, dict) else None
        if not isinstance(items, list) or not items:
            raise ValueError("no saved devices")
        described = []
        for item in items:
            # "config": the same description as 1.x saved it, under the
            # same Store key ("status" first: what 2.0 saves).
            status = item.get("status", item.get("config")) if isinstance(item, dict) else None
            if not isinstance(status, dict):
                raise ValueError("a saved device has no status")
            info = {key: str(item.get(key) or "") for key in ("mac", "type", "serial", "title")}
            if not info["mac"]:
                raise ValueError("a saved device has no MAC")
            described.append((info, status))
        self.devices = await self._async_build_devices(described)
        self.device_list = [info for info, _ in described]
        self.missing = saved_missing(cached)
        self.from_cache = True
        for device in self.devices:
            device.synced = False

    async def _async_build_devices(self, described: list[tuple[dict, dict]]) -> list[HaierDevice]:
        devices = []
        failure: Exception | None = None
        for info, status in described:
            try:
                # Profiles are YAML files: build the device off the event loop.
                device = await self.hass.async_add_executor_job(partial(
                    HaierDevice.create,
                    haier=self,
                    device_type=info["type"],
                    device_mac=info["mac"],
                    device_serial=info["serial"],
                    device_title=info["title"],
                    backend_data=status,
                ))
            except Exception as err:  # noqa: BLE001 - one odd description must not take the others down
                # A description this code cannot read (a new device type, a
                # changed format) used to fail the whole entry, every working
                # unit included, with no retry.
                failure = err
                self.unreadable.add(str(info["mac"]).lower())
                _LOGGER.warning(
                    "Skipping device %s: its description is not readable (%s: %s); "
                    "the debug log has the details",
                    info["title"] or info["mac"], type(err).__name__, err,
                )
                _LOGGER.debug("Description of %s failed to load", info["title"] or info["mac"], exc_info=err)
                continue
            device.device_type = info["type"]
            devices.append(device)
            _LOGGER.info("Added device: %s", device)
        if failure is not None and not devices:
            # Nothing at all could be read: that is not an outage a retry or
            # the saved descriptions would cure, so it fails as it did before.
            raise failure
        return devices

    def cache_dict(self) -> dict:
        """What the next start needs when the cloud does not answer."""
        return {
            "devices": [
                {
                    "mac": device.device_id,
                    "type": device.device_type,
                    "serial": device.device_serial or "",
                    "title": device.device_name or "",
                    "status": cacheable_description(device.status_data),
                }
                for device in self.devices
                if isinstance(device.status_data, dict)
                and str(device.device_id).lower() not in self.missing
            ],
            "missing": sorted(self.missing),
        }

    def start(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._task = entry.async_create_background_task(
            self.hass, self._async_ws_loop(), f"{C.DOMAIN} websocket"
        )
        # Push is the main source of state; this only catches a silent session
        # that stays open but stops delivering statuses.
        entry.async_on_unload(async_track_time_interval(
            self.hass, self._async_periodic_refresh, C.REST_REFRESH_INTERVAL,
            name=f"{C.DOMAIN} refresh", cancel_on_shutdown=True,
        ))
        entry.async_on_unload(async_track_time_interval(
            self.hass, self._async_check_device_list, C.DEVICE_LIST_REFRESH_INTERVAL,
            name=f"{C.DOMAIN} device list", cancel_on_shutdown=True,
        ))
        if self.from_cache:
            self._create_task(self._async_sync_from_cloud(), "initial refresh")

    async def _async_sync_from_cloud(self) -> None:
        """Started from the saved descriptions: read the real state in the background.

        As 1.4+ did: the entities come up at once, without waiting for the
        cloud, and are unavailable until their unit has been read. Retried with
        a growing pause until every unit has been read. A unit the cloud now
        describes differently, one added or removed in the app reloads the
        entry, and that start reads the cloud (RELOAD_FROM_CLOUD).
        """
        delay = C.CACHE_RETRY_FIRST
        if self.sync_delayed:
            await asyncio.sleep(delay)
        while not self._stopping:
            failed = await self._async_resync()
            if self._stopping or self._rebuild_scheduled or self._reauth_started:
                return
            if not failed:
                break
            _LOGGER.info("Haier Evo: no fresh state yet for %d device(s), next try in %s s", len(failed), delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, C.CACHE_RETRY_MAX)
        else:
            return
        self.from_cache = False
        await self._async_check_device_list()
        if self.on_synced is not None and not self._rebuild_scheduled:
            self.on_synced()

    def _start_reauth(self) -> None:
        """Once per start: every later request only finds the same dead sign-in."""
        if self._reauth_started:
            return
        self._reauth_started = True
        _LOGGER.error("Haier Evo rejected the saved sign-in, re-authentication is required")
        if self._entry is not None and not self._stopping:
            self._entry.async_start_reauth(self.hass)

    def _create_task(self, coro: Any, name: str) -> None:
        # Entry background tasks are cancelled on unload.
        if self._entry is not None:
            self._entry.async_create_background_task(self.hass, coro, f"{C.DOMAIN} {name}")
        else:
            self.hass.async_create_task(coro, eager_start=False)

    async def async_stop(self) -> None:
        self._stopping = True
        self._cancel_unavailable_timer()
        self._wakeup.set()
        ws = self._ws
        if ws is not None and not ws.closed:
            await ws.close()
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    # --- websocket ---

    def _mark_disconnected(self) -> None:
        self._connected.clear()
        if self._disconnected_since is None:
            self._disconnected_since = self._now()
        self._notify_all()
        # cloud_available is only evaluated when an entity writes its state:
        # without this nudge nothing would turn the entities unavailable.
        if self._unavailable_timer is None and not self._stopping:
            self._unavailable_timer = async_call_later(
                self.hass, C.WS_UNAVAILABLE_AFTER + 1, self._on_unavailable
            )

    @callback
    def _on_unavailable(self, _now: Any) -> None:
        self._unavailable_timer = None
        self._notify_all()

    def _cancel_unavailable_timer(self) -> None:
        if self._unavailable_timer is not None:
            self._unavailable_timer()
            self._unavailable_timer = None

    def _notify_all(self) -> None:
        for device in self.devices:
            device.notify()

    async def _async_ws_loop(self) -> None:
        backoff = C.WS_BACKOFF_MIN
        while not self._stopping:
            started = self._now()
            delay = 0.0
            try:
                await self._async_ws_session()
            except asyncio.CancelledError:
                raise
            except HaierAuthError:
                self._mark_disconnected()
                self._start_reauth()
                return
            except HaierRateLimitError as err:
                # The text carries the seconds left: one cause whatever they are.
                self._log_outage("Haier Evo: %s", err, cause="rate limit")
                delay = err.retry_after
            except HaierError as err:
                self._log_outage("Haier Evo WebSocket: %s", err)
            except Exception:  # noqa: BLE001 - keep the loop alive whatever happens
                _LOGGER.exception("Unexpected Haier Evo WebSocket error")
            self._mark_disconnected()
            if self._stopping:
                break
            if self._now() - started > C.WS_STABLE_AFTER:
                backoff = C.WS_BACKOFF_MIN
            delay = max(delay, backoff)
            backoff = min(backoff * 2, C.WS_BACKOFF_MAX)
            # A pending command wakes the loop up early: it is waiting for us.
            with suppress(TimeoutError):
                await asyncio.wait_for(self._wakeup.wait(), delay)
            self._wakeup.clear()

    def _log_outage(self, message: str, err: Exception, cause: str | None = None) -> None:
        """One warning per cause per outage.

        The attempts go on once a minute for as long as the outage lasts. A
        cause not seen in it yet is still a warning: an outage that starts as
        a network error and turns into a refused handshake must show that.
        """
        cause = cause or str(err)
        log = _LOGGER.debug if cause in self._outage_causes else _LOGGER.warning
        self._outage_causes.add(cause)
        log(message, err)

    async def _async_ws_session(self) -> None:
        token = await self.client.async_get_token()
        try:
            # Bounded: without it a stuck TLS or upgrade held the connection, and
            # every command waiting for it, for up to aiohttp's 300 s (1.4).
            async with asyncio.timeout(C.WS_CONNECT_TIMEOUT):
                ws = await self._session.ws_connect(
                    self.client.ws_url(token), heartbeat=C.WS_HEARTBEAT
                )
        except WSServerHandshakeError as err:
            # The exception text contains the URL, i.e. the token: never log it.
            if err.status in (401, 403):
                # renewed for the next try, unless issued seconds ago (1.7)
                self.client.token_rejected(token)
            raise HaierConnectionError(f"WebSocket handshake failed: HTTP {err.status}") from None
        except (ClientError, TimeoutError) as err:
            raise HaierConnectionError(f"WebSocket connect failed: {type(err).__name__}") from None

        outage = None if self._disconnected_since is None else self._now() - self._disconnected_since
        received = False

        def silent_check() -> None:
            # The cloud pushes statuses within seconds of an opening; a session
            # nothing came over is accepted but not served (JampireX, AGrigorii):
            # commands get no answer, pushes never come. Replace it.
            if received or ws.closed or self._ws is not ws:
                return
            self._silent_sessions += 1
            log = _LOGGER.info if self._silent_sessions == 1 else _LOGGER.debug
            log("Nothing came over the WebSocket since it opened, opening a new one "
                "(silent sessions in a row: %s)", self._silent_sessions)
            if self._silent_sessions == C.WS_SILENT_SESSIONS_FRESH_TOKEN:
                self.client.invalidate_access_token(token)
            self._connected.clear()
            self._create_task(ws.close(), "close silent")

        silent_timer = self.hass.loop.call_later(
            C.WS_SILENT_SESSION_TIMEOUT * 2 ** min(self._silent_sessions, 3), silent_check
        )
        self._ws = ws
        self._disconnected_since = None
        self._cancel_unavailable_timer()
        self._connected.set()
        if self._ever_connected:
            self.reconnects += 1
        if self._outage_causes:
            self._outage_causes.clear()
            _LOGGER.info("Haier Evo cloud connection is back")
        _LOGGER.debug("WebSocket connected")
        if self._ever_connected and outage is not None and outage > C.WS_RESYNC_AFTER:
            self._create_task(self._async_resync(), "resync")
        self._ever_connected = True
        self._notify_all()
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    if not received:
                        received = True
                        self._silent_sessions = 0
                    self._handle_message(msg.data)
                elif msg.type == WSMsgType.ERROR:
                    break
        finally:
            silent_timer.cancel()
            self._connected.clear()
            if self._ws is ws:
                self._ws = None
            if not ws.closed:
                await ws.close()
            _LOGGER.debug("WebSocket closed, code %s", ws.close_code)
        if ws.close_code == C.WS_CLOSE_TOKEN_REFUSED:
            # The gateway took the upgrade and then refused the token: without
            # this the same token was tried until it expired locally (up to an hour).
            _LOGGER.info("The WebSocket was closed for the token (code %s), renewing it", ws.close_code)
            self.client.token_rejected(token)

    async def _async_periodic_refresh(self, _now: Any = None) -> None:
        await self._async_resync()

    def _schedule_rebuild(self, reason: str) -> None:
        """The entities are built from what the cloud said at start: build them anew."""
        if self._entry is None or self._stopping or self._rebuild_scheduled:
            return
        self._rebuild_scheduled = True
        RELOAD_FROM_CLOUD.add(self._entry.entry_id)
        _LOGGER.info("Haier Evo: %s, reloading the integration", reason)
        self.hass.config_entries.async_schedule_reload(self._entry.entry_id)

    async def _async_check_device_list(self, _now: Any = None) -> None:
        """A unit added in the Evo app shows up without a manual reload (1.6).

        Units the cloud lists but refuses to describe (403) are not
        new: the setup skips them as well, a reload would only find them again.
        """
        try:
            found = parse_device_list(await self.client.async_get_devices_page())
        except HaierAuthError:
            self._start_reauth()
            return
        except HaierError as err:
            _LOGGER.debug("Device list check failed: %s", err)
            return
        listed = {info["mac"].lower() for info in found}
        if found:
            # A unit gone from the list that the cloud refuses to describe is
            # gone from the account (1.7): without a reload its entities kept
            # showing the last state as live. One the list merely left out but
            # that still answers is kept (the list is not reliable, #105).
            for device in self.devices:
                mac = str(device.device_id).lower()
                if mac in listed:
                    continue
                try:
                    await self.client.async_get_device_status(device.device_id)
                except HaierApiError:
                    # Waited for once, not at every start (see _async_check_complete).
                    self.missing.add(mac)
                    if self.cache_store is not None:
                        await self.cache_store.async_save(self.cache_dict())
                    self._schedule_rebuild(f"{device.device_name} is no longer in the app")
                    return
                except HaierError as err:
                    _LOGGER.debug("Device list check failed: %s", err)
                    return
        known = {str(d.device_id).lower() for d in self.devices} | self.unreadable
        for info in found:
            if info["mac"].lower() in known:
                continue
            try:
                await self.client.async_get_device_status(info["mac"])
            except HaierApiError:
                continue
            except HaierError as err:
                _LOGGER.debug("Device list check failed: %s", err)
                return
            self._schedule_rebuild(f"a new device in the app ({info['title'] or info['type']})")
            return

    async def _async_resync(self, devices: list[HaierDevice] | None = None) -> set[str]:
        """Re-read device statuses via REST (after an outage, a lost command, or periodically).

        Returns the MACs whose status could not be read for now (network,
        cloud errors); a unit the cloud refuses to describe is not among them:
        the device list check decides on it.
        """
        refused = False
        failed: set[str] = set()
        for device in devices if devices is not None else self.devices:
            started = self._now()
            try:
                status = await self.client.async_get_device_status(device.device_id)
            except HaierAuthError:
                self._start_reauth()
                return {str(d.device_id).lower() for d in self.devices}
            except HaierError as err:
                _LOGGER.debug("Resync of %s failed: %s", device.device_name, err)
                if isinstance(err, HaierApiError):
                    refused = True
                else:
                    failed.add(str(device.device_id).lower())
                continue
            if device.described_differently(status):
                # New firmware, attributes or rules: the entities and the
                # profile were built from the old description (1.6).
                self._schedule_rebuild(f"the cloud describes {device.device_name} differently")
            if devices is None and device.synced and device.changed_at > started:
                # A push or a command came in while the request was in flight:
                # the REST snapshot is older than what the entity already shows.
                # A targeted resync (lost or rejected command) applies anyway:
                # it exists to undo an optimistic value.
                continue
            device.update_from_backend(status)
            device.notify()
        if devices is None and refused:
            # A refused description may be a unit removed in the app: the
            # device list says, not in six hours but now.
            await self._async_check_device_list()
        if devices is None and self.on_refreshed is not None:
            self.on_refreshed()
        return failed

    def _handle_message(self, raw: str) -> None:
        try:
            message = json.loads(raw)
        except ValueError:
            _LOGGER.debug("Ignoring non-JSON WebSocket message")
            return
        if not isinstance(message, dict):
            return
        _LOGGER.debug("Received WSS message: %s", message)
        try:
            if message.get("event") == "command_response":
                self._handle_ack(message)
            device = self.get_device_by_id(message.get("macAddress") or "")
            if device is None:
                mac = str(message.get("macAddress") or "").lower()
                if mac in self.missing:
                    # A unit a setup stopped waiting for is alive after all.
                    # Forget that it was given up on, or the next start would
                    # not wait for it either if the cloud is slow to list it.
                    self.missing.discard(mac)
                    _LOGGER.info(
                        "Haier Evo unit %s is back in the cloud; reload the integration to add it", mac
                    )
                    if self.on_refreshed is not None:
                        self.on_refreshed()
                else:
                    _LOGGER.debug("Message for an unknown device: %s", message.get("event"))
                return
            device.on_message(message)
            if message.get("event") in ("status", "deviceStatusEvent") and self.on_refreshed is not None:
                # What came over the socket is worth saving too: a stop of Home
                # Assistant unloads nothing, so without this the file kept the
                # last REST snapshot, up to 15 minutes old.
                self.on_refreshed()
        except Exception:  # noqa: BLE001 - one bad message must not kill the socket
            _LOGGER.exception("Failed to handle %s message", message.get("event"))

    def _handle_ack(self, message: dict) -> None:
        payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
        trace = message.get("trace") or payload.get("trace")
        trace = trace if isinstance(trace, str) else None
        err = message.get("errNo", payload.get("errNo"))
        command = None
        if trace in self._pending:
            command = self._pending.pop(trace)
        elif trace:
            # The cloud also echoes responses to commands from the Evo app or
            # Alice. Counting one for our pending command would hide a lost one.
            _LOGGER.debug("command_response for a foreign command, errNo=%s", err)
            return
        else:
            # No trace in the answer: count it for the oldest command to that unit.
            mac = str(message.get("macAddress") or "").lower()
            for pending_trace, pending in self._pending.items():
                if str(pending.get("macAddress") or "").lower() == mac:
                    command = self._pending.pop(pending_trace)
                    trace = pending_trace
                    break
        waiter = self._waiters.pop(trace, None) if trace else None
        if waiter is not None and not waiter.done():
            # The service call is still waiting: it checks a refusal itself and
            # reports it to the user.
            waiter.set_result(err)
            return
        if _rejected(err):
            mac = message.get("macAddress") or (command or {}).get("macAddress") or ""
            device = self.get_device_by_id(mac)
            if command is None or device is None:
                _LOGGER.warning("Haier Evo rejected a command, errNo=%s", err)
            if device is not None:
                # The entity already shows the requested value: read the real one back.
                self._create_task(self._async_check_rejected(device, command, err), "resync")

    async def _async_check_rejected(self, device: HaierDevice, command: dict | None, err: Any) -> bool:
        """Read the real state after a non-zero errNo; warn only if the unit lacks the values.

        True when the unit already has every value of the command: the cloud
        answers so for values that are set already, no refusal.
        """
        try:
            status = await self.client.async_get_device_status(device.device_id)
        except HaierError as error:
            if command is not None:
                _LOGGER.warning("Haier Evo rejected a command, errNo=%s", err)
            _LOGGER.debug("Resync of %s failed: %s", device.device_name, error)
            return False
        applied = command is not None and command_applied(command, status)
        if command is not None:
            if applied:
                _LOGGER.debug("errNo=%s, but %s already has the requested values", err, device.device_name)
            else:
                _LOGGER.warning("Haier Evo rejected a command, errNo=%s", err)
        device.update_from_backend(status)
        device.notify()
        return applied

    async def _async_wait_answer(self, trace: str, payload: dict) -> None:
        """Wait for the cloud's answer to a command, up to WS_COMMAND_WAIT s.

        A refusal (non-zero errNo, and the unit does not have the values)
        raises: the user sees an error instead of a value sliding back a
        second later (idea of 1.4, without its blocking threads). No
        answer in time is no error: the watchdog re-sends and reads back.
        """
        waiter = self._waiters.get(trace)
        if waiter is None:
            return
        try:
            async with asyncio.timeout(C.WS_COMMAND_WAIT):
                err = await asyncio.shield(waiter)
        except TimeoutError:
            return
        finally:
            self._waiters.pop(trace, None)
        if not _rejected(err):
            return
        device = self.get_device_by_id(payload.get("macAddress") or "")
        if device is not None and await self._async_check_rejected(device, payload, err):
            return
        raise HaierCommandRejected(
            translation_domain=C.DOMAIN,
            translation_key="command_rejected",
            translation_placeholders={
                "device": str(device.device_name if device else payload.get("macAddress")),
                "code": str(err),
            },
        )

    async def _async_ack_watchdog(
        self, trace: str, payload: dict, sent_ws: ClientWebSocketResponse
    ) -> None:
        """No command_response in time: re-send once on a fresh connection.

        Catches the session that accepts writes but no longer delivers them,
        which a heartbeat cannot see (#89, idea from the JampireX fork).
        Commands are absolute values, so a duplicate is harmless.
        """
        await asyncio.sleep(C.WS_ACK_TIMEOUT)
        if trace not in self._pending:
            self._sent_number.pop(trace, None)
            return
        if self._superseded(trace, payload):
            # 22 °C unconfirmed, 24 °C sent since: re-sending 22 would set the
            # unit to it while the UI shows 24. The newer command has a
            # watchdog of its own.
            self._pending.pop(trace, None)
            self._sent_number.pop(trace, None)
            _LOGGER.debug("An unconfirmed command was superseded by a newer one, not re-sending it")
            return
        self._sent_number.pop(trace, None)
        _LOGGER.warning("Haier Evo did not confirm a command in %s s, re-sending it", C.WS_ACK_TIMEOUT)
        # Only the socket the command went out on is suspect: commands sent
        # together must not tear down the fresh connection one after another.
        if sent_ws is self._ws and not sent_ws.closed:
            self._connected.clear()
            await sent_ws.close()
        try:
            await self.async_send(payload, retry=True)
        except HomeAssistantError:
            pass
        else:
            await asyncio.sleep(C.WS_ACK_TIMEOUT)
        if self._pending.pop(trace, None) is None:
            return
        self.lost_commands += 1
        device = self.get_device_by_id(payload.get("macAddress") or "")
        _LOGGER.error(
            "Haier Evo did not confirm a command for %s, reading the real state back",
            device.device_name if device else "unknown device",
        )
        if device is not None:
            await self._async_resync([device])

    def _superseded(self, trace: str, payload: dict) -> bool:
        """A newer command went to the same unit since: to one of its attributes,
        or, for a unit whose commands are actions (start, cancel of a washer),
        any newer command at all."""
        number = self._sent_number.get(trace)
        if number is None:
            return False
        mac = str(payload.get("macAddress") or "").lower()
        device = self.get_device_by_id(mac)
        keys = [(mac, "")] if device is not None and not device.absolute_commands else []
        keys += [(mac, name) for name in command_names(payload)]
        return any(self._last_sent.get(key, 0) > number for key in keys)

    def _count_sent(self, trace: str, payload: dict) -> None:
        self._sent_count += 1
        self._sent_number[trace] = self._sent_count
        mac = str(payload.get("macAddress") or "").lower()
        self._last_sent[(mac, "")] = self._sent_count
        for name in command_names(payload):
            self._last_sent[(mac, name)] = self._sent_count

    async def _async_wait_connected(self, wait: float) -> ClientWebSocketResponse | None:
        if self.connected:
            return self._ws
        # The socket may be closed already while its session has not cleaned up
        # yet: the event is still set then, and waiting on it would return at
        # once and fail the command instead of waiting for the reconnect.
        self._connected.clear()
        self._wakeup.set()
        try:
            await asyncio.wait_for(self._connected.wait(), wait)
        except TimeoutError:
            return None
        return self._ws if self.connected else None

    async def async_send(self, payload: dict, *, retry: bool = False) -> None:
        """Send a command, reconnecting first if needed (#89, #102).

        The old client wrote into a socket that the server had already closed
        and silently dropped the command. Here a closed socket is detected by
        the heartbeat, the command waits for the reconnect, and a failed write
        is retried once on a fresh connection. If it still fails, the service
        call fails loudly instead of pretending it worked.
        """
        data = json.dumps(payload)
        trace = payload.get("trace") if not retry and self._entry is not None else None
        for _attempt in range(2):
            ws = await self._async_wait_connected(C.WS_SEND_TIMEOUT)
            if ws is None:
                break
            if trace:
                # Registered before the write: the answer may beat send_str.
                self._pending[trace] = payload
                self._waiters[trace] = self.hass.loop.create_future()
            try:
                await ws.send_str(data)
            except (ClientError, ConnectionError, RuntimeError) as err:
                _LOGGER.warning("Sending failed (%s), reconnecting", type(err).__name__)
                if trace:
                    self._pending.pop(trace, None)
                    self._waiters.pop(trace, None)
                self._connected.clear()
                with suppress(Exception):
                    await ws.close()
                continue
            except BaseException:
                # Cancelled mid-write: no watchdog will ever clear the entry.
                if trace:
                    self._pending.pop(trace, None)
                    self._waiters.pop(trace, None)
                raise
            _LOGGER.debug("Sent message: %s", data)
            if trace:
                self._count_sent(trace, payload)
                self._create_task(self._async_ack_watchdog(trace, payload, ws), "ack")
                await self._async_wait_answer(trace, payload)
            return
        raise HaierCommandError(translation_domain=C.DOMAIN, translation_key="command_failed")
