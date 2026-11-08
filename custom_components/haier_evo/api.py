from __future__ import annotations
import requests
import json
import re
import threading
import time
import uuid
import socket
import ssl
import weakref
from enum import Enum
from typing import Callable
from datetime import datetime, timezone, timedelta
from websocket import WebSocketApp, WebSocket
from requests.adapters import HTTPAdapter
from requests.exceptions import ConnectionError, Timeout, HTTPError
from functools import wraps
from urllib.parse import urlparse, urljoin, parse_qs
from urllib3.exceptions import NewConnectionError
from urllib3.util.retry import Retry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.components.climate.const import ClimateEntityFeature, HVACMode, SWING_OFF, PRESET_NONE
from homeassistant.util.ssl import get_default_context
from .logger import _LOGGER
from .limits import ResettableLimits
from . import config as CFG # noqa
from . import const as C # noqa


class InvalidAuth(HomeAssistantError):
    """Signing in or refreshing the tokens failed (cloud or network error)."""

class ReauthRequired(HomeAssistantError):
    """No usable tokens and no password: the user has to sign in again."""

class InvalidDevicesList(HomeAssistantError):
    """Error to indicate we cannot connect."""

class AuthError(HTTPError):
    pass

class AuthUserError(HTTPError):
    pass

class AuthValidationError(AuthError):
    pass

class AuthInternalError(AuthError):
    pass

class ManyRequestsError(HTTPError):
    pass

class HaierCommandLocked(HomeAssistantError):
    """The cloud marks the attribute read-only right now (see CFG.Modifiers)."""

class HaierNotConnected(HomeAssistantError):
    """No WebSocket connection to the cloud: the command was not sent."""

class HaierCommandRejected(HomeAssistantError):
    """The cloud answered command_response with a non-zero errNo."""


MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")


class SocketStatus(Enum):
    PRE_INITIALIZATION = 0
    INITIALIZING = 1
    INITIALIZED = 2
    NOT_INITIALIZED = 3


class AuthResponse(object):

    def __init__(self, response: requests.Response):
        self.response = response
        self.json_data = response.json() or {}
        self.data = self.json_data.get("data") or {}
        self.error = self.json_data.get("error")
        self.token = self.data.get("token") or {}

    def __getattr__(self, item):
        if hasattr(self.response, item):
            return getattr(self.response, item)
        raise AttributeError(item)

    def __repr__(self) -> str:
        return self.response.__repr__()

    def raise_for_error(self) -> None:
        if self.error and isinstance(self.error, dict):
            validation = self.error.get("validation") or {}
            if message := validation.get('refreshToken'):
                # noinspection PyTypeChecker
                raise AuthValidationError(message, response=self)
            if message := validation.get('email'):
                # noinspection PyTypeChecker
                raise AuthUserError(message, response=self)
            if message := validation.get('password'):
                # noinspection PyTypeChecker
                raise AuthUserError(message, response=self)
            if message := self.error.get("message"):
                # noinspection PyTypeChecker
                raise AuthInternalError(message, response=self)
            # noinspection PyTypeChecker
            raise AuthError(str(self.error), response=self)
        return None

    @property
    def access_token(self) -> str:
        value = self.token.get("accessToken") if isinstance(self.token, dict) else None
        if not isinstance(value, str) or not value:
            # the value itself is not shown: it may be a token in an odd form
            raise ValueError(f"Bad data: no usable accessToken ({type(value).__name__})")
        return value

    @property
    def access_expire(self) -> datetime:
        # without a readable expiry every request would refresh the token
        return parse_expire(self.token.get("expire")) or datetime.now(timezone.utc) + C.TOKEN_FALLBACK_TTL

    @property
    def refresh_token(self) -> str | None:
        value = self.token.get("refreshToken")
        return value if isinstance(value, str) and value else None

    @property
    def refresh_expire(self) -> datetime | None:
        return parse_expire(self.token.get("refreshExpire"))


# the cloud answers in Moscow time; an expiry without a zone is taken as such
_CLOUD_TZ = timezone(timedelta(hours=3))


def parse_expire(value) -> datetime | None:
    """A token expiry of the auth answer, None when it is missing or unreadable."""
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        try:
            parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z")
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=_CLOUD_TZ)


def firmware_text(value) -> str | None:
    """A firmware version as Home Assistant's device registry takes it: text or None."""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, int | float) and not isinstance(value, bool) and value == value and abs(value) != float("inf"):
        return str(value)
    return None


def device_part(data) -> dict:
    """The part of a description with info, settings and attributes: washers and
    dryers in the cloud's current format nest it in smartDeviceControl, next to
    the app's screens; the other devices have it at the top."""
    data = data if isinstance(data, dict) else {}
    inner = data.get("smartDeviceControl")
    return inner if isinstance(inner, dict) else data


def _limited(name: str):
    """Rate-limit a method of Haier with the limiter of its instance (attribute
    name): every client has its own limits, so a sign-in in the config flow, a
    reload or a second account does not reset the waiting of another one
    (up to 1.7.0 they were shared by the class and reset by any instance)."""
    def decorate(func):
        @wraps(func)
        def wrapper(self, *args, **kwargs):
            limits = getattr(self, name)
            return limits.sleep_and_retry(limits(func))(self, *args, **kwargs)
        return wrapper
    return decorate


