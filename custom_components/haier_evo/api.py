from __future__ import annotations
import requests
import json
import time
import threading
import uuid
import socket
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
from .logger import _LOGGER
from .limits import ResettableLimits
from . import yaml_helper
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
        return self.json(self.haier.to_dict())

    async def post(self, request):
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
        if http is True:
            hass.http.register_view(HaierAPI(self))
        self.reset_limits()

    @property
    def token(self) -> str | None:
        return self._token

    @property
    def load_tokens(self):
        return self._load_tokens

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
            "pull_data": self._pull_data,
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
        message_device = message_dict.get("macAddress")
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

    def __init__(self, haier: Haier, device_mac: str, device_serial: str, device_title: str) -> None:
        self._haier = haier
        self._device_id = device_mac
        self._device_serial = device_serial
        self._device_name = device_title
        # the following values are updated below
        self.model_name = "AC"
        self._current_temperature = 0
        self._target_temperature = 0
        self._status = None
        self._mode = None
        self._fan_mode = None
        self._swing_mode = None
        self._preset_mode = None
        self._min_temperature = 7
        self._max_temperature = 35
        self._sw_version = None
        self._available = True
        self._status_data = None
        # config values, updated below
        self._config = None
        self._config_current_temperature = None
        self._config_mode = None
        self._config_fan_mode = None
        self._config_swing_mode = None
        self._config_preset_mode = None
        self._config_status = None
        self._config_target_temperature = None
        self._config_command_name = None
        self._get_status()

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}("
            f"{self._device_id!r}, "
            f"name={self._device_name!r}, "
            f"serial={self._device_serial!r}, "
            f"model={self.model_name!r}"
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
    def swing_mode(self) -> str:
        return self._swing_mode

    @property
    def preset_mode(self) -> str:
        return self._preset_mode

    @property
    def status(self) -> int:
        return self._status

    @property
    def available(self) -> bool:
        return self._available

    @available.setter
    def available(self, value: bool):
        self._available = bool(value)
        self.write_ha_state()

    def update(self) -> None:
        self._haier.auth()

    def write_ha_state(self) -> None:
        pass

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
            "available": self.available,
            "mode": self.mode,
            "status_data": self.status_data,
            "config": {
                "command_name": self._config_command_name,
                "status": self._config_status,
                "mode": self._config_mode,
                "current_temperature": self._config_current_temperature,
                "target_temperature": self._config_target_temperature,
                "fan_mode": self._config_fan_mode,
                "swing_mode": self._config_swing_mode,
                "preset_mode": self._config_preset_mode,
            }
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
        if key == self._config_current_temperature:  # Температура в комнате
            self._current_temperature = float(value)
        elif key == self._config_status:  # Включение/выключение
            self._status = int(value)
        elif key == self._config_target_temperature:  # Целевая температура
            self._target_temperature = float(value)
        elif key == self._config_mode:  # Режимы
            self._mode = self._config.get_value(self._config_mode, int(value))
        elif key == self._config_fan_mode:  # Скорость вентилятора
            self._fan_mode = self._config.get_value(self._config_fan_mode, int(value))
        elif key == self._config_swing_mode:
            self._swing_mode = self._config.get_value(self._config_swing_mode, int(value))
        elif key == self._config_preset_mode:
            self._preset_mode = self._config.get_value(self._config_preset_mode, int(value))

    def _set_config_attribute(self, attr: dict) -> None:
        desc = attr.get('description', None)
        code = attr.get('name', None)
        if desc in ('Режимы',) and self._config_mode is None:
            self._config_mode = code
        if desc in ('Целевая температура',) and self._config_target_temperature is None:
            self._config_target_temperature = code
        if desc in ('Температура в комнате',) and self._config_current_temperature is None:
            self._config_current_temperature = code
        if desc in ('Скорость вентилятора',) and self._config_fan_mode is None:
            self._config_fan_mode = code
        if desc in ('Включение/выключение',) and self._config_status is None:
            self._config_status = code

    def _load_config_from_attributes(self, attributes: list[dict]):
        self._load_config()
        for attr in attributes:
            command_name = attr.get("command_name")
            if command_name and self._config_command_name is None:
                self._config_command_name = command_name
            self._set_config_attribute(attr)
            code = attr.get('name', "")
            curr = attr.get('currentValue')
            if code == self._config_target_temperature:
                self._min_temperature = float(attr.get('range', {}).get('data', {}).get('minValue', 0))
                self._max_temperature = float(attr.get('range', {}).get('data', {}).get('maxValue', 0))
            self._set_attribute_value(code, curr)
        _LOGGER.info(
            f"The following values are used: "
            f"command_name={self._config_command_name},"
            f"current_temperature={self._config_current_temperature},"
            f"mode={self._config_mode},"
            f"fan_mode={self._config_fan_mode},"
            f"swing_mode={self._config_swing_mode},"
            f"preset_mode={self._config_preset_mode},"
            f"status={self._config_status},"
            f"target_temperature={self._config_target_temperature}"
        )

    def _load_config(self) -> None:
        self._config = yaml_helper.DeviceConfig(self.model_name)
        self._config_command_name = self._config.get_command_name()
        self._config_mode = self._config.get_id_by_name('mode')
        self._config_target_temperature = self._config.get_id_by_name('target_temperature')
        self._config_current_temperature = self._config.get_id_by_name('current_temperature')
        self._config_fan_mode = self._config.get_id_by_name('fan_mode')
        self._config_status = self._config.get_id_by_name('status')
        self._config_swing_mode = self._config.get_id_by_name('swing_mode')
        self._config_preset_mode = self._config.get_id_by_name('preset_mode')

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
            device_info = data.setdefault("info", {})
            device_model = device_info.setdefault("model", "AC")
            # consider first symbols only and ignore some others
            device_model = device_model.replace('-','').replace('/', '')[:11]
            _LOGGER.info(f"Device model {device_model}")
            self.model_name = device_model
            attributes = data.setdefault("attributes", [])
            # read config values
            self._load_config_from_attributes(attributes)
            if self._swing_mode is None:
                self._swing_mode = SWING_OFF
            if self._preset_mode is None:
                self._preset_mode = PRESET_NONE
            settings = data.get("settings", {})
            firmware = settings.get('firmware', {}).get('value', None)
            self._sw_version = firmware
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
        self._available = False if str(status).upper() == 'OFFLINE' else True
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
            "commandName": self._config_command_name,
            "commands": commands,
        }))

    def get_hvac_modes(self) -> list[HVACMode]:
        return [
            HVACMode(value)
            for value in
            self._config.get_mapping_values('mode')
        ] + [HVACMode.OFF]

    def get_supported_features(self) -> ClimateEntityFeature:
        value = (
            ClimateEntityFeature.TARGET_TEMPERATURE |
            ClimateEntityFeature.TURN_OFF |
            ClimateEntityFeature.TURN_ON |
            ClimateEntityFeature.FAN_MODE
        )
        if self._config_swing_mode is not None:
            value = value | ClimateEntityFeature.SWING_MODE
        if self._config_preset_mode is not None:
            value = value | ClimateEntityFeature.PRESET_MODE
        return ClimateEntityFeature(value)

    def get_fan_modes(self) -> list[str]:
        return self._config.get_mapping_values('fan_mode')

    def get_swing_modes(self) -> list[str]:
        return self._config.get_mapping_values('swing_mode')

    def get_preset_modes(self) -> list[str]:
        return self._config.get_mapping_values('preset_mode')

    def set_temperature(self, temp: float) -> None:
        self._send_commands([
            {
                "commandName": self._config_target_temperature,
                "value": str(temp)
            }
        ])
        self._target_temperature = temp

    def switch_on(self, hvac_mode: str = None) -> None:
        hvac_mode = hvac_mode or self._mode or HVACMode.AUTO
        mode_haier = self._config.get_haier_code(self._config_mode, hvac_mode)
        self._send_commands([
            {
                "commandName": self._config_status,
                "value": "1"
            },
            {
                "commandName": self._config_mode,
                "value": str(mode_haier)
            }
        ])
        self._status = 1
        self._mode = hvac_mode

    def switch_off(self) -> None:
        self._send_commands([
            {
                "commandName": self._config_status,
                "value": "0"
            }
        ])
        self._status = 0

    def set_fan_mode(self, fan_mode: str) -> None:
        mode_haier = self._config.get_haier_code(self._config_fan_mode, fan_mode)
        self._send_commands([
            {
                "commandName": self._config_fan_mode,
                "value": str(mode_haier)
            }
        ])
        self._fan_mode = fan_mode

    def set_swing_mode(self, swing_mode: str) -> None:
        mode_haier = self._config.get_haier_code(self._config_swing_mode, swing_mode)
        self._send_commands([
            {
                "commandName": self._config_swing_mode,
                "value": str(mode_haier)
            }
        ])
        self._swing_mode = swing_mode

    def set_preset_mode(self, preset_mode: str) -> None:
        mode_haier = self._config.get_haier_code(self._config_preset_mode, preset_mode)
        self._send_commands([
            {
                "commandName": self._config_preset_mode,
                "value": str(mode_haier)
            }
        ])
        self._preset_mode = preset_mode
