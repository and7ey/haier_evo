from __future__ import annotations
import requests
import json
import time
import threading
import uuid
import socket
from aiohttp import web
from enum import Enum
from datetime import datetime, timezone, timedelta
from tenacity import retry, stop_after_attempt, retry_if_exception_type, wait_fixed
from websocket import WebSocketException, WebSocketApp, WebSocket
from requests.exceptions import ConnectionError, Timeout, HTTPError
from urllib.parse import urlparse, urljoin, parse_qs
from urllib3.exceptions import NewConnectionError
from homeassistant.core import HomeAssistant
from homeassistant import exceptions
from homeassistant.components.climate.const import ClimateEntityFeature, HVACMode, SWING_OFF, PRESET_NONE
from homeassistant.components.http import HomeAssistantView
from .config import HaierACConfig
from .logger import _LOGGER
from .limits import ResettableLimits
from . import config
from . import const as C # noqa


class InvalidAuth(exceptions.HomeAssistantError):
    """Error to indicate we cannot connect."""

class InvalidDevicesList(exceptions.HomeAssistantError):
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


class SocketStatus(Enum):
    PRE_INITIALIZATION = 0
    INITIALIZING = 1
    INITIALIZED = 2
    NOT_INITIALIZED = 3


class HaierAPI(HomeAssistantView):
    url = "/api/haier_evo"
    name = "/api:haier_evo"
    requires_auth = False

    def __init__(self, haier: Haier):
        self.haier = haier

    async def get(self, request):
        if not self.haier.allow_http:
            return web.Response(text="Not found", status=404, content_type="text/plain")
        return self.json(self.haier.to_dict())

    async def post(self, request):
        if not self.haier.allow_http:
            return web.Response(text="Not found", status=404, content_type="text/plain")
        data = await request.json()
        self.haier.send_message(json.dumps(data))
        return self.json({"result": "success"})


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
    def access_token(self) -> str | None:
        assert "accessToken" in self.token, f"Bad data: refreshToken not found"
        value = self.token["accessToken"]
        assert isinstance(value, str) and value, f"Bad token: {value!r}"
        return value

    @property
    def access_expire(self) -> datetime | None:
        assert "expire" in self.token, f"Bad data: expire not found"
        value = self.token["expire"]
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z")

    @property
    def refresh_token(self) -> str | None:
        assert "refreshToken" in self.token, f"Bad data: refreshToken not found"
        value = self.token["refreshToken"]
        assert isinstance(value, str) and value, f"Bad token: {value!r}"
        return value

    @property
    def refresh_expire(self) -> datetime | None:
        assert "refreshExpire" in self.token, f"Bad data: refreshExpire not found"
        value = self.token["refreshExpire"]
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z")