def entry_device_id(entry_id: str) -> str:
    """Device-Id of a config entry: the same on every start (it used to be random)."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{C.DOMAIN}:{entry_id}"))


class Haier(object):

    # templates: every instance takes copies of them (see _limited); tests
    # change these to make the waits shorter for the clients they create
    connect_limits = ResettableLimits(calls=1, period=5)
    common_limits = ResettableLimits(
        calls=C.COMMON_LIMIT_CALLS,
        period=C.COMMON_LIMIT_PERIOD,
    )
    auth_login_limits = ResettableLimits(
        calls=C.LOGIN_LIMIT_CALLS,
        period=C.LOGIN_LIMIT_PERIOD,
        max=C.LOGIN_LIMIT_MAX
    )
    auth_refresh_limits = ResettableLimits(
        calls=C.REFRESH_LIMIT_CALLS,
        period=C.REFRESH_LIMIT_PERIOD,
        max=C.REFRESH_LIMIT_MAX
    )

    def __init__(
        self,
        hass: HomeAssistant,
        email: str,
        password: str | None,
        region: str,
        tokens: dict | None = None,
        token_saver: Callable[[dict], None] | None = None,
        on_auth_failed: Callable[[], None] | None = None,
        device_id: str | None = None,
    ) -> None:
        """password may be None: the tokens are enough until the cloud rejects
        the refresh token, then on_auth_failed asks the user to sign in again.
        device_id: the entry's own (entry_device_id), random without an entry."""
        self._lock = threading.Lock()
        self._pull_data = None
        self._device_id = device_id or str(uuid.uuid4())
        self._token_issued = 0.0  # time.monotonic() of the last sign-in or refresh
        self._ws_token: str | None = None  # the token the websocket connected with
        self.hass: HomeAssistant = hass
        self.devices: list[HaierDevice] = []
        self.email: str = email
        self.password: str = password
        self.region: str = region
        self._token_saver = token_saver
        self._on_auth_failed = on_auth_failed
        self.token: str | None = None
        self.tokenexpire: datetime | None = None
        self.refreshtoken: str | None = None
        self.refreshexpire: datetime | None = None
        self.socket_app: WebSocketApp | None = None
        self.disconnect_requested = False
        self.socket_status: SocketStatus = SocketStatus.PRE_INITIALIZATION
        self.socket_thread = None
        self._session = self._build_session()
        self._session.headers["Device-Id"] = self._device_id  # the sign-in too, as the app does
        self._connected = threading.Event()
        self._ever_connected = False
        # messages over the current websocket session, and sessions in a row
        # the cloud sent nothing over (see _check_silent_session)
        self._session_messages = 0
        self._silent_sessions = 0
        self._silent_timer: threading.Timer | None = None
        self._down_since: float | None = None  # time.monotonic() when the socket was lost
        # time.monotonic() since when there is no websocket (from the start
        # until the first connection too): entities go unavailable after
        # C.WS_UNAVAILABLE_AFTER s, a timer tells them when
        self._offline_since: float | None = time.monotonic()
        self._offline_timer: threading.Timer | None = None
        self._offline_lock = threading.Lock()
        # trace -> {"event": Event, "mac": str, "err": errNo} for commands awaiting command_response
        self._pending: dict[str, dict] = {}
        self._pending_lock = threading.Lock()
        # commands waiting for the socket / an answer hold executor threads
        self._command_slots = threading.BoundedSemaphore(C.WS_MAX_PENDING_COMMANDS)
        # control of washers and dryers (beta): the entry's option, set by async_setup_entry
        self.laundry_control = False
        cls = type(self)
        self.connect_limits = cls.connect_limits.copy()
        self.common_limits = cls.common_limits.copy()
        self.auth_login_limits = cls.auth_login_limits.copy()
        self.auth_refresh_limits = cls.auth_refresh_limits.copy()
        self.set_tokens(tokens)

    @staticmethod
    def _build_session() -> requests.Session:
        """HTTP session: keep-alive (fewer new TCP connections) and retries.

        Connection errors are retried for every method (the request has not
        reached the server), read errors only for idempotent GETs.
        """
        retries = Retry(
            total=None,
            connect=C.API_CONNECT_RETRIES,
            read=C.API_READ_RETRIES,
            status=0,
            other=0,
            backoff_factor=0.5,
            raise_on_status=False,
        )
        session = requests.Session()
        # no redirects: requests would take X-Auth-Token and Device-Id along to
        # another host (it drops only Authorization), and the sign-in form with
        # the password on 307/308; the cloud's endpoints never redirect
        session.max_redirects = 0
        session.mount("https://", HTTPAdapter(max_retries=retries))
        session.headers.update({
            "User-Agent": "evo-mobile",
            "Platform": "android",
            "Accept": "*/*",
        })
        return session

    @staticmethod
    def _timeout() -> tuple[float, float]:
        return C.API_CONNECT_TIMEOUT, C.API_READ_TIMEOUT

    def to_dict(self) -> dict:
        return {
            "socket_status": getattr(self.socket_status, "value", None),
            "backend_data": self._pull_data,
            "devices": [device.to_dict() for device in self.devices]
        }

    def set_tokens(self, data: dict | None) -> None:
        data = data if isinstance(data, dict) else {}

        def parse(value):
            try:
                return datetime.fromisoformat(value) if value else None
            except (TypeError, ValueError):
                return None

        self.token = data.get("token") or None
        self.tokenexpire = parse(data.get("tokenexpire"))
        self.refreshtoken = data.get("refreshtoken") or None
        self.refreshexpire = parse(data.get("refreshexpire"))

    def tokens_dict(self) -> dict:
        return {
            "token": self.token,
            "tokenexpire": self.tokenexpire.isoformat() if self.tokenexpire else None,
            "refreshtoken": self.refreshtoken,
            "refreshexpire": self.refreshexpire.isoformat() if self.refreshexpire else None,
        }

    def save_tokens(self) -> None:
        if self._token_saver is None:
            return
        try:
            self._token_saver(self.tokens_dict())
        except Exception as e:
            _LOGGER.error(f"Failed to save tokens: {e!r}")

    def clear_tokens(self) -> None:
        self.token = None
        self.tokenexpire = None
        self.refreshtoken = None
        self.refreshexpire = None
        self.save_tokens()

    def reset_limits(self) -> None:
        self.connect_limits.reset()
        self.common_limits.reset()
        self.auth_login_limits.reset()
        self.auth_refresh_limits.reset()

    @property
    def cloud_available(self) -> bool:
        """False once the websocket has been down for C.WS_UNAVAILABLE_AFTER s."""
        since = self._offline_since
        return since is None or time.monotonic() - since < C.WS_UNAVAILABLE_AFTER

    def _went_offline(self) -> None:
        """The websocket is gone: arm the timer that makes the entities unavailable."""
        with self._offline_lock:
            if self._offline_since is None:
                self._offline_since = time.monotonic()
            if self._offline_timer is not None or self.disconnect_requested:
                return
            delay = max(1.0, self._offline_since + C.WS_UNAVAILABLE_AFTER + 1 - time.monotonic())
            self._offline_timer = timer = threading.Timer(delay, self._offline_timer_fired)
            timer.daemon = True
            timer.start()

    def _offline_timer_fired(self) -> None:
        with self._offline_lock:
            self._offline_timer = None
        if not self.disconnect_requested:
            self.write_ha_state()  # available is only read when an entity writes its state

    def _came_online(self) -> None:
        with self._offline_lock:
            was_offline = not self.cloud_available
            self._offline_since = None
            if self._offline_timer is not None:
                self._offline_timer.cancel()
                self._offline_timer = None
        if was_offline:
            self.write_ha_state()

    def stop(self) -> None:
        self.disconnect_requested = True
        self._connected.clear()
        self._cancel_silent_check()
        with self._offline_lock:
            if self._offline_timer is not None:
                self._offline_timer.cancel()
                self._offline_timer = None
        self.reset_limits()
        self._end_session(self.socket_app)
        self._session.close()  # idle keep-alive connections

    @_limited("common_limits")
    def make_request(self, method: str, url: str, **kwargs) -> requests.Response:
        try:
            if self.disconnect_requested:
                raise RuntimeError("Service already stopped")
            # Setting a default timeout for requests
            kwargs.setdefault('timeout', self._timeout())
            resp = self._session.request(method, url, **kwargs)
            # _LOGGER.debug(resp.text)
            # Handling 429 Too Many Requests with retry
            if resp.status_code == 429:
                raise ManyRequestsError("429 Too Many Requests", response=resp)
            # Raise for other HTTP errors
            resp.raise_for_status()
            return resp
        except (ConnectionError, NewConnectionError, socket.gaierror) as e:
            _LOGGER.error(f"Network error occurred: {e}")
            raise e  # Re-raise to allow retry mechanisms to handle this
        except Timeout as e:
            _LOGGER.error(f"Request timed out: {e}")
            raise e
        except HTTPError as e:
            _LOGGER.error(f"HTTP error occurred: {e}")
            raise e

    @_limited("auth_login_limits")
    def auth_login(self) -> AuthResponse:
        try:
            path = urljoin(C.API_PATH, C.API_LOGIN.format(region=self.region))
            _LOGGER.debug(f"Logging in to {path}")
            response = AuthResponse(self.make_request('POST', path, data={
                'email': self.email,
                'password': self.password
            }))
            # _LOGGER.info(f"Login status code: {response.status_code}")
            response.raise_for_error()
        except ManyRequestsError as e:
            self.auth_login_limits.add_period(C.LOGIN_LIMIT_429)
            raise e
        except AuthInternalError as e:
            _LOGGER.error(str(e))
            self.auth_login_limits.add_period(C.LOGIN_LIMIT_500)
            response = e.response
        except AuthUserError as e:
            self.disconnect_requested = True
            raise e
        else:
            self.auth_login_limits.set_period()
        finally:
            self.auth_refresh_limits.reset()
        return response

    @_limited("auth_refresh_limits")
    def auth_refresh(self) -> AuthResponse:
        try:
            path = urljoin(C.API_PATH, C.API_TOKEN_REFRESH.format(region=self.region))
            _LOGGER.debug(f"Refreshing token in to {path}")
            response = AuthResponse(self.make_request('POST', path, data={
                'refreshToken': self.refreshtoken
            }))
            # _LOGGER.info(f"Refresh status code: {response.status_code}")
            response.raise_for_error()
        except ManyRequestsError as e:
            self.auth_refresh_limits.add_period(C.REFRESH_LIMIT_429)
            raise e
        except AuthValidationError as e:
            _LOGGER.error(str(e))
            self.clear_tokens()
            raise e
        except AuthInternalError as e:
            _LOGGER.error(str(e))
            self.auth_refresh_limits.add_period(C.REFRESH_LIMIT_500)
            response = e.response
        else:
            self.auth_refresh_limits.set_period()
        finally:
            self.auth_login_limits.reset()
        return response

    def login(self, refresh: bool = False) -> None:
        """Get new tokens: refresh them, or sign in with the password.

        A rejected refresh token falls back to the password. The password is
        not kept after setup, so without it ReauthRequired is raised.
        """
        for attempt in (1, 2):
            resp = None
            try:
                if refresh and self.refreshtoken:
                    resp = self.auth_refresh()
                else:
                    if not self.password:
                        raise ReauthRequired("Haier Evo: no valid tokens, sign in again")
                    resp = self.auth_login()
                if not resp:
                    raise ValueError("No response from login")
                # called under self._lock (auth) or before any thread runs (config flow)
                self.token = resp.access_token
                self.tokenexpire = resp.access_expire
                if refresh_token := resp.refresh_token:
                    # an answer without a new refresh token keeps the old one
                    self.refreshtoken, self.refreshexpire = refresh_token, resp.refresh_expire
                self._token_issued = time.monotonic()
                self.save_tokens()
                _LOGGER.debug("Successful update tokens")
                return
            except AuthValidationError as e:
                # refresh token rejected (tokens are cleared): try the password
                if attempt == 2:
                    raise ReauthRequired(str(e)) from e
                refresh = False
            except (ReauthRequired, AuthUserError):
                raise
            except Exception as e:
                _LOGGER.error(f"Failed to login/refresh token, response was: {resp}, err: {e!r}")
                raise InvalidAuth() from e

    def auth(self) -> None:
        with self._lock:
            now = datetime.now(timezone.utc)
            # renewed a minute early: a token must not expire on its way to the cloud
            if self.token and self.tokenexpire and self.tokenexpire - C.TOKEN_MARGIN > now:
                return None
            if self.refreshtoken and (self.refreshexpire is None or self.refreshexpire > now):
                return self.login(refresh=True)
            return self.login()

    def pull_data_from_api(self) -> dict:
        self.auth()
        response = None
        try:
            devices_path = urljoin(C.API_PATH, C.API_DEVICES.format(region=self.region))
            _LOGGER.debug(f"Getting devices, url: {devices_path}")
            response = self._session.get(devices_path, headers={
                'X-Auth-Token': self.token,
                'Device-Id': self._device_id,
                'Content-Type': 'application/json'
            }, timeout=self._timeout())
            # _LOGGER.debug(response.text)
            response.raise_for_status()
            body = response.json()
            data = body.get("data", {}) if isinstance(body, dict) else None
            if not isinstance(data, dict):
                raise ValueError(f"Data is not a dict: {type(data).__name__}")
            return data
        except Exception as e:
            _LOGGER.error(f"Failed to get devices {e}, response was: {response}")
            return {}

    def token_rejected(self, token: str | None) -> bool:
        """The cloud refused this access token: renew it on the next use.

        Not when another request has renewed it already (each renewal rotates
        the pair, the newer one must not be thrown away), nor right after it was
        issued: the IoT backend may not know a new token for a few seconds.
        Returns True when the caller should wait a little and retry as is.
        """
        with self._lock:
            if not token or token != self.token:
                return False
            if time.monotonic() - self._token_issued < C.TOKEN_FRESH:
                return True
            self.tokenexpire = None
            return False

    def pull_device_data(self, device_mac: str) -> dict:
        for attempt in (1, 2):
            self.auth()
            token = self.token
            response = None
            try:
                status_url = C.API_STATUS.format(mac=device_mac)
                _LOGGER.debug(f"Getting initial status of device {device_mac}, url: {status_url}")
                response = self._session.get(status_url, headers={
                    'X-Auth-Token': token,
                    'Device-Id': self._device_id,
                    'Content-Type': 'application/json'
                }, timeout=self._timeout())
                response.raise_for_status()
                return response.json()
            except HTTPError as e:
                _LOGGER.error(f"Failed to get status: {e}, response was: {response}")
                if attempt == 2 or self.is_denied(e):
                    if response is not None and response.status_code == 401:
                        self.token_rejected(token)  # renewed for the next request
                    raise  # a refused device stays refused, no second try
                if response is not None and response.status_code == 401 and self.token_rejected(token):
                    time.sleep(C.TOKEN_RETRY_DELAY)  # a token just issued: give the cloud a moment
            except Exception as e:
                _LOGGER.error(f"Failed to get status: {e!r}, response was: {response}")
                raise

    def pull_data(self) -> None:
        self._pull_data = data = self.pull_data_from_api()
        if not self._pull_data:
            raise InvalidDevicesList()
        for listed in self._parse_device_list(data):
            try:
                device = HaierDevice.create(haier=self, **listed)
            except HTTPError as e:
                if not self.is_denied(e):
                    raise
                # listed by the app, but the cloud will not describe it (a
                # stale or no longer shared entry): start with the others
                _LOGGER.warning(
                    f"Haier Evo: skipping {listed['device_title'] or listed['device_mac']}, "
                    f"the cloud answers HTTP {e.response.status_code} for it"
                )
                continue
            self.devices.append(device)
            _LOGGER.info(f"Added device: {device.device_name} ({device.device_type} {device.device_model}, {device.device_id})")
            _LOGGER.debug(f"Added device: {device!r}")
        if len(self.devices) > 0:
            self.connect_in_thread()

    @staticmethod
    def is_denied(error: Exception) -> bool:
        """HTTP 403/404 for a device status: the cloud refuses that device."""
        response = getattr(error, "response", None)
        return isinstance(error, HTTPError) and response is not None and response.status_code in (403, 404)

    def denied_devices(self, macs) -> set[str]:
        """Of these MACs, the ones the cloud refuses to describe (blocking).

        Such a device is skipped at the start; it is not a reason to rebuild
        until the cloud describes it again.
        """
        denied = set()
        for mac in macs:
            try:
                self.pull_device_data(mac)
            except (ReauthRequired, AuthUserError):
                raise
            except Exception as e:
                if self.is_denied(e):
                    denied.add(mac)
        return denied

    def load_cached(self, cached: list[dict]) -> None:
        """Build the devices from the descriptions saved by the last good start.

        No network: the values in them are old, so every device stays
        unavailable until refresh_devices() reads its real state.
        """
        devices = []
        for item in cached:
            mac = str(item.get("mac") or "")
            if not MAC_RE.match(mac) or not isinstance(item.get("config"), dict):
                raise ValueError(f"Bad cached device: {mac!r}")
            device = HaierDevice.create(
                haier=self,
                device_type=str(item.get("type") or "UNKNOWN"),
                device_mac=mac,
                device_serial=item.get("serial"),
                device_title=item.get("title"),
                backend_data=item["config"],
            )
            device.synced = False
            devices.append(device)
        if not devices:
            raise ValueError("No cached devices")
        self.devices.extend(devices)
        _LOGGER.info(f"Started {len(devices)} device(s) from the saved descriptions")

    def refresh_devices(self) -> dict:
        """Read the real state of every device (blocking).

        Returns {"failed": [macs], "changed": bool}: changed means the cloud now
        describes a device differently (attributes, firmware) and the entities
        have to be rebuilt.
        """
        failed, changed = [], False
        for device in list(self.devices):
            if self.disconnect_requested:
                break
            started = time.monotonic()
            try:
                data = self.pull_device_data(device.device_id)
            except (ReauthRequired, AuthUserError):
                raise
            except Exception as e:
                _LOGGER.debug(f"{device.device_name}: refresh failed: {e!r}")
                failed.append(device.device_id)
                continue
            changed = device.apply_fresh(data, read_at=started) or changed
        return {"failed": failed, "changed": changed}

    def list_device_ids(self) -> set[str] | None:
        """MACs the app lists now, None when the cloud gave nothing usable."""
        try:
            data = self.pull_data_from_api()
        except (ReauthRequired, AuthUserError):
            raise
        except Exception as e:
            _LOGGER.debug(f"Device list: {e!r}")
            return None
        ids = {d["device_mac"].lower() for d in self._parse_device_list(data)} if data else set()
        return ids or None  # an empty list is a known cloud glitch, not "no devices"

    def cache_dict(self) -> dict:
        return {
            "version": C.CACHE_VERSION,
            "devices": [device.cache_dict() for device in self.devices],
        }

    @staticmethod
    def _parse_device_list(data: dict) -> list[dict]:
        """Devices of the app's home screen: type, MAC, serial and title of each.

        The list is found by its component name ("deviceList"), not by the
        layout UUID the app happens to use today; every such block counts, a
        device listed twice is taken once, a block that cannot be read is
        skipped. Appliances registered without Wi-Fi come in a block of their
        own ("non-iot" links, UUID ids) and are left out quietly. The cloud's
        answer is not changed.
        """
        listed, seen = [], set()
        presentation = data.get("presentation") if isinstance(data, dict) else None
        layout = presentation.get("layout") if isinstance(presentation, dict) else None
        containers = layout.get("scrollContainer") if isinstance(layout, dict) else None
        for item in containers if isinstance(containers, list) else []:
            if not isinstance(item, dict):
                continue
            tracking = item.get("trackingData")
            component = tracking.get("component") if isinstance(tracking, dict) else None
            if not isinstance(component, dict) or component.get("componentName") != "deviceList":
                continue
            state = item.get("state") or {}
            if isinstance(state, str):
                try:
                    state = json.loads(state)
                except ValueError:
                    _LOGGER.warning("Haier Evo: the device list block cannot be read, skipping it")
                    continue
            devices = state.get("items") if isinstance(state, dict) else None
            for d in devices if isinstance(devices, list) else []:
                if not isinstance(d, dict):
                    continue
                action = d.get('action')
                device_link = urlparse(str(action.get('link', '')) if isinstance(action, dict) else '')
                if device_link.netloc == "non-iot" or device_link.path.startswith("/non-iot"):
                    # an appliance registered in the app without Wi-Fi (dishwasher,
                    # hood...): its own "deviceList" block, UUID ids, nothing to control
                    continue
                query_params = parse_qs(device_link.query)
                device_type = query_params.get('type', ['UNKNOWN'])[0]
                device_mac = query_params.get('deviceId', [''])[0].replace('%3A', ':')
                device_serial = query_params.get('serialNum', [''])[0]
                if not MAC_RE.match(device_mac):
                    _LOGGER.debug(f"Skipping a device list item with an unexpected id: {device_mac!r}")
                    continue
                if device_mac.lower() in seen:
                    continue
                seen.add(device_mac.lower())
                listed.append({
                    "device_type": device_type,
                    "device_mac": device_mac,
                    "device_serial": device_serial,
                    "device_title": d['title'] if isinstance(d.get('title'), str) else '',
                })
        return listed

    def get_device_by_id(self, id_: str) -> HaierDevice | None:
        # the device list and the websocket may spell a MAC in different case
        id_ = str(id_).lower()
        return next((d for d in self.devices if str(d.device_id).lower() == id_), None)

    def _open_socket(self) -> ssl.SSLSocket:
        """TCP + TLS with a timeout (websocket-client only knows a process-wide one).

        The timeout also covers the WebSocket upgrade, _on_open removes it.
        """
        host = urlparse(C.API_WS_PATH).hostname
        raw = socket.create_connection((host, 443), timeout=C.WS_CONNECT_TIMEOUT)
        try:
            return get_default_context().wrap_socket(raw, server_hostname=host)
        except Exception:
            raw.close()
            raise

    def _init_ws(self) -> None:
        self.auth()
        self._ws_token = token = self.token
        url = urljoin(C.API_WS_PATH, token)
        # a prepared socket is used once, so every connection gets a new app
        self.socket_app = WebSocketApp(
            url=url,
            on_message=self._on_message,
            on_open=self._on_open,
            on_close=self._on_close,
            on_error=self._on_error,
            socket=self._open_socket(),
        )

    # noinspection PyUnusedLocal
    def _on_message(self, ws: WebSocket, message: str) -> None:
        _LOGGER.debug(f"Received WSS message: {message}")
        self._session_messages += 1
        self._silent_sessions = 0
        try:
            message_dict = json.loads(message)
        except (TypeError, ValueError):
            _LOGGER.warning(f"Haier Evo: an unreadable websocket message ({len(message or '')} bytes) was ignored")
            return
        if not isinstance(message_dict, dict):
            _LOGGER.debug("Haier Evo: a websocket message that is not an object was ignored")
            return
        if message_dict.get("event") == "command_response":
            self._handle_ack(message_dict)
        if not message_dict.get("macAddress"):
            return  # e.g. deviceStatusEvent without a device: nothing to apply
        message_device = str(message_dict.get("macAddress")).lower()
        device = self.get_device_by_id(message_device)
        if device is None:
            _LOGGER.debug(f"Got a message for a device we don't know about: {message_device}")
        else:
            device.on_message(message_dict)

    # noinspection PyMethodMayBeStatic,PyUnusedLocal
    def _on_open(self, ws: WebSocketApp) -> None:
        if self.disconnect_requested:
            self._end_session(ws)  # stop() came while this connection was being opened
            return
        if ws.sock is not None:
            ws.sock.settimeout(None)  # connect timeout must not limit reads
        self.socket_status = SocketStatus.INITIALIZED
        self._connected.set()
        _LOGGER.debug("Websocket opened")
        self._arm_silent_check(ws)
        self._came_online()
        down_since, self._down_since = self._down_since, None
        self._ever_connected = True
        if down_since is not None and time.monotonic() - down_since >= C.WS_RESYNC_AFTER:
            _LOGGER.info(f"Haier Evo: the connection was down for {int(time.monotonic() - down_since)} s, "
                         f"reading the state of the devices again")
            threading.Thread(target=self._resync_all, name="haier_evo resync", daemon=True).start()
        for device in self.devices:
            try:
                device.init_if_needed()
            except Exception as e:
                _LOGGER.warning(f"{device.device_name}: init after connect failed: {e!r}")

    def _arm_silent_check(self, app: WebSocketApp) -> None:
        self._session_messages = 0
        self._cancel_silent_check()
        # 60 s, then twice as long for every silent session in a row (up to 8 min):
        # a cloud that never pushes anything costs a reconnect now and then, no more
        delay = C.WS_SILENT_SESSION_TIMEOUT * 2 ** min(self._silent_sessions, 3)
        self._silent_timer = timer = threading.Timer(delay, self._check_silent_session, args=(app,))
        timer.daemon = True
        timer.start()

    def _cancel_silent_check(self) -> None:
        timer, self._silent_timer = self._silent_timer, None
        if timer is not None:
            timer.cancel()

    def _check_silent_session(self, app: WebSocketApp) -> None:
        """Drop a session the cloud has sent nothing over since it opened.

        Within seconds of an opening the cloud pushes the state of the devices;
        a session nothing at all has come over is accepted but not served (seen
        in the fork JampireX/haier_evo): commands into it get no answer, pushes
        never come. The connect loop opens a new one; the second silent session
        in a row goes with a renewed token.
        """
        if self.disconnect_requested or app is not self.socket_app or not self._connected.is_set():
            return
        if self._session_messages:
            return
        self._silent_sessions += 1
        log = _LOGGER.warning if self._silent_sessions == 1 else _LOGGER.debug
        log(f"Haier Evo: nothing came over the websocket since it opened, opening a new one "
            f"(silent sessions in a row: {self._silent_sessions})")
        if self._silent_sessions >= C.WS_SILENT_SESSIONS_FRESH_TOKEN:
            self.token_rejected(self._ws_token)
        self._end_session(app)

    # noinspection PyUnusedLocal
    def _on_error(self, ws: WebSocketApp, error: Exception) -> None:
        """websocket-client reports here what ends run_forever, the handshake included."""
        status = getattr(error, "status_code", None)
        if status in (401, 403):
            # the cloud dropped the token before it expired: refresh it for the
            # next try (a token issued seconds ago is tried once more as is)
            _LOGGER.info(f"Haier Evo: the websocket refused the token (HTTP {status})")
            self.token_rejected(self._ws_token)
        else:
            _LOGGER.debug(f"Websocket error: {error!r}")

    def _resync_all(self) -> None:
        for device in list(self.devices):
            if self.disconnect_requested:
                return
            device.resync(guard=True)

    # noinspection PyMethodMayBeStatic,PyUnusedLocal
    def _on_close(self, ws: WebSocketApp, close_code: int, close_message: str) -> None:
        if ws is self.socket_app:
            self._connected.clear()
            self.socket_status = SocketStatus.NOT_INITIALIZED
            self._cancel_silent_check()
        _LOGGER.debug(f"Websocket closed. Code: {close_code}, message: {close_message}")
        # the gateway may take the upgrade and then close with 1007 "Auth token
        # not valid!": the token is refused, renew it for the next connection
        # (up to 1.6.3 the same token was tried again until it expired locally,
        # up to an hour with the devices unavailable)
        if close_code == C.WS_CLOSE_TOKEN_REFUSED or "token" in str(close_message or "").lower():
            _LOGGER.info(f"Haier Evo: the websocket was closed for the token ({close_code} {close_message!r})")
            self.token_rejected(self._ws_token)

    def write_ha_state(self) -> None:
        for device in self.devices:
            device.write_ha_state()

    def _wait_connected(self, timeout: float) -> WebSocketApp | None:
        """Return the connected app, (re)starting the connection thread if needed."""
        if self.disconnect_requested:
            return None
        if not (self.socket_thread and self.socket_thread.is_alive()):
            self.connect_in_thread()
        if self._connected.wait(timeout):
            return self.socket_app
        return None

    def _drop_socket(self, app: WebSocketApp | None) -> None:
        """Close a connection that looks dead, the connection thread reconnects."""
        if app is not None and app is self.socket_app:
            self._connected.clear()
            self._end_session(app)

    @staticmethod
    def _end_session(app: WebSocketApp | None) -> None:
        """End a websocket session at once, without waiting for the cloud.

        WebSocketApp.close() sends the close frame and then reads the socket for
        the cloud's answer, up to 3 s in websocket-client 1.9, alongside the
        websocket thread that reads the same socket; called from the event loop
        (unload, up to 1.6.4) it froze Home Assistant for that long, from a
        command it held the retry back. As the fork JampireX/haier_evo does:
        the run loop is told to stop, the close frame goes out without waiting
        for the answer (on an open session only), and the TCP socket is shut
        down, so the read the websocket thread waits in returns at once and the
        thread tears the session down itself. Nothing here blocks or raises.
        """
        if app is None:
            return
        app.keep_running = False
        ws = getattr(app, "sock", None)
        if ws is not None and getattr(ws, "connected", False):
            try:
                ws.send_close()
            except Exception as e:  # the connection may be gone already
                _LOGGER.debug(f"Failed to send the websocket close frame: {e!r}")
        # the socket of the session; during the handshake, the one _open_socket gave
        sock = getattr(ws, "sock", None) or getattr(app, "prepared_socket", None)
        if sock is None:
            return
        try:
            # socket.socket.shutdown on purpose: SSLSocket.shutdown() would also
            # take the TLS layer away from the websocket thread reading through it
            socket.socket.shutdown(sock, socket.SHUT_RDWR)
        except (OSError, TypeError) as e:  # closed already, or not a socket
            _LOGGER.debug(f"Failed to shut the websocket down: {e!r}")

    @staticmethod
    def _close_prepared_socket(app: WebSocketApp | None) -> None:
        """Close the socket _open_socket gave the app, once its run is over.

        websocket-client closes it itself, except when the session ends during
        the handshake (stop() up to 1.6.1 left that connection open; now
        _end_session only shuts the socket down, and closing it is left here).
        Closing an already closed socket is harmless.
        """
        sock = getattr(app, "prepared_socket", None)
        if sock is None:
            return
        try:
            sock.close()
        except OSError as e:
            _LOGGER.debug(f"Failed to close the websocket's socket: {e!r}")

    def connect(self) -> None:
        self.socket_status = SocketStatus.NOT_INITIALIZED
        while not self.disconnect_requested:
            self.run_forever()
        _LOGGER.debug("Connection stoped")

    def connect_in_thread(self) -> None:
        self.socket_thread = thread = threading.Thread(target=self.connect)
        thread.daemon = True
        thread.start()

    @_limited("connect_limits")
    def run_forever(self) -> None:
        _LOGGER.debug(f"Connecting to websocket ({C.API_WS_PATH})")
        try:
            self._connected.clear()
            self.socket_status = SocketStatus.INITIALIZING
            self._init_ws()
            if self.disconnect_requested:
                return  # stop() came while connecting: finally closes the socket
            self.socket_app.run_forever(
                ping_interval=C.WS_PING_INTERVAL,
                ping_timeout=C.WS_PING_TIMEOUT,
            )
        except (ReauthRequired, AuthUserError) as e:
            # retrying cannot help: stop and let Home Assistant ask for the password
            _LOGGER.error(f"Haier Evo rejected the saved sign-in, re-authentication is needed: {e!r}")
            self.disconnect_requested = True
            if self._on_auth_failed is not None:
                self._on_auth_failed()
        except Exception as e:
            _LOGGER.warning(f"Error connecting to websocket: {e!r}")
        finally:
            self._close_prepared_socket(self.socket_app)
            self._connected.clear()
            self.socket_status = SocketStatus.NOT_INITIALIZED
            if self._ever_connected and self._down_since is None:
                self._down_since = time.monotonic()
            self._went_offline()

    def send_message(self, payload: str) -> None:
        """Send a raw message as is, without waiting (debug HTTP endpoint)."""
        _LOGGER.debug(f"Sending message: {payload}")
        app = self.socket_app if self._connected.is_set() else None
        if app is None:
            raise HaierNotConnected("Websocket is not connected")
        app.send(payload)

    def _handle_ack(self, message: dict) -> None:
        trace = message.get("trace")
        err = message.get("errNo")
        mac = str(message.get("macAddress") or "").lower()
        with self._pending_lock:
            pending = self._pending.get(trace) if trace else None
            if pending is None and not trace:
                # no trace in the answer: count it for the oldest command to that device
                pending = next((p for p in self._pending.values() if p["mac"] == mac), None)
            if pending is None:
                _LOGGER.debug(f"command_response for a foreign or late command: {message}")
                return
            pending["err"] = err
            pending["event"].set()

    def send_command(self, message: dict) -> None:
        """Send a device command and wait for its command_response.

        Commands carry absolute values, so re-sending one is harmless. Without
        an answer the socket is treated as dead (it may accept writes but not
        deliver them), dropped, and the command goes once more over a new one.
        """
        mac = str(message.get("macAddress") or "").lower()
        device = self.get_device_by_id(mac)
        name = str(device.device_name if device else mac)
        if threading.current_thread() is self.socket_thread:
            # called from a websocket callback: an ack could not arrive while we wait
            self.send_message(json.dumps({**message, "trace": str(uuid.uuid4())}))
            return
        if not self._command_slots.acquire(timeout=C.WS_SLOT_WAIT):
            # too many commands already wait for the cloud: fail fast instead
            # of tying up more of Home Assistant's executor threads
            raise HaierNotConnected(
                translation_domain=C.DOMAIN,
                translation_key="too_many_commands",
                translation_placeholders={"device": name},
            )
        try:
            self._send_command_attempts(message, mac, device, name)
        finally:
            self._command_slots.release()

    def _send_command_attempts(self, message: dict, mac: str, device, name: str) -> None:
        deadline = time.monotonic() + C.WS_COMMAND_BUDGET
        sent = False
        for attempt in (1, 2):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            app = self._wait_connected(min(C.WS_SEND_WAIT, remaining))
            if app is None:
                break
            trace = str(uuid.uuid4())
            pending = {"event": threading.Event(), "mac": mac, "err": None}
            with self._pending_lock:
                self._pending[trace] = pending
            try:
                payload = json.dumps({**message, "trace": trace})
                _LOGGER.debug(f"Sending message: {payload}")
                app.send(payload)
                sent = True
                ack_wait = max(0.5, min(C.WS_ACK_TIMEOUT, deadline - time.monotonic()))
                acked = pending["event"].wait(ack_wait)
            except Exception as e:
                _LOGGER.warning(f"{name}: failed to send a command: {e!r}")
                acked = False
            finally:
                with self._pending_lock:
                    self._pending.pop(trace, None)
            if acked:
                err = pending["err"]
                if str(err) not in ("0", "None"):
                    # a non-zero errNo also comes for values the device already has
                    # (-1 for a setpoint or mode, 22 for power): check the real state
                    data = device.resync() if device is not None else None
                    if data is not None and already_applied(message, data):
                        _LOGGER.debug(f"{name}: errNo={err}, the device already has the requested values")
                        return
                    _LOGGER.warning(f"{name}: the cloud rejected a command, errNo={err}")
                    raise HaierCommandRejected(
                        translation_domain=C.DOMAIN,
                        translation_key="command_rejected",
                        translation_placeholders={"device": name, "code": str(err)},
                    )
                if attempt > 1:
                    _LOGGER.info(f"{name}: command confirmed after re-sending")
                return
            _LOGGER.warning(
                f"{name}: no confirmation (attempt {attempt}), reconnecting the websocket"
            )
            self._drop_socket(app)
        if not sent:
            raise HaierNotConnected(
                translation_domain=C.DOMAIN,
                translation_key="not_connected",
                translation_placeholders={"device": name},
            )
        # sent but never confirmed: show the real state instead of the expected
        # one, in the background (the cloud may be slow right now)
        _LOGGER.error(f"{name}: command was not confirmed by the cloud, reading the state back")
        if device is not None:
            threading.Thread(target=device.resync, daemon=True).start()


