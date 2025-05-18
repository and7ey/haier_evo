from __future__ import annotations
import inspect
import requests
import json
import time
import threading
import uuid
import socket
from enum import Enum
from datetime import datetime, timezone, timedelta
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from ratelimit import limits, sleep_and_retry
from websocket import WebSocketConnectionClosedException, WebSocketException, WebSocketApp, WebSocket
from requests.exceptions import ConnectionError, Timeout, HTTPError
from urllib.parse import urlparse, urljoin, parse_qs
from urllib3.exceptions import NewConnectionError
from homeassistant.core import HomeAssistant
from homeassistant import exceptions
from homeassistant.components.climate.const import ClimateEntityFeature, HVACMode, SWING_OFF, PRESET_NONE
from .logger import _LOGGER
from . import yaml_helper
from . import const as C # noqa


class InvalidAuth(exceptions.HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidDevicesList(exceptions.HomeAssistantError):
    """Error to indicate we cannot connect."""


class SocketStatus(Enum):
    PRE_INITIALIZATION = 0
    INITIALIZING = 1
    INITIALIZED = 2
    NOT_INITIALIZED = 3


class Haier(object):

    def __init__(self, hass: HomeAssistant, email: str, password: str) -> None:
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

    @property
    def token(self) -> str | None:
        return self._token

    @property
    def load_tokens(self):
        return self._load_tokens

    def _load_tokens(self) -> None:
        try:
            filename = self.hass.config.path(C.DOMAIN)
            with open(filename, "r") as f:
                data = json.load(f)
            assert isinstance(data, dict), "Bad saved tokens file"
            self._token = data.get("token", None)
            tokenexpire = data.get("tokenexpire")
            self._tokenexpire = datetime.fromisoformat(tokenexpire) if tokenexpire else None
            self._refreshtoken = data.get("refreshtoken", None)
            refreshexpire = data.get("refreshexpire")
            self._refreshexpire = datetime.fromisoformat(refreshexpire) if refreshexpire else None
        except Exception as e:
            _LOGGER.error(f"Failed to load tokens file: {e}")
        else:
            _LOGGER.info(f"Loaded tokens file: {filename}")

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

    def stop(self) -> None:
        self._disconnect_requested = True
        self._socket_app.close()

    @sleep_and_retry
    @limits(calls=C.CALLS, period=C.RATE_LIMIT)
    def make_request(self, method: str, url: str, **kwargs) -> requests.Response:
        try:
            # Setting a default timeout for requests
            kwargs.setdefault('timeout', C.API_TIMEOUT)
            headers = kwargs.setdefault('headers', {})
            headers.setdefault('User-Agent', "curl/7.81.0")
            headers.setdefault('Accept', "*/*")
            resp = requests.request(method, url, **kwargs)
            # Handling 429 Too Many Requests with retry
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", "5"))
                _LOGGER.info(f"Rate limited. Retrying after {retry_after} seconds.")
                time.sleep(retry_after)
                raise HTTPError("429 Too Many Requests")
            # Raise for other HTTP errors
            resp.raise_for_status()
            return resp
        except (ConnectionError, NewConnectionError, socket.gaierror) as e:
            _LOGGER.error(f"Network error occurred: {e}. Retrying...")
            raise e  # Re-raise to allow retry mechanisms to handle this
        except Timeout as e:
            _LOGGER.error(f"Request timed out: {e}. Retrying...")
            raise e
        except HTTPError as e:
            _LOGGER.error(f"HTTP error occurred: {e}. Retrying...")
            raise e

    @retry(
        retry=retry_if_exception_type(requests.exceptions.HTTPError),
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=4, max=10)
    )
    def login(self, refresh: bool = False) -> None:
        if refresh and self._refreshtoken: # token refresh
            refresh_path = urljoin(C.API_PATH, C.API_TOKEN_REFRESH)
            _LOGGER.info(f"Refreshing token in to {refresh_path} with email {self._email}")
            resp = self.make_request('POST', refresh_path, data={'refreshToken': self._refreshtoken})
            _LOGGER.info(f"Refresh ({self._email}) status code: {resp.status_code}")
        else:  # initial login
            login_path = urljoin(C.API_PATH, C.API_LOGIN)
            _LOGGER.info(f"Logging in to {login_path} with email {self._email}")
            resp = self.make_request('POST', login_path, data={'email': self._email, 'password': self._password})
            _LOGGER.info(f"Login ({self._email}) status code: {resp.status_code}")
        try:
            assert resp, "No response from login"
            assert resp.status_code == 200, f"Status code is not 200 {resp.status_code}"
            assert "application/json" in resp.headers.get("content-type"), f"Bad content type"
            data = resp.json()
            _LOGGER.debug(f"{data}")
            assert "data" in data, f"Bad json, data not found"
            error = data.get("error")
            if error is not None:
                self._clear_tokens()
                raise AssertionError(f"Error {error}")
            data = data["data"]
            assert isinstance(data, dict), f"Data is not dict: {data}"
            assert "token" in data, f"Bad data, token not found"
            token = data["token"]
            assert isinstance(token, dict), f"Token is not dict: {token}"
            assert "accessToken" in token, f"Bad token data, accessToken not found"
            assert "refreshToken" in token, f"Bad token data, refreshToken not found"
            self._token = token.get("accessToken")
            self._tokenexpire = datetime.strptime(token.get("expire"), "%Y-%m-%dT%H:%M:%S%z")
            self._refreshtoken = token.get("refreshToken")
            self._refreshexpire = datetime.strptime(token.get("refreshExpire"), "%Y-%m-%dT%H:%M:%S%z")
            _LOGGER.info(
                f"Successful refreshed token for email {self._email}"
                if refresh else
                f"Successful login for email {self._email}"
            )
            self._save_tokens()
        except Exception as e:
            _LOGGER.error(
                f"Failed to login/refresh token for email {self._email}, "
                f"response was: {resp}, "
                f"err: {e}"
            )
            raise InvalidAuth()

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

    def pull_data(self) -> None:
        self.auth()
        devices_path = urljoin(C.API_PATH, C.API_DEVICES)
        _LOGGER.info(f"Getting devices, url: {devices_path}")
        resp = requests.get(devices_path, headers={
            'X-Auth-Token': self._token,
            'User-Agent': 'evo-mobile',
            'Device-Id': str(uuid.uuid4()),
            'Content-Type': 'application/json'
        }, timeout=C.API_TIMEOUT)
        if (
            resp.status_code == 200
            and "application/json" in resp.headers.get("content-type")
            and resp.json().get("data", {}).get("presentation", {}).get("layout", {}).get('scrollContainer', [])
        ):
            _LOGGER.debug(resp.text)
            data = resp.json().get("data", {})
            containers = data.get("presentation", {}).get("layout", {}).get('scrollContainer', [])
            for item in containers:
                component_id = item.get("trackingData", {}).get("component", {}).get("componentId", "")
                _LOGGER.debug(component_id)
                if (
                    item.get("contractName", "") == "deviceList"
                    and component_id == "72a6d224-cb66-4e6d-b427-2e4609252684"
                ): # check for smart devices only
                    state_data = item.get("state", {})
                    state_json = json.loads(state_data)
                    devices = state_json.get('items', [{}])
                    for d in devices:
                        # haierevo://device?deviceId=12:34:56:78:90:68&type=AC&serialNum=AAC0M1E0000000000000&uitype=AC_BASE
                        device_title = d.get('title', '') # only one device is supported
                        device_link = d.get('action', {}).get('link', '')
                        parsed_link = urlparse(device_link)
                        query_params = parse_qs(parsed_link.query)
                        device_mac = query_params.get('deviceId', [''])[0]
                        device_mac = device_mac.replace('%3A', ':')
                        device_serial = query_params.get('serialNum', [''])[0]
                        _LOGGER.info(
                            f"Received device successfully, "
                            f"device title {device_title}, "
                            f"device mac {device_mac}, "
                            f"device serial {device_serial}"
                        )
                        self.devices.append(HaierAC(
                            haier=self,
                            device_mac=device_mac,
                            device_serial=device_serial,
                            device_title=device_title
                        ))
                    break
            if len(self.devices) > 0:
                self.connect_in_thread()
        else:
            _LOGGER.error(f"Failed to get devices, response was: {resp}")
            raise InvalidDevicesList()

    def get_device_by_id(self, id_: str) -> HaierAC | None:
        return next(filter(lambda d: d.device_id == id_, self.devices), None)

    def _init_ws(self) -> None:
        self.auth()
        self._socket_app = WebSocketApp(
            url=urljoin(C.API_WS_PATH, self.token),
            on_message=self._on_message,
            on_open=self._on_open,
            on_ping=self._on_ping,
            on_close=self._on_close,
        )

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
        self._socket_status = SocketStatus.INITIALIZED

    # noinspection PyUnusedLocal
    def _on_ping(self, ws: WebSocket) -> None:
        self._socket_app.sock.pong()

    # noinspection PyUnusedLocal
    def _on_close(self, ws: WebSocket, close_code: int, close_message: str) -> None:
        _LOGGER.debug(f"Websocket closed. Code: {close_code}, message: {close_message}")
        self._auto_reconnect_if_needed()

    def _auto_reconnect_if_needed(self, command: str = None) -> None:
        self._socket_status = SocketStatus.NOT_INITIALIZED
        if not self._disconnect_requested:
            _LOGGER.debug(f"Automatically reconnecting on unwanted closed socket. {command}")
            self.connect_in_thread()
        else:
            _LOGGER.debug("Disconnect was explicitly requested, not attempting to reconnect")

    def connect(self) -> None:
        if self._socket_status not in [
            SocketStatus.INITIALIZED,
            SocketStatus.INITIALIZING,
        ]:
            self._socket_status = SocketStatus.INITIALIZING
            _LOGGER.debug(f"Connecting to websocket ({C.API_WS_PATH})")
            try:
                self._init_ws()
                self._socket_app.run_forever()
            except WebSocketException: # socket is already opened
                pass
            except Exception:
                self._auto_reconnect_if_needed()
        else:
            _LOGGER.info(
                f"Can not attempt socket connection because of current "
                f"socket status: {self._socket_status}"
            )

    def connect_in_thread(self) -> None:
        thread = threading.Thread(target=self.connect)
        thread.daemon = True
        thread.start()

    def send_message(self, payload: str) -> None:
        calling_method = inspect.stack()[1].function
        _LOGGER.debug(
            f"Sending message for command {calling_method}: "
            f"{payload}"
        )
        try:
            self._socket_app.send(payload)
        except WebSocketConnectionClosedException:
            self._auto_reconnect_if_needed()


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

    def update(self) -> None:
        self._haier.auth()

    def write_ha_state(self) -> None:
        pass

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

    def _set_attribute(self, key: str, value) -> None:
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

    def _get_status(self) -> None:
        status_url = C.API_STATUS.replace("{mac}", self.device_id)
        _LOGGER.info(f"Getting initial status of device {self.device_id}, url: {status_url}")
        resp = requests.get(status_url, headers={"X-Auth-token": self._haier.token}, timeout=C.API_TIMEOUT)
        if (
            resp.status_code == 200
            and resp.json().get("attributes", {})
        ):
            _LOGGER.info(f"Update device {self.device_id} status code: {resp.status_code}")
            _LOGGER.debug(resp.text)
            device_info = resp.json().get("info", {})
            device_model = device_info.get("model", "AC")
            # consider first symbols only and ignore some others
            device_model = device_model.replace('-','').replace('/', '')[:11]
            _LOGGER.info(f"Device model {device_model}")
            self.model_name = device_model
            # read config values
            self._config = yaml_helper.DeviceConfig(device_model)
            self._config_current_temperature = self._config.get_id_by_name('current_temperature')
            self._config_mode = self._config.get_id_by_name('mode')
            self._config_fan_mode = self._config.get_id_by_name('fan_mode')
            self._config_swing_mode = self._config.get_id_by_name('swing_mode')
            self._config_preset_mode = self._config.get_id_by_name('preset_mode')
            self._config_status = self._config.get_id_by_name('status')
            self._config_target_temperature = self._config.get_id_by_name('target_temperature')
            self._config_command_name = self._config.get_command_name()
            _LOGGER.info(
                f"The following values are used: "
                f"current temp - {self._config_current_temperature}, "
                f"mode - {self._config_mode}, "
                f"fan speed - {self._config_fan_mode}, "
                f"swing - {self._config_swing_mode}, "
                f"preset - {self._config_preset_mode}, "
                f"status - {self._config_status}, "
                f"target temp - {self._config_target_temperature}"
            )
            attributes = resp.json().get("attributes", {})
            for attr in attributes:
                key = attr.get('name', '')
                value = attr.get('currentValue')
                if key == self._config_target_temperature:
                    self._min_temperature = float(attr.get('range', {}).get('data', {}).get('minValue', 0))
                    self._max_temperature = float(attr.get('range', {}).get('data', {}).get('maxValue', 0))
                self._set_attribute(key, value)
            if self._swing_mode is None:
                self._swing_mode = SWING_OFF
            if self._preset_mode is None:
                self._preset_mode = PRESET_NONE
            settings = resp.json().get("settings", {})
            firmware = settings.get('firmware', {}).get('value', None)
            self._sw_version = firmware
        self.write_ha_state()

    def _handle_status_update(self, received_message: dict) -> None:
        message_statuses = received_message.get("payload", {}).get("statuses", [{}])
        _LOGGER.info(f"Received status update {self.device_id} {received_message}")
        for key, value in message_statuses[0]['properties'].items():
            self._set_attribute(key, value)
        self.write_ha_state()

    def _handle_device_status_update(self, received_message: dict) -> None:
        _LOGGER.info(f"Received status update {self.device_id} {received_message}")
        self.write_ha_state()

    def _handle_info(self, received_message: dict) -> None:
        payload = received_message.get("payload", {})
        self._sw_version = payload.get("swVersion") or self._sw_version

    def _send_message(self, message: str) -> None:
        self._haier.send_message(message)

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

    def set_temperature(self, temp) -> None:
        self._send_message(json.dumps({
            "action": "operation",
            "macAddress": self.device_id,
            "commandName": self._config_command_name,
            "commands": [
                {
                    "commandName": self._config_target_temperature,
                    "value": str(temp)
                }
            ]
        }))
        self._target_temperature = temp

    def switch_on(self, hvac_mode: str = None) -> None:
        hvac_mode = hvac_mode or self._mode or "auto"
        mode_haier = self._config.get_haier_code(self._config_mode, hvac_mode)
        self._send_message(json.dumps({
            "action": "operation",
            "macAddress": self.device_id,
            "commandName": self._config_command_name,
            "commands": [
                {
                    "commandName": self._config_status,
                    "value": "1"
                },
                {
                    "commandName": self._config_mode,
                    "value": str(mode_haier)
                }
            ]
        }))
        self._status = 1
        self._mode = hvac_mode

    def switch_off(self) -> None:
        self._send_message(json.dumps({
            "action": "operation",
            "macAddress": self.device_id,
            "commandName": self._config_command_name,
            "commands": [
                {
                    "commandName": self._config_status,
                    "value": "0"
                }
            ]
        }))
        self._status = 0

    def set_fan_mode(self, fan_mode) -> None:
        mode_haier = self._config.get_haier_code(self._config_fan_mode, fan_mode)
        self._send_message(json.dumps({
            "action": "operation",
            "macAddress": self.device_id,
            "commandName": self._config_command_name,
            "commands": [
                {
                    "commandName": self._config_fan_mode,
                    "value": str(mode_haier)
                }
            ]
        }))
        self._fan_mode = fan_mode

    def set_swing_mode(self, swing_mode) -> None:
        mode_haier = self._config.get_haier_code(self._config_swing_mode, swing_mode)
        self._send_message(json.dumps({
            "action": "operation",
            "macAddress": self.device_id,
            "commandName": self._config_command_name,
            "commands": [
                {
                    "commandName": self._config_swing_mode,
                    "value": str(mode_haier)
                }
            ]
        }))
        self._swing_mode = swing_mode

    def set_preset_mode(self, preset_mode) -> None:
        mode_haier = self._config.get_haier_code(self._config_preset_mode, preset_mode)
        self._send_message(json.dumps({
            "action": "operation",
            "macAddress": self.device_id,
            "commandName": self._config_command_name,
            "commands": [
                {
                    "commandName": self._config_preset_mode,
                    "value": str(mode_haier)
                }
            ]
        }))
        self._preset_mode = preset_mode