class Haier(object):

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

    def __init__(self, hass: HomeAssistant, email: str, password: str, http: bool = C.API_HTTP_ROUTE) -> None:
        self.hass: HomeAssistant = hass
        self.devices: list[HaierAC] = []
        self._email: str = email
        self._password: str = password
        self._lock = threading.Lock()
        self._token: str | None = None
        self._tokenexpire: datetime | None = None
        self._refreshtoken: str | None = None
        self._refreshexpire: datetime | None = None
        self._socket_app: WebSocketApp | None = None
        self._disconnect_requested = False
        self._socket_status: SocketStatus = SocketStatus.PRE_INITIALIZATION
        self._socket_thread = None
        self._pull_data = None
        self.allow_http: bool = http
        hass.http.register_view(HaierAPI(self))
        self.reset_limits()

    @property
    def token(self) -> str | None:
        return self._token

    @property
    def load_tokens(self):
        return self._load_tokens

    @property
    def write_ha_state(self):
        return self._write_ha_state

    @property
    def socket_status(self) -> SocketStatus:
        return self._socket_status

    @socket_status.setter
    def socket_status(self, status: SocketStatus):
        self._socket_status = status
        # _LOGGER.debug("socket_status: %s", status)

    def _load_tokens(self) -> None:
        filename = self.hass.config.path(C.DOMAIN)
        try:
            with open(filename, "r") as f:
                data = json.load(f)
            assert isinstance(data, dict), "Bad saved tokens file"
            self._token = data.get("token", None)
            tokenexpire = data.get("tokenexpire")
            self._tokenexpire = datetime.fromisoformat(tokenexpire) if tokenexpire else None
            self._refreshtoken = data.get("refreshtoken", None)
            refreshexpire = data.get("refreshexpire")
            self._refreshexpire = datetime.fromisoformat(refreshexpire) if refreshexpire else None
            _LOGGER.info(f"Loaded tokens file: {filename}")
        except FileNotFoundError:
            _LOGGER.warning(f"No tokens file: {filename}")
        except Exception as e:
            _LOGGER.error(f"Failed to load tokens file: {e}")

    def _save_tokens(self) -> None:
        try:
            filename = self.hass.config.path(C.DOMAIN)
            with open(filename, "w") as f:
                json.dump({
                    "token": self._token,
                    "tokenexpire": str(self._tokenexpire) if self._tokenexpire else None,
                    "refreshtoken": self._refreshtoken,
                    "refreshexpire": str(self._refreshexpire) if self._refreshexpire else None,
                }, f)
        except Exception as e:
            _LOGGER.error(f"Failed to save tokens file: {e}")
        else:
            _LOGGER.info(f"Saved tokens file: {filename}")

    def _clear_tokens(self) -> None:
        self._token = None
        self._tokenexpire = None
        self._refreshtoken = None
        self._refreshexpire = None
        self._save_tokens()

    def reset_limits(self) -> None:
        self.common_limits.reset()
        self.auth_login_limits.reset()
        self.auth_refresh_limits.reset()

    def stop(self) -> None:
        self._disconnect_requested = True
        self.reset_limits()
        if self._socket_app is not None:
            self._socket_app.close()

    def to_dict(self) -> dict:
        return {
            "socket_status": self._socket_status,
            "backend_data": self._pull_data,
            "devices": [device.to_dict() for device in self.devices]
        }

    @common_limits.sleep_and_retry
    @common_limits
    def make_request(self, method: str, url: str, **kwargs) -> requests.Response:
        try:
            assert self._disconnect_requested is False, 'Service already stoped'
            # Setting a default timeout for requests
            kwargs.setdefault('timeout', C.API_TIMEOUT)
            headers = kwargs.setdefault('headers', {})
            headers.setdefault('User-Agent', "curl/7.81.0")
            headers.setdefault('Accept', "*/*")
            resp = requests.request(method, url, **kwargs)
            _LOGGER.debug(resp.text)
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

    @auth_login_limits.sleep_and_retry
    @auth_login_limits
    def auth_login(self) -> AuthResponse:
        try:
            path = urljoin(C.API_PATH, C.API_LOGIN)
            _LOGGER.info(f"Logging in to {path} with email {self._email}")
            response = AuthResponse(self.make_request('POST', path, data={
                'email': self._email,
                'password': self._password
            }))
            # _LOGGER.info(f"Login ({self._email}) status code: {response.status_code}")
            response.raise_for_error()
        except ManyRequestsError as e:
            self.auth_login_limits.add_period(C.LOGIN_LIMIT_429)
            raise e
        except AuthInternalError as e:
            _LOGGER.error(str(e))
            self.auth_login_limits.add_period(C.LOGIN_LIMIT_500)
            response = e.response
        except AuthUserError as e:
            self._disconnect_requested = True
            raise e
        else:
            self.auth_login_limits.set_period()
        finally:
            self.auth_refresh_limits.reset()
        return response

    @auth_refresh_limits.sleep_and_retry
    @auth_refresh_limits
    def auth_refresh(self) -> AuthResponse:
        try:
            path = urljoin(C.API_PATH, C.API_TOKEN_REFRESH)
            _LOGGER.info(f"Refreshing token in to {path} with email {self._email}")
            response = AuthResponse(self.make_request('POST', path, data={
                'refreshToken': self._refreshtoken
            }))
            # _LOGGER.info(f"Refresh ({self._email}) status code: {response.status_code}")
            response.raise_for_error()
        except ManyRequestsError as e:
            self.auth_refresh_limits.add_period(C.REFRESH_LIMIT_429)
            raise e
        except AuthValidationError as e:
            _LOGGER.error(str(e))
            self._clear_tokens()
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

    @retry(
        retry=retry_if_exception_type((AuthValidationError,)),
        stop=stop_after_attempt(2),
    )
    def login(self, refresh: bool = False) -> None:
        resp = None
        try:
            if refresh and self._refreshtoken:  # token refresh
                resp = self.auth_refresh()
            else:  # initial login
                resp = self.auth_login()
            assert resp, "No response from login"
            self._token = resp.access_token
            self._tokenexpire = resp.access_expire
            self._refreshtoken = resp.refresh_token
            self._refreshexpire = resp.refresh_expire
            self._save_tokens()
        except AssertionError as e:
            _LOGGER.error(f"Assertion error: {e}")
        except Exception as e:
            _LOGGER.error(
                f"Failed to login/refresh token for email {self._email}, "
                f"response was: {resp}, "
                f"err: {e}"
            )
            raise InvalidAuth()
        else:
            _LOGGER.info(f"Successful update tokens for email {self._email}")

    def auth(self) -> None:
        with self._lock:
            tzinfo = timezone(timedelta(hours=+3.0))
            # tzinfo = datetime.now(timezone.utc).astimezone().tzinfo
            now = datetime.now(tzinfo)
            tokenexpire = self._tokenexpire or now
            refreshexpire = self._refreshexpire or now
            if self._token:
                if tokenexpire > now:
                    return None
                elif self._refreshtoken and refreshexpire > now:
                    _LOGGER.info(f"Token to be refreshed")
                    return self.login(refresh=True)
            _LOGGER.info(f"Token expired or empty")
            return self.login()

    def pull_data_from_api(self) -> dict:
        response = None
        try:
            devices_path = urljoin(C.API_PATH, C.API_DEVICES)
            _LOGGER.info(f"Getting devices, url: {devices_path}")
            response = requests.get(devices_path, headers={
                'X-Auth-Token': self._token,
                'User-Agent': 'evo-mobile',
                'Device-Id': str(uuid.uuid4()),
                'Content-Type': 'application/json'
            }, timeout=C.API_TIMEOUT)
            _LOGGER.debug(response.text)
            response.raise_for_status()
            data = response.json().get("data", {})
            assert isinstance(data, dict), f"Data is not dict: {data}"
            return data
        except Exception as e:
            _LOGGER.error(f"Failed to get devices {e}, response was: {response}")
            return {}

    def pull_data(self) -> None:
        self.auth()
        self._pull_data = data = self.pull_data_from_api()
        if not self._pull_data:
            raise InvalidDevicesList()
        need_container_id = "72a6d224-cb66-4e6d-b427-2e4609252684"
        presentation = data.setdefault("presentation", {})
        layout = presentation.setdefault("layout", {})
        containers = layout.setdefault("scrollContainer", [])
        for item in containers[:]:
            tracking_data = item.setdefault("trackingData", {})
            component = tracking_data.setdefault("component", {})
            component_id = component.setdefault("componentId", "")
            # _LOGGER.debug(component_id)
            component_name = component.setdefault("componentName", "")
            if not (
                component_name == "deviceList"
                and component_id == need_container_id
            ):
                containers.remove(item)
                continue
            state_data = item.setdefault("state", "{}")
            state_json = item['state'] = json.loads(state_data)
            devices = state_json.setdefault("items", [])
            for d in devices:
                device_title = d.get('title', '')
                device_link = d.get('action', {}).get('link', '')
                parsed_link = urlparse(device_link)
                query_params = parse_qs(parsed_link.query)
                device_mac = query_params.get('deviceId', [''])[0]
                device_mac = device_mac.replace('%3A', ':')
                device_serial = query_params.get('serialNum', [''])[0]
                device = HaierAC(
                    haier=self,
                    device_mac=device_mac,
                    device_serial=device_serial,
                    device_title=device_title
                )
                self.devices.append(device)
                _LOGGER.info(f"Added device: {device}")
        if len(self.devices) > 0:
            self.connect_in_thread()

    def get_device_by_id(self, id_: str) -> HaierAC | None:
        return next(filter(lambda d: d.device_id == id_, self.devices), None)

    def _init_ws(self) -> None:
        self.auth()
        url = urljoin(C.API_WS_PATH, self.token)
        if self._socket_app is None:
            self._socket_app = WebSocketApp(
                url=url,
                on_message=self._on_message,
                on_open=self._on_open,
                on_ping=self._on_ping,
                on_close=self._on_close,
            )
        else:
            self._socket_app.url = url

    # noinspection PyUnusedLocal
    def _on_message(self, ws: WebSocket, message: str) -> None:
        _LOGGER.debug(f"Received WSS message: {message}")
        message_dict: dict = json.loads(message)
        message_device = str(message_dict.get("macAddress")).lower()
        device = self.get_device_by_id(message_device)
        if device is None:
            _LOGGER.error(f"Got a message for a device we don't know about: {message_device}")
        else:
            device.on_message(message_dict)

    # noinspection PyMethodMayBeStatic,PyUnusedLocal
    def _on_open(self, ws: WebSocket) -> None:
        _LOGGER.debug("Websocket opened")
        self.socket_status = SocketStatus.INITIALIZED

    # noinspection PyUnusedLocal
    def _on_ping(self, ws: WebSocket) -> None:
        self._socket_app.sock.pong()

    # noinspection PyMethodMayBeStatic,PyUnusedLocal
    def _on_close(self, ws: WebSocket, close_code: int, close_message: str) -> None:
        _LOGGER.debug(f"Websocket closed. Code: {close_code}, message: {close_message}")
        # self._auto_reconnect_if_needed()

    def _auto_reconnect_if_needed(self, command: str = None) -> None:
        self._socket_status = SocketStatus.NOT_INITIALIZED
        if not self._disconnect_requested:
            _LOGGER.debug(f"Automatically reconnecting on unwanted closed socket. {command}")
            self.connect_in_thread()
        else:
            _LOGGER.debug("Disconnect was explicitly requested, not attempting to reconnect")

    def _wait_websocket(self, timeout: float) -> None:
        current = time.time()
        if self.socket_status == SocketStatus.INITIALIZED:
            return
        while time.time() <= (current + timeout):
            if self.socket_status == SocketStatus.INITIALIZED:
                return
            time.sleep(0.1)

    def _write_ha_state(self) -> None:
        for device in self.devices:
            device.write_ha_state()

    def connect_if_needed(self, timeout: float = 4.0) -> None:
        if self._socket_thread and self._socket_thread.is_alive():
            return self._wait_websocket(timeout)
        return self.connect_in_thread()

    def connect(self) -> None:
        self.socket_status = SocketStatus.NOT_INITIALIZED
        while not self._disconnect_requested:
            _LOGGER.debug(f"Connecting to websocket ({C.API_WS_PATH})")
            try:
                self.socket_status = SocketStatus.INITIALIZING
                self._init_ws()
                self._socket_app.run_forever()
            except WebSocketException: # socket is already opened
                pass
            except Exception as e:
                _LOGGER.error(f"Error connecting to websocket: {e}")
        _LOGGER.info("Connection stoped")

    def connect_in_thread(self) -> None:
        self._socket_thread = thread = threading.Thread(target=self.connect)
        thread.daemon = True
        thread.start()

    @retry(
        retry=retry_if_exception_type(Exception),
        stop=stop_after_attempt(2),
        retry_error_callback=lambda _: None,
        wait=wait_fixed(0.5),
    )
    def send_message(self, payload: str) -> None:
        _LOGGER.debug(f"Sending message: {payload}")
        try:
            self._socket_app.send(payload)
        except Exception as e:
            _LOGGER.warning(f"Failed to send message: {e}")
            self.connect_if_needed()
            raise e