class HaierDevice(object):

    def __init__(
        self,
        haier: Haier,
        device_mac: str,
        device_serial: str = None,
        device_title: str = None,
        backend_data: dict = None,
    ) -> None:
        self._haier = weakref.proxy(haier)
        self.device_id = device_mac
        self.device_serial = device_serial
        self.device_name = device_title
        self.device_model = "UNKNOWN"
        self.sw_version: str | None = None
        self._write_ha_state_callbacks = []
        self._available = True
        self._config = None
        self._status_data = backend_data
        self.alarms: list[str] = []
        self._values: dict[str, str] = {}  # raw attribute values by code (for modifiers)
        self._modifiers = CFG.Modifiers([])
        self.device_type = "UNKNOWN"
        self.link_status: str | None = None  # deviceConnectionStatusEvent
        # (platform, unique_id) of entities older versions created and this
        # device no longer offers: removed from the entity registry at setup
        self.hidden_entities: list[tuple[str, str]] = []
        # False while the values come from the saved description, not the cloud
        self.synced = True
        self._fresh_description: dict | None = None  # a newer description that needs a reload
        # time.monotonic() of the last event or command: a REST snapshot taken
        # before it is older than what the entities show
        self.changed_at = 0.0

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}("
            f"{self.device_id!r},"
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
            manufacturer="Haier"
        )

    @property
    def device_mac(self) -> str:
        return self.device_id

    @property
    def available(self) -> bool:
        # the cloud's ONLINE/OFFLINE flaps (see the connectivity sensor), so
        # it does not count here: the real state has been read since the
        # start, and the websocket has not been down for minutes
        return self.synced and getattr(self._haier, "cloud_available", True)

    @available.setter
    def available(self, value: bool | str):
        if not isinstance(value, bool):
            self._available = False if str(value).upper() == 'OFFLINE' else True
        else:
            self._available = value

    @property
    def status_data(self) -> dict:
        return self._status_data

    @property
    def connected(self) -> bool:
        """The cloud reports the device online (entities stay available anyway)."""
        return self._available

    @property
    def problem(self) -> bool:
        return any(a not in MAINTENANCE_ALARMS for a in self.alarms)

    @property
    def filter_cleaning(self) -> bool:
        return FILTER_ALARM in self.alarms

    @property
    def _firmware(self) -> dict:
        firmware = (device_part(self.status_data).get("settings") or {}).get("firmware")
        return firmware if isinstance(firmware, dict) else {}

    @property
    def firmware_update(self) -> bool | None:
        """The cloud says a newer firmware is waiting (the app installs it)."""
        value = self._firmware.get("updateRequired")
        return value if isinstance(value, bool) else None

    @property
    def firmware_auto_update(self) -> bool | None:
        value = (self._firmware.get("autoUpdate") or {}).get("enabled")
        return value if isinstance(value, bool) else None

    @property
    def alarm_texts(self) -> list[str]:
        """Human-readable alarms from the cloud's own table (errors.alarmToResult)."""
        errors = device_part(self.status_data).get("errors") or {}
        table = errors.get("alarmToResult") or {}
        unknown = (errors.get("undefinedAlarm") or {}).get("text") or "Unknown error"
        return [
            (table.get(alarm) or {}).get("text") or f"{unknown} ({alarm})"
            for alarm in self.alarms
        ]

    def _store_value(self, code, value) -> None:
        if value is not None:
            self._values[str(code)] = CFG.Constraint.value2code(str(value))

    def _set_alarms(self, alarms) -> None:
        names = []
        for alarm in alarms if isinstance(alarms, list) else []:
            if isinstance(alarm, dict):
                alarm = alarm.get("name") or alarm.get("alarmName") or alarm.get("code")
            # "alarmCancel" is Haier's "alarms cleared" marker, not an alarm
            if alarm and str(alarm) != "alarmCancel" and str(alarm) not in names:
                names.append(str(alarm))
        self.alarms = names

    def _cloud_attribute(self, code) -> dict:
        return next((
            a for a in device_part(self.status_data).get("attributes") or []
            if isinstance(a, dict) and str(a.get("name")) == str(code)
        ), {})

    def app_title(self, code, fallback: str | None = None) -> str | None:
        """What the Haier app calls an attribute, else the cloud's flat-list name."""
        if code is None:
            return fallback
        return (CFG.app_screens(self.status_data).get(str(code))
                or (self._cloud_attribute(code).get("description") or "").strip()
                or fallback)

    def offered_by_app(self, code) -> bool:
        """False when the app has screens for this device but none for the attribute."""
        screens = CFG.app_screens(self.status_data)
        return not screens or str(code) in screens

    def _cloud_value_name(self, code, value) -> str:
        """The cloud's own (Russian) name of a list value, e.g. 2/"6" -> "Вентилятор"."""
        for item in (self._cloud_attribute(code).get("list") or {}).get("data") or []:
            if CFG.Constraint.value2code(str(item.get("data"))) == CFG.Constraint.value2code(str(value)):
                return str(item.get("name") or value)
        return str(value)

    def _locked(self, values: dict | None = None) -> dict[str, str]:
        alarms = set(self.alarms) if self.enforce_alarm_locks else set()
        return self._modifiers.locked(values or self._values, alarms)

    # alarms lock attributes in the cloud's rules; some devices list mere
    # maintenance reminders there, so this is decided per device type
    enforce_alarm_locks = True

    def _check_writable(self, name: str, value=None, assume: dict | None = None) -> None:
        """Refuse a command the cloud marks read-only right now (CFG.Modifiers).

        assume: raw values (code -> value) the same command changes first,
        e.g. power on together with a new mode.
        """
        attr = self.config.get_attr_by_name(name) if self.config else None
        if attr is None:
            return
        code = str(attr.code)
        values = {**self._values, **{str(k): CFG.Constraint.value2code(str(v)) for k, v in (assume or {}).items()}}
        placeholders = {
            "device": str(self.device_name),
            "attribute": self.app_title(code, attr.description or name),
        }
        reason = self._locked(values).get(code)
        if reason is None:
            allowed = self._modifiers.allowed(values, set(self.alarms) if self.enforce_alarm_locks else set()).get(code)
            if value is not None and allowed is not None:
                item = CFG.Constraint.value2code(str(attr.get_item_code(str(value), value)))
                if item not in allowed[0]:
                    raise HaierCommandLocked(
                        translation_domain=C.DOMAIN,
                        translation_key="value_not_allowed",
                        translation_placeholders={**placeholders, "allowed": ", ".join(allowed[1])},
                    )
            return
        if reason == CFG.Modifiers.ALARM:
            key, extra = "locked_alarm", {}
        elif reason == self.config["status"]:
            key, extra = "locked_off", {}
        elif reason == self.config["mode"]:
            key, extra = "locked_mode", {"mode": self._cloud_value_name(reason, values.get(reason))}
        else:
            cause = self.app_title(reason, str(reason))
            key, extra = "locked_by", {"cause": str(cause).strip()}
        raise HaierCommandLocked(
            translation_domain=C.DOMAIN,
            translation_key=key,
            translation_placeholders={**placeholders, **extra},
        )

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
            "device_id": self.device_id,
            "device_mac": self.device_mac,
            "device_name": self.device_name,
            "device_serial": self.device_serial,
            "sw_version": self.sw_version,
            "config": self.config.to_dict() if self.config else None,
            "backend_data": self.status_data,
        }

    def _get_status(self, data: dict) -> dict:
        self._status_data = whole = (data or {})
        data = device_part(whole)
        info = data.setdefault("info", {})
        self.device_serial = info.setdefault("serialNumber", self.device_serial)
        device_model = info.setdefault("model", "AC")
        device_model = device_model.replace('-','').replace('/', '')[:11]
        self.device_model = device_model
        self.available = data.setdefault("status", "ONLINE")
        settings = data.setdefault("settings", {})
        self.device_name = settings.setdefault("name", {}).setdefault("name", self.device_name)
        self.sw_version = firmware_text(settings.setdefault('firmware', {}).setdefault('value', None))
        for attr in data.get("attributes") or []:
            if isinstance(attr, dict):
                self._store_value(attr.get("name"), attr.get("currentValue"))
        self._modifiers = CFG.Modifiers(data.get("modifiers"))
        self._set_alarms(data.get("alarms"))
        # read config and current values
        self._load_config_from_attributes(data)
        return whole

    def _load_config_from_attributes(self, data: dict) -> None:
        pass

    def _set_attribute_value(self, code: str, value: str) -> None:
        pass

    def _handle_status_update(self, received_message: dict) -> None:
        payload = received_message.get("payload")
        statuses = payload.get("statuses") if isinstance(payload, dict) else None
        self.changed_at = time.monotonic()
        # every entry counts (up to 1.5.3 only the first one's values did)
        for status in statuses if isinstance(statuses, list) else []:
            if not isinstance(status, dict):
                continue
            if "alarms" in status:
                self._set_alarms(status["alarms"])
            properties = status.get("properties")
            for key, value in (properties.items() if isinstance(properties, dict) else ()):
                self._apply_value(key, value)
        self.available = True
        self.write_ha_state()

    def _apply_value(self, code, value) -> None:
        """Store and apply one raw value; one that cannot be read is skipped,
        the other values of the same message still apply."""
        if value is None:
            return
        try:
            self._store_value(code, value)
            self._set_attribute_value(str(code), value)
        except (TypeError, ValueError) as e:
            _LOGGER.debug(f"{self.device_name}: value {value!r} of attribute {code} skipped: {e!r}")

    def _handle_device_status_update(self, received_message: dict) -> None:
        payload = received_message.get("payload")
        if not isinstance(payload, dict) or payload.get("status") is None:
            return  # nothing to tell (up to 1.6.2 a null payload broke the handler)
        self.available = payload["status"]
        self.write_ha_state()

    def _handle_info(self, received_message: dict) -> None:
        payload = received_message.get("payload")
        if isinstance(payload, dict):
            self.sw_version = firmware_text(payload.get("swVersion")) or self.sw_version

    def _send_message(self, message: dict) -> None:
        self.changed_at = time.monotonic()
        try:
            self._haier.send_command(message)
        finally:
            # the command waits for its answer: a snapshot read meanwhile is older
            self.changed_at = time.monotonic()

    def update_from_backend(self, data: dict) -> None:
        """Apply a fresh REST status (values only, the config stays)."""
        if not isinstance(data, dict) or self.config is None:
            return
        data = device_part(data)
        self.available = data.get("status") or "ONLINE"
        self._set_alarms(data.get("alarms"))
        for attr in data.get("attributes") or []:
            if isinstance(attr, dict):
                self._apply_value(attr.get("name"), attr.get("currentValue"))
        self.synced = True
        self.write_ha_state()

    @staticmethod
    def description_signature(data: dict | None) -> tuple:
        """What the entities are built from: attributes and their values, louver
        screens, model and firmware. Values and alarms are not part of it."""
        data = device_part(data)
        attrs = []
        for attr in data.get("attributes") or []:
            if not isinstance(attr, dict):
                continue
            items = ((attr.get("list") or {}).get("data") or []) if isinstance(attr.get("list"), dict) else []
            attrs.append((
                str(attr.get("name")),
                str(attr.get("description")),
                bool(attr.get("writable")),
                tuple(sorted(str(i.get("data")) for i in items if isinstance(i, dict))),
            ))
        firmware = ((data.get("settings") or {}).get("firmware") or {})
        return (
            str((data.get("info") or {}).get("model")),
            str(firmware.get("value") if isinstance(firmware, dict) else None),
            tuple(sorted(attrs)),
            tuple(sorted((k, tuple(v)) for k, v in CFG.louver_layout(data.get("modes")).items())),
        )

    def apply_fresh(self, data: dict, read_at: float | None = None) -> bool:
        """Apply a description just read from the cloud.

        Returns True when the cloud describes the device differently than the
        one it was built from: the entities then have to be rebuilt (reload).
        read_at: time.monotonic() when the request went out (see is_stale).
        """
        if not isinstance(data, dict):
            return False
        if self.description_signature(data) != self.description_signature(self.status_data):
            _LOGGER.info(f"{self.device_name}: the cloud describes the device differently now, reloading")
            self._fresh_description = data
            if not self.is_stale(read_at):
                self.update_from_backend(data)
            return True
        # same structure: keep the fresh texts, firmware flags and rules
        self._status_data = data
        self._modifiers = CFG.Modifiers(data.get("modifiers"))
        if not self.is_stale(read_at):
            self.update_from_backend(data)
        return False

    def is_stale(self, read_at: float | None) -> bool:
        """A REST snapshot requested before the last event or command is older
        than what the entities show. A device that has not been read yet takes
        it anyway: an old snapshot still beats the saved description."""
        if read_at is None or not self.synced or self.changed_at <= read_at:
            return False
        _LOGGER.debug(f"{self.device_name}: newer events came while the state was read, snapshot skipped")
        return True

    def cache_dict(self) -> dict:
        return {
            "type": self.device_type,
            "mac": self.device_id,
            "serial": self.device_serial,
            "title": self.device_name,
            "config": cacheable_description(self._fresh_description or self.status_data),
        }

    def resync(self, guard: bool = False) -> dict | None:
        """Read the real state back from the cloud.

        guard: skip the snapshot if events came meanwhile (after a reconnect).
        Without it (a lost or rejected command) it always applies: it is there
        to undo the value shown in advance.
        """
        started = time.monotonic()
        try:
            data = self._haier.pull_device_data(self.device_id)
        except Exception as e:
            _LOGGER.warning(f"{self.device_name}: failed to read the state back: {e!r}")
            return None
        if not (guard and self.is_stale(started)):
            self.update_from_backend(data)
        return data

    def _send_commands(self, commands: list[dict]) -> None:
        self._send_group_command(commands)
        # keep the modifiers right until the device echoes the new values
        for command in commands:
            self._store_value(command.get("commandName"), command.get("value"))

    def _send_group_command(self, commands: list[dict]) -> None:
        trace = str(uuid.uuid4())
        _ = self._send_message({
            "action": "operation",
            "macAddress": self.device_id,
            "commandName": self.config.command_name,
            "commands": commands,
            "trace": trace,
        }) if self.config.command_name else [
            self._send_single_command(c)
            for c in commands
        ]

    def _send_single_command(self, command: dict) -> None:
        trace = str(uuid.uuid4())
        self._send_message({
            "action": "command",
            "macAddress": self.device_id,
            "command": command,
            "trace": trace,
        })

    def init_if_needed(self) -> None:
        pass

    def get_commands(self, name: str, value: str | bool) -> list[dict]:
        value = str({True: "on", False: "off", None: "off"}.get(value, value))
        if custom := self.config.get_command_by_name(f"{name}_{value}"):
            return custom
        attr = self.config.get_attr_by_name(name)
        if attr is None:
            return []
        code = attr.get_item_code(value)
        if code is None and name == "status" and value in ("on", "off"):
            code = "1" if value == "on" else "0"  # power is 1/0 on every known device
        if code is None:
            # nothing the device would understand: send nothing, not "None"
            _LOGGER.warning(f"{self.device_name}: no command for {name}={value}")
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
            # the module's link to the cloud ("GOOD" seen so far), sent on connect
            payload = message_dict.get("payload")
            if isinstance(payload, dict):
                self.link_status = payload.get("status")
                self.write_ha_state()
        else:
            _LOGGER.warning(f"{self.device_name}: got an unknown message {str(message_type)[:40]!r}")
            _LOGGER.debug(f"Unknown message: {message_dict}")

    def write_ha_state(self) -> None:
        # a copy: an entity may unsubscribe from the event loop meanwhile
        for callback in list(self._write_ha_state_callbacks):
            self.hass.loop.call_soon_threadsafe(callback)

    def add_write_ha_state_callback(self, callback) -> Callable[[], None]:
        """Call callback in the event loop on every change; returns the unsubscribe."""
        if callback not in self._write_ha_state_callbacks:
            self._write_ha_state_callbacks.append(callback)

        def remove() -> None:
            if callback in self._write_ha_state_callbacks:
                self._write_ha_state_callbacks.remove(callback)
        return remove

    # noinspection PyMethodMayBeStatic
    def create_entities_climate(self) -> list:
        return []

    # noinspection PyMethodMayBeStatic
    def create_entities_water_heater(self) -> list:
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
    def create_entities_button(self) -> list:
        return []

    @classmethod
    def create(
        cls,
        haier: Haier,
        device_type: str,
        device_mac: str,
        device_serial: str = None,
        device_title: str = None,
        backend_data: dict | None = None,
    ) -> HaierDevice:
        """backend_data: a saved description; without it the cloud is asked."""
        if backend_data is None:
            backend_data = haier.pull_device_data(device_mac)
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
            _LOGGER.warning(f"Unknown device type: {device_type}")
        device = device_cls(
            haier=haier,
            device_mac=device_mac,
            device_serial=device_serial,
            device_title=device_title,
            backend_data=backend_data,
        )
        device.device_type = device_type
        return device


class HaierAC(HaierDevice):
    # the cloud locks almost everything on any alarm, filterCleaningAlarm
    # included: a filter reminder must not take the AC away from HA
    enforce_alarm_locks = False

    def __init__(
        self,
        backend_data: dict = None,
        **kwargs
    ) -> None:
        super().__init__(**kwargs)
        self.current_temperature = 0
        self.target_temperature = 0
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
        self._deferred_target = None  # setpoint chosen while the unit was off
        self._get_status(backend_data)
        self._inited = False

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
            "alarms": self.alarms,
            "connected": self.connected,
            "deferred_target": self._deferred_target,
            "locked": self._locked(),
        })
        return data

    def _set_attribute_value(self, code: str, value: str) -> None:
        # one code may stand behind several attributes ("Тихий" is both the
        # sleep preset and the quiet switch): all of them follow the unit,
        # not only the first one (up to 1.5.2 the switches never did)
        if value is None:
            return
        for attr in self.config.get_attrs_by_code(code):
            self._apply_attribute(attr, value)

    def _apply_attribute(self, attr: CFG.Attribute, value: str) -> None:
        if attr.name == "current_temperature":
            self.current_temperature = float(value)
        elif attr.name == "status":
            power = CFG.Constraint.value2code(str(value).strip().lower())
            self.status = 1 if power == "1" else 0 if power == "0" else int(power)
            if self.status and self._deferred_target is not None:
                # on now, but not by us (our "power on" takes the kept setpoint
                # along and drops it): the kept one is stale, show the unit's own
                self._deferred_target = None
                self._show_unit_setpoint()
        elif attr.name == "target_temperature":
            # a setpoint kept for the next "on" stays on the card until then:
            # the cloud keeps reporting the old one of a unit that is off
            if self._deferred_target is None:
                self.target_temperature = float(value)
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
        attributes = data.setdefault("attributes", [])
        sensors = data.setdefault("sensors", {}).get("items", [])
        sensor_curr_temp = next(filter(lambda i: (
            isinstance(i, dict)
            and isinstance(i.get("value"), dict)
            and i.get("value", {}).get("description") == "indoorTemperature"
        ), sensors), {}).get("value", {}).get("name")
        attrs = list(sorted(map(lambda x: CFG.Attribute(x), attributes), key=lambda x: x.code))
        for attr in attrs:
            if attr.name == "current_temperature" and str(attr.code) != sensor_curr_temp:
                # the other "Температура в комнате" (code 38 on S2SF): 1.6.0
                # showed it as the outdoor unit, but on AS25S2SF2FA it never
                # changes (the same values for days), so it is not used
                continue
            self.config.attrs.append(attr)
        self.config.merge_attributes()
        self.config.apply_louver_layout(data.get("modes"))
        for attr in self.config.attrs:
            self._apply_value(attr.code, attr.current)
            if attr.name == "target_temperature" and attr.range:
                self.min_temperature = parsefloat(attr.range.min_value, self.min_temperature)
                self.max_temperature = parsefloat(attr.range.max_value, self.max_temperature)
            _LOGGER.debug(f"{self.device_name}: {attr}")
        self.constraint.extend(data.setdefault("constraint", []))

    def _get_status(self, data: dict) -> dict:
        data = super()._get_status(data)
        if self.swing_horizontal_mode is None:
            self.swing_horizontal_mode = SWING_OFF
        if self.swing_mode is None:
            self.swing_mode = SWING_OFF
        if self.preset_mode is None:
            self.preset_mode = PRESET_NONE
        self.write_ha_state()
        return data

    def init_if_needed(self) -> None:
        if not self._inited and next(filter(
            lambda a: (not a.name.startswith("preset_mode_") and a.current is None),
            self.config.attrs
        ), None) is not None:
            self.set_temperature(self.target_temperature)
        self._inited = True

    def get_commands(self, name: str, value: str | bool) -> list[dict]:
        if name != "preset_mode":
            self._check_writable(name, value)
            return super().get_commands(name, value)
        if value not in ("none", None):
            self._check_writable(f"preset_mode_{value}")
        func = getattr(self, f"get_preset_mode_{value}", None)
        if func is not None:
            return func()
        return self.get_preset_mode_command(value)

    def get_preset_mode_none(self) -> list[dict]:
        if custom := self.config.get_command_by_name('preset_mode_none'):
            return custom
        return [{
            "commandName": str(attr.code),
            "value": attr.get_item_code("off", "0"),
        } for attr in filter(
            lambda a: a.name.startswith("preset_mode"),
            self.config.attrs
        )]

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
        return self.config.get_values('swing_mode')

    def get_preset_modes(self) -> list[str]:
        return ["none"] + self.config.get_preset_modes()

    def get_eco_sensor_options(self) -> list[str]:
        return self.config.get_values('eco_sensor')

    def set_temperature(self, value: float) -> None:
        try:
            self._check_writable("target_temperature")
        except HaierCommandLocked as e:
            if e.translation_key != "locked_off":
                raise
            # the unit ignores a setpoint while off: keep it for the next "on"
            self._deferred_target = value
            self.target_temperature = value
            self.write_ha_state()
            return
        self._send_commands([
            {
                "commandName": self.config['target_temperature'],
                "value": number_text(value)
            }
        ])
        self.target_temperature = value

    def _show_unit_setpoint(self) -> None:
        """Show the setpoint the unit itself reported last."""
        raw = self._values.get(str(self.config["target_temperature"]))
        if (value := parsefloat(raw)) is not None:
            self.target_temperature = value

    def switch_on(self, value: str = None) -> None:
        value = value or self.mode or HVACMode.AUTO
        # the mode is locked while off, but goes out together with "power on"
        assume = {}
        if status_code := self.config["status"]:
            assume[status_code] = "1"
        if mode_attr := self.config.get_attr_by_name("mode"):
            assume[str(mode_attr.code)] = mode_attr.get_item_code(value, value)
        self._check_writable("mode", value, assume=assume)
        # "power on" goes out even when the unit is believed to run: that
        # belief may be stale (an event lost while the cloud was away), and a
        # mode alone does not start a unit that is off
        power_on = HaierDevice.get_commands(self, "status", "on")
        believed_on = bool(self.status)
        if believed_on:
            # only the power command itself: the cloud's power-on extras stop
            # the cleaning cycles, which a mode change must not do
            power_on = [c for c in power_on if str(c.get("commandName")) == str(status_code)]
        commands = HaierDevice.get_commands(self, "mode", value)
        if self._deferred_target is not None and self.config["target_temperature"]:
            try:
                self._check_writable("target_temperature", assume=assume)
            except HaierCommandLocked:
                # the new mode has no setpoint (fan only): drop it and show the
                # unit's own one again (its echo of "on" would drop it anyway)
                self._deferred_target = None
                self._show_unit_setpoint()
            else:
                commands.append({
                    "commandName": self.config["target_temperature"],
                    "value": number_text(self._deferred_target),
                })
                self.target_temperature = self._deferred_target
                self._deferred_target = None
        try:
            self._send_commands([*power_on, *commands])
        except HaierCommandRejected:
            if not (believed_on and power_on and commands):
                raise
            # the cloud may refuse "power on" for a unit that already runs
            # (errNo 22) and the rest of the group with it: once more without it
            _LOGGER.debug(f"{self.device_name}: power on refused for a running unit, sending the mode alone")
            self._send_commands(commands)
        self.status = 1
        self.mode = value

    def switch_off(self) -> None:
        self._send_commands([
            *self.get_commands("status", "off"),
        ])
        self.status = 0

    def set_fan_mode(self, value: str) -> None:
        if commands := self.get_commands("fan_mode", value):
            self._send_commands(commands)
            self.fan_mode = value

    def set_swing_horizontal_mode(self, value: str) -> None:
        if commands := self.get_commands("swing_horizontal_mode", value):
            self._send_commands(commands)
            self.swing_horizontal_mode = value

    def set_swing_mode(self, value: str) -> None:
        if commands := self.get_commands("swing_mode", value):
            self._send_commands(commands)
            self.swing_mode = value

    def set_preset_mode(self, value: str) -> None:
        if commands := self.get_commands("preset_mode", value):
            self._send_commands(commands)
            self.preset_mode = value
            if value in (PRESET_NONE, "sleep", "boost"):
                # these presets are read from the two switches, which the
                # unit's echo confirms; until then show what was asked
                self.quiet_on = value == "sleep"
                self.turbo_on = value == "boost"

    def set_light_on(self, value: bool) -> None:
        if commands := self.get_commands("light", value):
            self._send_commands(commands)
            self.light_on = value

    def set_sound_on(self, value: bool) -> None:
        if commands := self.get_commands("sound", value):
            self._send_commands(commands)
            self.sound_on = value

    def set_quiet_on(self, value: bool) -> None:
        if commands := self.get_commands("quiet", value):
            self._send_commands(commands)
            self.quiet_on = value

    def set_health_on(self, value: bool) -> None:
        if commands := self.get_commands("health", value):
            self._send_commands(commands)
            self.health_on = value

    def set_turbo_on(self, value: bool) -> None:
        if commands := self.get_commands("turbo", value):
            self._send_commands(commands)
            self.turbo_on = value

    def set_comfort_on(self, value: bool) -> None:
        if commands := self.get_commands("comfort", value):
            self._send_commands(commands)
            self.comfort_on = value

    def set_cleaning_on(self, value: bool) -> None:
        if commands := self.get_commands("cleaning", value):
            self._send_commands(commands)
            self.cleaning_on = value

    def set_antifreeze_on(self, value: bool) -> None:
        if commands := self.get_commands("antifreeze", value):
            self._send_commands(commands)
            self.antifreeze_on = value

    def set_autohumidity_on(self, value: bool) -> None:
        if commands := self.get_commands("autohumidity", value):
            self._send_commands(commands)
            self.autohumidity_on = value

    def set_eco_sensor(self, value: str) -> None:
        if commands := self.get_commands("eco_sensor", value):
            self._send_commands(commands)
            self.eco_sensor = value

    def create_entities_climate(self) -> list:
        from . import climate
        return [climate.HaierACEntity(self)]
    
    def create_entities_switch(self) -> list:
        """Switches for what the app offers; the rest (e.g. "Звуковой сигнал",
        which AS25S2SF2FA rejects) is listed in hidden_entities for removal."""
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