class HaierAC(object):

    def __init__(
        self,
        haier: Haier,
        device_mac: str,
        device_serial: str = None,
        device_title: str = None
    ) -> None:
        self._haier = haier
        self._device_id = device_mac
        self._device_serial = device_serial
        self._device_name = device_title
        self.device_model = "AC"
        self._current_temperature = 0
        self._target_temperature = 0
        self._status = None
        self._mode = None
        self._fan_mode = None
        self._swing_horizontal_mode = None
        self._swing_mode = None
        self._preset_mode = None
        self._min_temperature = 7
        self._max_temperature = 35
        self._sw_version = None
        self._available = True
        self._light_on = True
        self._sound_on = True
        self._quiet_on = False
        self._turbo_on = False
        self._health_on = False
        self._comfort_on = False
        self._status_data = None
        self._config = None
        self._write_ha_state_callbacks = []
        self._get_status()

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}("
            f"{self._device_id!r}, "
            f"name={self._device_name!r}, "
            f"serial={self._device_serial!r}, "
            f"model={self.device_model!r}"
            f")"
        )

    @property
    def status_data(self):
        return self._status_data

    @property
    def hass(self) -> HomeAssistant:
        return self._haier.hass

    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    def device_mac(self) -> str:
        return self._device_id

    @property
    def device_name(self) -> str:
        return self._device_name

    @property
    def sw_version(self) -> str:
        return self._sw_version

    @property
    def max_temperature(self) -> float:
        return self._max_temperature

    @property
    def min_temperature(self) -> float:
        return self._min_temperature

    @property
    def current_temperature(self) -> float:
        return self._current_temperature

    @property
    def target_temperature(self) -> float:
        return self._target_temperature

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def fan_mode(self) -> str:
        return self._fan_mode

    @property
    def swing_horizontal_mode(self) -> str:
        return self._swing_horizontal_mode

    @property
    def swing_mode(self) -> str:
        return self._swing_mode

    @property
    def preset_mode(self) -> str:
        return self._preset_mode

    @property
    def status(self) -> int:
        return self._status

    @property
    def light_on(self) -> bool:
        return self._light_on

    @property
    def sound_on(self) -> bool:
        return self._sound_on

    @property
    def quiet_on(self) -> bool:
        return self._quiet_on

    @property
    def turbo_on(self) -> bool:
        return self._turbo_on

    @property
    def comfort_on(self) -> bool:
        return self._comfort_on

    @property
    def health_on(self) -> bool:
        return self._health_on

    @property
    def available(self) -> bool:
        return self._available

    @available.setter
    def available(self, value: bool):
        self._available = bool(value)

    @property
    def config(self) -> HaierACConfig:
        return self._config

    def update(self) -> None:
        self._haier.auth()

    def write_ha_state(self) -> None:
        for callback in self._write_ha_state_callbacks:
            self.hass.loop.call_soon_threadsafe(callback)

    def add_write_ha_state_callback(self, callback) -> None:
        if callback not in self._write_ha_state_callbacks:
            self._write_ha_state_callbacks.append(callback)

    def to_dict(self) -> dict:
        return {
            "device_id": self.device_id,
            "device_mac": self.device_mac,
            "device_name": self.device_name,
            "device_serial": self._device_serial,
            "sw_version": self.sw_version,
            "max_temperature": self.max_temperature,
            "min_temperature": self.min_temperature,
            "current_temperature": self.current_temperature,
            "target_temperature": self.target_temperature,
            "fan_mode": self.fan_mode,
            "swing_mode": self.swing_mode,
            "preset_mode": self.preset_mode,
            "status": self.status,
            "light_on": self.light_on,
            "sound_on": self.sound_on,
            "quiet_on": self.quiet_on,
            "turbo_on": self._turbo_on,
            "health_on": self.health_on,
            "comfort_on": self._comfort_on,
            "available": self.available,
            "mode": self.mode,
            "backend_data": self.status_data,
            "config": self.config.to_dict()
        }

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
        else:
            _LOGGER.warning(f"Got unknown message: {message_dict}")

    def _set_attribute_value(self, key: str, value) -> None:
        if key == self.config['current_temperature']:  # Температура в комнате
            self._current_temperature = float(value)
        elif key == self.config['status']:  # Включение/выключение
            self._status = int(value)
        elif key == self.config['target_temperature']:  # Целевая температура
            self._target_temperature = float(value)
        elif key == self.config['mode']:  # Режимы
            self._mode = self.config.get_value("mode", value)
        elif key == self.config['fan_mode']:  # Скорость вентилятора
            self._fan_mode = self.config.get_value("fan_mode", value)
        elif key == self.config['swing_horizontal_mode']:
            self._swing_horizontal_mode = self.config.get_value("swing_horizontal_mode", value)
        elif key == self.config['swing_mode']:
            self._swing_mode = self.config.get_value("swing_mode", value)
        elif key == self.config['light']:
            self._light_on = parsebool(self.config.get_value("light", value))
        elif key == self.config['sound']:
            self._sound_on = parsebool(self.config.get_value("sound", value))
        elif key == self.config['quiet']:
            self._quiet_on = parsebool(self.config.get_value("quiet", value))
        elif key == self.config['turbo']:
            self._turbo_on = parsebool(self.config.get_value("turbo", value))
        elif key == self.config['health']:
            self._health_on = parsebool(self.config.get_value("health", value))
        elif key == self.config['comfort']:
            self._comfort_on = parsebool(self.config.get_value("comfort", value))
        # elif key == self.config.preset_mode_sleep:
        #     self._preset_mode_sleep = parsebool(self.config.get_value("preset_mode_sleep", value))
        # elif key == self.config_preset_mode:
        #     self._preset_mode = self.config.get_value(self.config_preset_mode, int(value))

    def _load_config_from_attributes(self, data: dict) -> None:
        self._config = config.HaierACConfig(self.device_model, self.hass.config.path(C.DOMAIN))
        attributes = data.setdefault("attributes", [])
        sensors = data.setdefault("sensors", {}).get("items", [])
        sensor_curr_temp = next(filter(lambda i: (
            isinstance(i, dict)
            and isinstance(i.get("value"), dict)
            and i.get("value", {}).get("description") == "indoorTemperature"
        ), sensors), {}).get("value", {}).get("name")
        attrs = list(sorted(map(lambda x: config.Attribute(x), attributes), key=lambda x: x.code))
        for attr in attrs:
            if attr.name == "current_temperature" and str(attr.code) != sensor_curr_temp:
                continue
            self.config.attrs.append(attr)
            if attr.command_name and self.config.command_name is None:
                self.config.command_name = attr.command_name
            self._set_attribute_value(str(attr.code), attr.current)
        attr = self.config.get_attr_by_name("target_temperature")
        if attr:
            self._min_temperature = float(attr.range.min_value)
            self._max_temperature = float(attr.range.max_value)
        self.config.merge_attributes()
        for attr in self.config.attrs:
            _LOGGER.info(f"{self._device_name}: {attr}")
        _LOGGER.info(self.config)

    def _get_status_data(self) -> dict | None:
        response = None
        try:
            status_url = C.API_STATUS.replace("{mac}", self.device_id)
            _LOGGER.info(f"Getting initial status of device {self.device_id}, url: {status_url}")
            response = requests.get(
                url=status_url,
                headers={"X-Auth-token": self._haier.token},
                timeout=C.API_TIMEOUT
            )
            _LOGGER.info(f"Update device {self.device_id} status code: {response.status_code}")
            _LOGGER.debug(response.text)
            response.raise_for_status()
            data = response.json()
            return data
        except Exception as e:
            _LOGGER.error(f"Failed to get status: {e}, response was: {response}")
            self._available = False
            return None

    def _get_status(self) -> None:
        self._status_data = data = self._get_status_data()
        if self._status_data:
            info = data.setdefault("info", {})
            self._device_serial = info.setdefault("serialNumber", self._device_serial)
            device_model = info.setdefault("model", "AC")
            device_model = device_model.replace('-','').replace('/', '')[:11]
            self.device_model = device_model
            self.set_available(data.setdefault("status", "ONLINE"))
            settings = data.setdefault("settings", {})
            self._device_name = settings.setdefault("name", {}).setdefault("name", self._device_name)
            self._sw_version = settings.setdefault('firmware', {}).setdefault('value', None)
            # read config and current values
            self._load_config_from_attributes(data)
            if self._swing_horizontal_mode is None:
                self._swing_horizontal_mode = SWING_OFF
            if self._swing_mode is None:
                self._swing_mode = SWING_OFF
            if self._preset_mode is None:
                self._preset_mode = PRESET_NONE
        self.write_ha_state()

    def _handle_status_update(self, received_message: dict) -> None:
        message_statuses = received_message.get("payload", {}).get("statuses", [{}])
        _LOGGER.info(f"Received status update {self.device_id} {received_message}")
        for key, value in message_statuses[0]['properties'].items():
            self._set_attribute_value(key, value)
        self._available = True
        self.write_ha_state()

    def _handle_device_status_update(self, received_message: dict) -> None:
        _LOGGER.info(f"Received status update {self.device_id} {received_message}")
        status = received_message.get("payload", {}).get("status")
        self.set_available(status)
        self.write_ha_state()

    def _handle_info(self, received_message: dict) -> None:
        payload = received_message.get("payload", {})
        self._sw_version = payload.get("swVersion") or self._sw_version

    def _send_message(self, message: str) -> None:
        self._haier.send_message(message)

    def _send_commands(self, commands: list[dict]) -> None:
        self._send_message(json.dumps({
            "action": "operation",
            "macAddress": self.device_id,
            "commandName": self.config.command_name,
            "commands": commands,
        }))

    def get_commands(self, name, value):
        if name == "preset_mode":
            func = getattr(self, f"get_preset_mode_{value}", None)
            if func is not None:
                return func()
            return self.get_preset_mode_command(value)
        if custom := self.config.get_command_by_name(f"{name}_{value}"):
            return custom
        attr = self.config.get_attr_by_name(name)
        return [{
            "commandName": str(attr.code),
            "value": str(getattr(next(filter(lambda i: i.name == value, attr.list), None), "value", None))
        }] if attr else []

    def get_preset_mode_none(self):
        if custom := self.config.get_command_by_name('preset_mode_none'):
            return custom
        return [{
            "commandName": str(attr.code),
            "value": getattr(next(filter(lambda i: i.name == "off", attr.list), None), "value", "0")
        } for attr in filter(lambda a: a.name.startswith("preset_mode"), self.config.attrs)]

    def get_preset_mode_command(self, mode):
        if custom := self.config.get_command_by_name(f'preset_mode_{mode}'):
            return custom
        attr = self.config.get_attr_by_name(f"preset_mode_{mode}")
        return [{
            "commandName": str(attr.code),
            "value": getattr(next(filter(lambda i: i.name == "on", attr.list), None), "value", "1"),
        }] if attr else []

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
        if self.config.preset_mode is not None:
            value = value | ClimateEntityFeature.PRESET_MODE
        return ClimateEntityFeature(value)

    def get_hvac_modes(self) -> list[HVACMode]:
        modes = []
        for mode in self.config.get_mapping_values('mode'):
            try:
                modes.append(HVACMode(mode))
            except ValueError:
                pass
        return modes + [HVACMode.OFF]

    def get_fan_modes(self) -> list[str]:
        return self.config.get_mapping_values('fan_mode')

    def get_swing_horizontal_modes(self) -> list[str]:
        return self.config.get_mapping_values('swing_horizontal_mode')

    def get_swing_modes(self) -> list[str]:
        return self.config.get_mapping_values('swing_mode')

    def get_preset_modes(self) -> list[str]:
        return ["none"] + self.config.get_preset_modes()

    def set_temperature(self, temp: float) -> None:
        self._send_commands([
            {
                "commandName": self.config['target_temperature'],
                "value": str(temp)
            }
        ])
        self._target_temperature = temp

    def switch_on(self, hvac_mode: str = None) -> None:
        hvac_mode = hvac_mode or self._mode or HVACMode.AUTO
        mode_haier = self.config.get_haier_code("mode", hvac_mode)
        self._send_commands([
            {
                "commandName": self.config['status'],
                "value": "1"
            },
            {
                "commandName": self.config['mode'],
                "value": str(mode_haier)
            }
        ])
        self._status = 1
        self._mode = hvac_mode

    def switch_off(self) -> None:
        self._send_commands([
            {
                "commandName": self.config['status'],
                "value": "0"
            }
        ])
        self._status = 0

    def set_fan_mode(self, fan_mode: str) -> None:
        if commands := self.get_commands(f"fan_mode", fan_mode):
            self._send_commands(commands)
            self._fan_mode = fan_mode

    def set_swing_horizontal_mode(self, swing_mode: str) -> None:
        if commands := self.get_commands(f"swing_horizontal_mode", swing_mode):
            self._send_commands(commands)
            self._swing_horizontal_mode = swing_mode

    def set_swing_mode(self, swing_mode: str) -> None:
        if commands := self.get_commands(f"swing_mode", swing_mode):
            self._send_commands(commands)
            self._swing_mode = swing_mode

    def set_preset_mode(self, preset_mode: str) -> None:
        if commands := self.get_commands(f"preset_mode", preset_mode):
            self._send_commands(commands)
            self._preset_mode = preset_mode

    def set_available(self, status: str) -> None:
        self._available = False if str(status).upper() == 'OFFLINE' else True

    def set_light_on(self, state: bool) -> None:
        value = "on" if state else "off"
        if commands := self.get_commands(f"light", value):
            self._send_commands(commands)
            self._light_on = state

    def set_sound_on(self, state: bool) -> None:
        value = "on" if state else "off"
        if commands := self.get_commands(f"sound", value):
            self._send_commands(commands)
            self._sound_on = state

    def set_quiet_on(self, state: bool) -> None:
        value = "on" if state else "off"
        if commands := self.get_commands(f"quiet", value):
            self._send_commands(commands)
            self._quiet_on = state

    def set_health_on(self, state: bool) -> None:
        value = "on" if state else "off"
        if commands := self.get_commands(f"health", value):
            self._send_commands(commands)
            self._health_on = state

    def set_turbo_on(self, state: bool) -> None:
        value = "on" if state else "off"
        if commands := self.get_commands(f"turbo", value):
            self._send_commands(commands)
            self._turbo_on = state

    def set_comfort_on(self, state: bool) -> None:
        value = "on" if state else "off"
        if commands := self.get_commands(f"comfort", value):
            self._send_commands(commands)
            self._comfort_on = state


def parsebool(value) -> bool:
    if value in ("on", 1, True, "true", "enable", "1"):
        return True
    return False