class HaierREF(HaierDevice):

    def __init__(
        self,
        backend_data: dict = None,
        **kwargs
    ) -> None:
        super().__init__(**kwargs)
        self.current_fridge_temperature = 0
        self.current_freezer_temperature = 0
        self.current_temperature = 0
        self.fridge_mode = None
        self.freezer_mode = None
        self.my_zone = None
        self.super_cooling = False
        self.super_freeze = False
        self.vacation_mode = False
        self.door_open = False
        self._get_status(backend_data)

    @property
    def config(self) -> CFG.HaierREFConfig:
        return self._config

    def to_dict(self) -> dict:
        data = super().to_dict()
        data.update({
            "current_fridge_temperature": self.current_fridge_temperature,
            "current_freezer_temperature": self.current_freezer_temperature,
            "current_temperature": self.current_temperature,
            "fridge_mode": self.fridge_mode,
            "freezer_mode": self.freezer_mode,
            "my_zone": self.my_zone,
            "super_cooling": self.super_cooling,
            "super_freeze": self.super_freeze,
            "vacation_mode": self.vacation_mode,
            "door_open": self.door_open,
        })
        return data

    def _load_config_from_attributes(self, data: dict) -> None:
        self._config = CFG.HaierREFConfig(self.device_model, self.hass.config.path(C.DOMAIN))
        attributes = data.setdefault("attributes", [])
        attrs = list(sorted(map(lambda x: CFG.Attribute(x), attributes), key=lambda x: x.code))
        for attr in attrs:
            self.config.attrs.append(attr)
        self.config.merge_attributes()
        for attr in self.config.attrs:
            self._apply_value(attr.code, attr.current)
            _LOGGER.debug(f"{self.device_name}: {attr}")

    def _set_attribute_value(self, code: str, value: str) -> None:
        attr = self.config.get_attr_by_code(code)
        if not (attr and value is not None):
            return
        elif attr.name == "current_fridge_temperature":
            self.current_fridge_temperature = float(value)
        elif attr.name == "current_freezer_temperature":
            self.current_freezer_temperature = float(value)
        elif attr.name == "current_temperature":
            self.current_temperature = float(value)
        elif attr.name == "fridge_mode":
            self.fridge_mode = attr.get_item_name(value)
        elif attr.name == "freezer_mode":
            self.freezer_mode = attr.get_item_name(value)
        elif attr.name == "my_zone":
            self.my_zone = attr.get_item_name(value)
        elif attr.name == "super_cooling":
            self.super_cooling = parsebool(attr.get_item_name(value))
        elif attr.name == "super_freeze":
            self.super_freeze = parsebool(attr.get_item_name(value))
        elif attr.name == "vacation_mode":
            self.vacation_mode = parsebool(attr.get_item_name(value))
        elif attr.name == "door_open":
            self.door_open = parsebool(attr.get_item_name(value))

    def get_fridge_mode_options(self) -> list[str]:
        return self.config.get_values('fridge_mode')

    def get_freezer_mode_options(self) -> list[str]:
        return self.config.get_values('freezer_mode')

    def get_my_zone_options(self) -> list[str]:
        return self.config.get_values('my_zone')

    def set_super_cooling(self, value: bool) -> None:
        if commands := self.get_commands("super_cooling", value):
            self._send_single_command(commands[0])
            self.super_cooling = value

    def set_super_freeze(self, value: bool) -> None:
        if commands := self.get_commands("super_freeze", value):
            self._send_single_command(commands[0])
            self.super_freeze = value

    def set_vacation_mode(self, value: bool) -> None:
        if commands := self.get_commands("vacation_mode", value):
            self._send_single_command(commands[0])
            self.vacation_mode = value

    def set_fridge_mode(self, value: str) -> None:
        if commands := self.get_commands("fridge_mode", value):
            self._send_single_command(commands[0])
            self.fridge_mode = value

    def set_freezer_mode(self, value: str) -> None:
        if commands := self.get_commands("freezer_mode", value):
            self._send_single_command(commands[0])
            self.freezer_mode = value

    def set_my_zone(self, value: str) -> None:
        if commands := self.get_commands("my_zone", value):
            self._send_single_command(commands[0])
            self.my_zone = value

    def create_entities_switch(self) -> list:
        from . import switch
        entities = []
        if self.config['super_cooling'] is not None:
            entities.append(switch.HaierREFSuperCoolingSwitch(self))
        if self.config['super_freeze'] is not None:
            entities.append(switch.HaierREFSuperFreezeSwitch(self))
        if self.config['vacation_mode'] is not None:
            entities.append(switch.HaierREFVacationSwitch(self))
        return entities

    def create_entities_select(self) -> list:
        from . import select
        entities = []
        if self.config['fridge_mode'] is not None:
            entities.append(select.HaierREFFridgeModeSelect(self))
        if self.config['freezer_mode'] is not None:
            entities.append(select.HaierREFFreezerModeSelect(self))
        if self.config['my_zone'] is not None:
            entities.append(select.HaierREFMyZoneSelect(self))
        return entities

    def create_entities_sensor(self) -> list:
        from . import sensor
        entities = []
        if self.config['current_temperature'] is not None:
            entities.append(sensor.HaierREFTemperatureSensor(self))
        if self.config['current_fridge_temperature'] is not None:
            entities.append(sensor.HaierREFFridgeTemperatureSensor(self))
        if self.config['current_freezer_temperature'] is not None:
            entities.append(sensor.HaierREFFreezerTemperatureSensor(self))
        if self.config['fridge_mode'] is not None:
            entities.append(sensor.HaierREFFridgeModeSensor(self))
        if self.config['freezer_mode'] is not None:
            entities.append(sensor.HaierREFFreezerModeSensor(self))
        return entities

    def create_entities_binary_sensor(self) -> list:
        from . import binary_sensor
        entities = []
        if self.config['super_cooling'] is not None:
            entities.append(binary_sensor.HaierREFSuperCoolingSensor(self))
        if self.config['super_freeze'] is not None:
            entities.append(binary_sensor.HaierREFSuperFreezeSensor(self))
        if self.config['vacation_mode'] is not None:
            entities.append(binary_sensor.HaierREFVacationSensor(self))
        if self.config['door_open'] is not None:
            entities.append(binary_sensor.HaierREFDoorSensor(self))
        return entities


class HaierWM(HaierDevice):

    def __init__(
        self,
        backend_data: dict = None,
        **kwargs
    ) -> None:
        super().__init__(**kwargs)
        self.status = None
        self.program = None
        self.temperature = None
        self.spin_speed = None
        self.remaining_time = None
        self._get_status(backend_data)

    @property
    def config(self) -> CFG.HaierWMConfig:
        return self._config

    def to_dict(self) -> dict:
        data = super().to_dict()
        data.update({
            "status": self.status,
            "program": self.program,
            "temperature": self.temperature,
            "spin_speed": self.spin_speed,
            "remaining_time": self.remaining_time,
        })
        return data

    def _load_config_from_attributes(self, data: dict) -> None:
        self._config = CFG.HaierWMConfig(self.device_model, self.hass.config.path(C.DOMAIN))
        attributes = data.setdefault("attributes", [])
        attrs = list(sorted(map(lambda x: CFG.Attribute(x), attributes), key=lambda x: x.code))
        for attr in attrs:
            self.config.attrs.append(attr)
        self.config.merge_attributes()
        for attr in self.config.attrs:
            self._apply_value(attr.code, attr.current)
            _LOGGER.debug(f"{self.device_name}: {attr}")

    def _set_attribute_value(self, code: str, value: str) -> None:
        attr = self.config.get_attr_by_code(code)
        if not (attr and value is not None):
            return
        elif attr.name == "status":
            self.status = attr.get_item_name(value)
        elif attr.name == "program":
            self.program = attr.get_item_name(value)
        elif attr.name == "temperature":
            self.temperature = attr.get_item_name(value)
        elif attr.name == "spin_speed":
            self.spin_speed = attr.get_item_name(value)
        elif attr.name == "remaining_time":
            self.remaining_time = float(value) if value else None

    def get_program_options(self) -> list[str]:
        return self.config.get_values('program')

    def get_temperature_options(self) -> list[str]:
        return self.config.get_values('temperature')

    def get_spin_speed_options(self) -> list[str]:
        return self.config.get_values('spin_speed')

    def set_program(self, value: str) -> None:
        if commands := self.get_commands("program", value):
            self._send_single_command(commands[0])
            self.program = value

    def set_temperature(self, value: str) -> None:
        if commands := self.get_commands("temperature", value):
            self._send_single_command(commands[0])
            self.temperature = value

    def set_spin_speed(self, value: str) -> None:
        if commands := self.get_commands("spin_speed", value):
            self._send_single_command(commands[0])
            self.spin_speed = value

    def create_entities_select(self) -> list:
        from . import select
        entities = []
        if self.config['program'] is not None:
            entities.append(select.HaierWMProgramSelect(self))
        if self.config['temperature'] is not None:
            entities.append(select.HaierWMTemperatureSelect(self))
        if self.config['spin_speed'] is not None:
            entities.append(select.HaierWMSpinSpeedSelect(self))
        return entities

    def create_entities_sensor(self) -> list:
        from . import sensor
        entities = []
        if self.config['remaining_time'] is not None:
            entities.append(sensor.HaierWMRemainingTimeSensor(self))
        if self.config['status'] is not None:
            entities.append(sensor.HaierWMStatusSensor(self))
        return entities


class HaierWH(HaierDevice):
    """Water heater (device type "WH"), e.g. ES80V-F7.

    Attributes reported by the cloud: current/target water temperature,
    power, heating mode (Eco = half of the tank, Max = whole tank),
    heating status (heating / keeping warm) and sterilization (BPS).
    """

    def __init__(
        self,
        backend_data: dict = None,
        **kwargs
    ) -> None:
        super().__init__(**kwargs)
        self.current_temperature = None
        self.target_temperature = None
        self.min_temperature = 35
        self.max_temperature = 75
        self.status = False
        self.heating_mode = None
        self.heating = False
        self.sterilization_on = False
        self._get_status(backend_data)

    @property
    def config(self) -> CFG.HaierWHConfig:
        return self._config

    def to_dict(self) -> dict:
        data = super().to_dict()
        data.update({
            "current_temperature": self.current_temperature,
            "target_temperature": self.target_temperature,
            "min_temperature": self.min_temperature,
            "max_temperature": self.max_temperature,
            "status": self.status,
            "heating_mode": self.heating_mode,
            "heating": self.heating,
            "sterilization_on": self.sterilization_on,
            "alarms": self.alarms,
            "connected": self.connected,
            "locked": self._locked(),
        })
        return data

    def _load_config_from_attributes(self, data: dict) -> None:
        self._config = CFG.HaierWHConfig(self.device_model, self.hass.config.path(C.DOMAIN))
        attributes = data.setdefault("attributes", [])
        attrs = list(sorted(map(lambda x: CFG.Attribute(x), attributes), key=lambda x: x.code))
        for attr in attrs:
            self.config.attrs.append(attr)
        self.config.merge_attributes()
        for attr in self.config.attrs:
            self._apply_value(attr.code, attr.current)
            if attr.name == "target_temperature" and attr.range:
                self.min_temperature = float(attr.range.min_value)
                self.max_temperature = float(attr.range.max_value)
            _LOGGER.debug(f"{self.device_name}: {attr}")
        self.constraint.extend(data.setdefault("constraint", []))

    def _set_attribute_value(self, code: str, value: str) -> None:
        attr = self.config.get_attr_by_code(code)
        if not (attr and value is not None):
            return
        # booleans come as "true"/"false" in the attribute lists and as "1"/"0" in values
        value = CFG.Constraint.value2code(str(value))
        try:
            if attr.name == "current_temperature":
                self.current_temperature = float(value)
            elif attr.name == "target_temperature":
                self.target_temperature = float(value)
            elif attr.name == "status":
                self.status = parsebool(value)
            elif attr.name == "heating_mode":
                # "none" is reported while sterilization runs, afterwards the
                # device returns to the previous mode: keep showing that one
                if (mode := attr.get_item_name(value)) not in (None, "none", "None"):
                    self.heating_mode = mode
            elif attr.name == "heating_status":
                self.heating = attr.get_item_name(value) == "heating"
            elif attr.name == "sterilization":
                self.sterilization_on = parsebool(value)
        except ValueError:
            _LOGGER.warning(f"{self.device_name}: bad value {value!r} for {attr.name}")

    def _apply(self, commands: list[dict]) -> None:
        """Send commands and show the expected state until the device echoes it."""
        if not commands:
            return
        self._send_commands(commands)
        for command in commands:
            self._set_attribute_value(str(command.get("commandName")), command.get("value"))
        self.write_ha_state()

    def get_heating_modes(self) -> list[str]:
        return [m for m in self.config.get_values("heating_mode") if m != "none"]

    def set_temperature(self, value: float) -> None:
        self._check_writable("target_temperature")
        value = int(round(min(max(float(value), self.min_temperature), self.max_temperature)))
        self._apply([{
            "commandName": self.config["target_temperature"],
            "value": str(value),
        }])

    def switch_on(self) -> None:
        self._check_writable("status")
        self._apply(self.get_commands("status", "on"))

    def switch_off(self) -> None:
        self._check_writable("status")
        self._apply(self.get_commands("status", "off"))

    def set_heating_mode(self, value: str) -> None:
        if value not in self.get_heating_modes():
            raise ValueError(f"Unsupported heating mode: {value}")
        commands = []
        if not self.status:
            # the mode cannot be changed while the device is off: turn it on first
            self._check_writable("status")
            commands += self.get_commands("status", "on")
        self._check_writable("heating_mode", value, assume={self.config["status"]: "1"})
        commands += self.get_commands("heating_mode", value)
        self._apply(commands)

    def set_sterilization_on(self, value: bool) -> None:
        self._check_writable("sterilization")
        self._apply(self.get_commands("sterilization", value))

    def create_entities_water_heater(self) -> list:
        from . import water_heater
        return [water_heater.HaierWHEntity(self)]

    def create_entities_switch(self) -> list:
        from . import switch
        entities = []
        if self.config['sterilization'] is not None:
            entities.append(switch.HaierWHSterilizationSwitch(self))
        return entities

    def create_entities_sensor(self) -> list:
        from . import sensor
        entities = []
        if self.config['current_temperature'] is not None:
            entities.append(sensor.HaierWHTemperatureSensor(self))
        return entities

    def create_entities_binary_sensor(self) -> list:
        from . import binary_sensor
        entities = [
            binary_sensor.HaierProblemSensor(self),
            binary_sensor.HaierConnectivitySensor(self),
            binary_sensor.HaierFirmwareUpdateSensor(self),
        ]
        if self.config['heating_status'] is not None:
            entities.append(binary_sensor.HaierWHHeatingSensor(self))
        return entities


def _same_value(a, b) -> bool:
    a, b = CFG.Constraint.value2code(str(a)), CFG.Constraint.value2code(str(b))
    try:
        return float(a) == float(b)  # "24.00" == "24"
    except ValueError:
        return a == b


def already_applied(message: dict, data: dict) -> bool:
    """True if a REST status already holds every value the command asks for."""
    current = {
        str(a.get("name")): a.get("currentValue")
        for a in (data.get("attributes") or [])
        if isinstance(a, dict)
    }
    commands = message.get("commands") or ([message["command"]] if message.get("command") else [])
    return bool(commands) and all(
        current.get(str(c.get("commandName"))) is not None
        and _same_value(current[str(c.get("commandName"))], c.get("value"))
        for c in commands
    )


MAINTENANCE_ALARMS = {"filterCleaningAlarm"}
FILTER_ALARM = "filterCleaningAlarm"


# washing machines, washer-dryers and dryers
LAUNDRY_TYPES = ("WM", "WD", "DRY", "TD")
# the state of a program as the cloud names it (control.currentProgram / program / events)
LAUNDRY_PROGRAM_STATES = {
    "IN_PROGRESS": "running", "STARTED": "running", "PAUSED": "paused",
    "FINISHED": "finished", "CANCELED": "cancelled", "CANCELLED": "cancelled",
}
LAUNDRY_STATES = ("off", "idle", "running", "paused", "finished", "cancelled")


def laundry_codes(data: dict) -> dict[str, str]:
    """{meaning: attribute code} from the app's screens in a description.

    The attributes of a washer or a dryer come without names, and the codes
    differ between models (remaining hours: 31 on HW60, 32 on HW90); the app's
    screens link to them as {"name": code, "description": "remainingTimeHH"}.
    The first link of a meaning wins.
    """
    codes_known = {str(a.get("name")) for a in device_part(data).get("attributes") or [] if isinstance(a, dict)}
    found: dict[str, str] = {}

    def walk(node, depth=0):
        if depth > 12:
            return
        if isinstance(node, dict):
            code, meaning = node.get("name"), node.get("description")
            if (isinstance(code, str) and code in codes_known and isinstance(meaning, str)
                    and re.fullmatch(r"[A-Za-z][A-Za-z0-9]{2,40}", meaning) and meaning != "not found"):
                found.setdefault(meaning, code)
            for key, value in node.items():
                if key not in ("smartDeviceControl", "attributes"):
                    walk(value, depth + 1)
        elif isinstance(node, list):
            for value in node:
                walk(value, depth + 1)

    walk(data if isinstance(data, dict) else {})
    return found


def laundry_screen(data: dict) -> dict:
    """The monitoring screen: a washer has it in allProgram, a dryer in device."""
    data = data if isinstance(data, dict) else {}
    for parent in (data.get("device"), data.get("allProgram")):
        screen = parent.get("monitoringScreen") if isinstance(parent, dict) else None
        if isinstance(screen, dict):
            return screen
    return {}


def laundry_phase_titles(data: dict) -> dict[str, str]:
    """{value of the cycle phase: the app's title}."""
    screen = laundry_screen(data)
    found = screen.get("phase")
    phase: dict = found if isinstance(found, dict) else {}
    titles = phase.get("cyclePhaseValueToTitle")
    out = {str(k): str(v) for k, v in titles.items() if isinstance(v, str) and v.strip()} if isinstance(titles, dict) else {}
    for block, key in (("weighing", "weightAttrValue"), ("finishing", "finishingAttrValue"),
                       ("finishing", "cyclePhaseValue"), ("freshAirSystem", "freshAirSystemAttrValue")):
        for parent in (screen, phase):
            part = parent.get(block)
            if isinstance(part, dict) and part.get(key) not in (None, "") and isinstance(part.get("title"), str):
                out.setdefault(str(part[key]), part["title"])
    return out


def _d(value) -> dict:
    return value if isinstance(value, dict) else {}


# control of washers and dryers (beta: only with the entry's option on); the
# states the app offers each action in
LAUNDRY_ACTION_STATES = {
    "start": ("idle", "finished", "cancelled", "off"),
    "pause": ("running",),
    "resume": ("paused",),
    "cancel": ("running", "paused"),
}


def _laundry_value_allowed(field: dict, value) -> bool:
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


def laundry_programs(data: dict) -> list[dict]:
    """The programs a washer or dryer is started with, in the app's order:
    [{"title", "link", "group", "commands"}].

    The app's catalogue (allProgram of a washer, device.programDetails of a
    dryer) links every program to its definition in
    smartDeviceControl.businessAttributes: a group command with a value for
    each of its attributes, the program's selected values over the
    defaults. Sending it starts the program (a dryer's also turns the
    machine on and sets it running). An attribute without a value (the
    delayed start's status, the machine sets it) is not sent, as the fork
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
            if value is None or not _laundry_value_allowed(field, value):
                value = field.get("defaultValue")
            if value is None or isinstance(value, (dict, list)):
                continue  # the machine sets it
            commands.append({"commandName": str(field["name"]), "value": str(value).lower() if isinstance(value, bool) else str(value)})
        if commands:
            titles.add(title.strip())
            out.append({"title": title.strip(), "link": link, "group": str(definition["name"]), "commands": commands})
    return out


def laundry_actions(data: dict) -> dict[str, dict]:
    """{"pause" | "resume" | "cancel": {"commandName", "value"}} from the app's
    control screens: controlBlock of a washer, device.mainControl of a dryer.
    An action the description gives no attribute of that the device has is
    left out."""
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
        cancel = _d(dryer.get("cancel"))
        candidates.update({
            "pause": (code, _d(button.get("onButton")).get("commandValue")),
            "resume": (code, _d(button.get("offButton")).get("commandValue")),
            "cancel": (_d(cancel.get("link")).get("name"), cancel.get("cancelValue")),
        })
    out = {}
    for action, (code, value) in candidates.items():
        if (code is not None and str(code) in known and isinstance(value, (str, int, float))
                and not isinstance(value, bool) and str(value) != ""):
            out[action] = {"commandName": str(code), "value": str(value)}
    return out


class HaierLaundry(HaierDevice):
    """A washing machine, washer-dryer or dryer in the cloud's current format:
    its description (inside smartDeviceControl) has attributes without names,
    and what a code means is read from the app's screens (laundry_codes), so
    models and kinds with other codes work alike: what is running, the
    phase, the time left, remote control, the child lock and the alarms.

    Control is a beta, only with the entry's option "laundry_control" on: a
    program is chosen (nothing is sent) and started, paused, resumed or
    cancelled with the commands the cloud's description gives
    (laundry_programs, laundry_actions), and only when the machine allows
    remote control and is in a state the app offers the action in. It has
    not been checked on a real machine.
    """

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
        self._control_lock = threading.Lock()  # one command at a time
        self._get_status(backend_data or {})

    @staticmethod
    def describes(data) -> bool:
        return isinstance(data, dict) and isinstance(data.get("smartDeviceControl"), dict)

    def _load_config_from_attributes(self, data: dict) -> None:
        config = CFG.HaierDeviceConfig(self.device_model, self.hass.config.path(C.DOMAIN))
        config.attrs = [CFG.Attribute(a) for a in data.get("attributes") or [] if isinstance(a, dict)]
        self._config = config  # type: ignore[assignment]  # the base declares it as None (old typing)
        whole = self.status_data
        self.codes = laundry_codes(whole)
        self.phase_titles = laundry_phase_titles(whole)
        self.is_dryer = isinstance(whole.get("device"), dict)
        self._apply_program(self._program_block(whole))
        self.programs = laundry_programs(whole)
        self.actions = laundry_actions(whole)
        if self.selected_program not in {p["title"] for p in self.programs}:
            # the last program of the machine, as the app suggests it
            link = _d(_d(self._program_block(whole)).get("config")).get("link")
            self.selected_program = next((p["title"] for p in self.programs if p["link"] == _d(link).get("name")), None)

    @staticmethod
    def _program_block(data) -> dict | None:
        data = data if isinstance(data, dict) else {}
        program = data.get("program")
        if isinstance(program, dict):
            return program
        control = data.get("control")
        current = control.get("currentProgram") if isinstance(control, dict) else None
        return current if isinstance(current, dict) else None

    def _apply_program(self, program: dict | None) -> None:
        if program is None:
            self.program_title = self.program_status = None
            return
        title = program.get("title")
        self.program_title = title.strip() if isinstance(title, str) and title.strip() else None
        status = program.get("status")
        self.program_status = str(status).upper().removeprefix("PROGRAM_") if isinstance(status, str) and status else None

    def update_from_backend(self, data: dict) -> None:
        if isinstance(data, dict):
            self._apply_program(self._program_block(data))
        super().update_from_backend(data)

    def _set_attribute_value(self, code: str, value: str) -> None:
        pass  # the raw values (self._values) are read where they are needed

    def _raw(self, meaning: str) -> str | None:
        code = self.codes.get(meaning)
        return None if code is None else self._values.get(code)

    def _flag(self, meaning: str) -> bool | None:
        raw = self._raw(meaning)
        return None if raw is None else raw in ("1", "on")

    def _number(self, meaning: str) -> int | None:
        raw = self._raw(meaning)
        try:
            value = float(raw) if raw is not None else float("nan")
        except (TypeError, ValueError):
            return None
        return int(value) if value == value and abs(value) != float("inf") else None

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
        return self._raw("cyclePhase")

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
            data = message_dict.get("data")
            data = data if isinstance(data, dict) else {}
            program = data.get("program")
            if isinstance(program, dict):
                self._apply_program(program)
            elif data.get("status") in ("PROGRAM_STARTED", "PROGRAM_FINISHED"):
                self.program_status = str(data["status"]).removeprefix("PROGRAM_").replace("STARTED", "IN_PROGRESS")
            self.changed_at = time.monotonic()
            self.write_ha_state()
        elif event == "alarms":
            payload = message_dict.get("payload")
            alarms = payload.get("alarms") if isinstance(payload, dict) else None
            if isinstance(alarms, list):
                self._set_alarms(alarms)
                self.write_ha_state()
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
        entities = [binary_sensor.HaierProblemSensor(self)]
        if "remoteCtrValid" in self.codes:
            entities.append(binary_sensor.HaierLaundryRemoteControlSensor(self))
        if "childLockStatus" in self.codes:
            entities.append(binary_sensor.HaierLaundryChildLockSensor(self))
        return entities

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
        self.write_ha_state()

    def _check_control(self, action: str, translation_key: str) -> None:
        """translation_key: the error when the state does not allow the action."""
        if not self.control_enabled:
            raise self._control_error(translation_key="laundry_control_off")
        if self.remote_control is not True:
            # off on the machine, or the description does not say: the app sends nothing either
            raise self._control_error(translation_key="laundry_remote_off")
        if self.state not in LAUNDRY_ACTION_STATES[action] or (action != "start" and action not in self.actions):
            raise self._control_error(translation_key=translation_key, state=str(self.state))

    def _send_control(self, message: dict) -> None:
        if not self._control_lock.acquire(blocking=False):
            raise self._control_error(translation_key="laundry_busy")  # a second press while the first command is on its way
        try:
            self._send_message(message)
        finally:
            self._control_lock.release()

    def start(self) -> None:
        """Start the chosen program with its values: one group command, as the app sends it."""
        self._check_control("start", translation_key="laundry_cannot_start")
        program = next((p for p in self.programs if p["title"] == self.selected_program), None)
        if program is None:
            raise self._control_error(translation_key="laundry_no_program")
        self._send_control({
            "action": "operation",
            "macAddress": self.device_id,
            "commandName": program["group"],
            "commands": [dict(c) for c in program["commands"]],
        })

    def _act(self, action: str, translation_key: str) -> None:
        self._check_control(action, translation_key=translation_key)
        self._send_control({"action": "command", "macAddress": self.device_id, "command": dict(self.actions[action])})

    def pause(self) -> None:
        self._act("pause", translation_key="laundry_cannot_pause")

    def resume(self) -> None:
        self._act("resume", translation_key="laundry_cannot_resume")

    def cancel(self) -> None:
        self._act("cancel", translation_key="laundry_cannot_cancel")

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


def parsebool(value) -> bool:
    if value in ("on", 1, True, "true", "enable", "1"):
        return True
    return False


def parsefloat(value, default: float | None = None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def number_text(value) -> str:
    """A setpoint as the app sends it and the cloud reports it: "23", not "23.0".

    Home Assistant's services hand over floats (23.0); a half degree stays "23.5".
    """
    number = float(value)
    return str(int(number)) if number.is_integer() else str(number)


# Parts of a device description the integration never reads: app analytics,
# pairing details, scenario links. Links (signed for a week) are dropped too.
_UNCACHED_KEYS = {"analyticsEvents", "analyticsEvent", "trackingData", "pairingInfo", "scenarios"}


def cacheable_description(data: dict | None) -> dict:
    """A copy of a device description that is worth saving between starts."""
    def clean(value):
        if isinstance(value, dict):
            return {k: clean(v) for k, v in value.items() if k not in _UNCACHED_KEYS}
        if isinstance(value, list):
            return [clean(v) for v in value]
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return ""
        return value
    return clean(data if isinstance(data, dict) else {})
