import inspect
import requests
import json
import logging
import time
import threading
import uuid
import socket
from enum import Enum
from datetime import datetime, timezone
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from ratelimit import limits, sleep_and_retry
from websocket import WebSocketConnectionClosedException, WebSocketException, WebSocketApp, WebSocket
from requests.exceptions import ConnectionError, Timeout, HTTPError
from urllib.parse import urlparse, urljoin, parse_qs
from urllib3.exceptions import NewConnectionError
from homeassistant.core import HomeAssistant
from homeassistant import exceptions
from . import yaml_helper
from .const import DOMAIN


CALLS = 5
RATE_LIMIT = 60
API_TIMEOUT = 15
API_PATH = "https://evo.haieronline.ru"
API_LOGIN = "v1/users/auth/sign-in"
API_TOKEN_REFRESH = "v1/users/auth/refresh"
API_DEVICES = "v2/ru/pages/sduiRawPaginated/smartHome?part=1&partitionWeight=6"
API_STATUS = "https://iot-platform.evo.haieronline.ru/mobile-backend-service/api/v1/config/{mac}?type=DETAILED"
API_WS_PATH = "wss://iot-platform.evo.haieronline.ru/gateway-ws-service/ws/"
_LOGGER = logging.getLogger(__name__)
_LOGGER.setLevel(logging.INFO)


class Haier(object):

    def __init__(self, hass: HomeAssistant, email: str, password: str) -> None:
        self.hass: HomeAssistant = hass
        self.devices: list[HaierAC] = []
        self._email: str = email
        self._password: str = password
        self._subscription = HaierSubscription(self)
        self._lock = threading.Lock()
        self._token: str | None = None
        self._tokenexpire: datetime | None = None
        self._refreshtoken: str | None = None
        self._refreshexpire: datetime | None = None
        self._load_tokens()

    @property
    def token(self) -> str | None:
        return self._token

    @property
    def subscription(self) -> "HaierSubscription":
        return self._subscription

    def _load_tokens(self) -> None:
        try:
            filename = self.hass.config.path(DOMAIN)
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

    def _save_tokens(self) -> None:
        try:
            filename = self.hass.config.path(DOMAIN)
            with open(filename, "w") as f:
                json.dump({
                    "token": self._token,
                    "tokenexpire": str(self._tokenexpire) if self._tokenexpire else None,
                    "refreshtoken": self._refreshtoken,
                    "refreshexpire": str(self._refreshexpire) if self._refreshexpire else None,
                }, f)
        except Exception as e:
            _LOGGER.error(f"Failed to save tokens file: {e}")

    @sleep_and_retry
    @limits(calls=CALLS, period=RATE_LIMIT)
    def make_request(self, method: str, url: str, **kwargs) -> requests.Response:
        try:
            # Setting a default timeout for requests
            kwargs.setdefault('timeout', API_TIMEOUT)
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
        if not refresh: # initial login
            login_path = urljoin(API_PATH, API_LOGIN)
            _LOGGER.info(f"Logging in to {login_path} with email {self._email}")
            resp = self.make_request('POST', login_path, data={'email': self._email, 'password': self._password})
            _LOGGER.info(f"Login ({self._email}) status code: {resp.status_code}")
        else: # token refresh
            refresh_path = urljoin(API_PATH, API_TOKEN_REFRESH)
            _LOGGER.info(f"Refreshing token in to {refresh_path} with email {self._email}")
            resp = self.make_request('POST', refresh_path, data={'refreshToken': self._refreshtoken})
            _LOGGER.info(f"Refresh ({self._email}) status code: {resp.status_code}")
        try:
            assert resp, "No response from login"
            data = resp.json()
            _LOGGER.debug(f"{data}")
            assert resp.status_code == 200, f"Status code is not 200 {resp.status_code}"
            assert "application/json" in resp.headers.get("content-type"), f"Bad content type"
            assert "data" in data, f"Bad json"
            data = data["data"]
            assert "token" in data, f"Bad data"
            token = data["token"]
            assert "accessToken" in token, f"Bad token data"
            assert "refreshToken" in token, f"Bad token data"
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
            _LOGGER.error(f"Failed to login/refresh token for email {self._email}, response was: {resp}, err: {e}")
            raise InvalidAuth()

    def auth(self) -> None:
        with self._lock:
            tzinfo = datetime.now(timezone.utc).astimezone().tzinfo
            now = datetime.now(tzinfo)
            tokenexpire = self._tokenexpire or now
            refreshexpire = self._refreshexpire or now
            if self._token:
                if tokenexpire > now:
                    return None
                elif self._refreshtoken and refreshexpire <= now:
                    _LOGGER.info(f"RefreshToken expired")
                    return self.login()
                elif self._refreshtoken:
                    _LOGGER.info(f"Token to be refreshed")
                    return self.login(refresh=True)
                else:
                    return self.login()
            else:
                _LOGGER.info(f"Token expired or empty")
                return self.login()

    def pull_data(self) -> None:
        self.auth()
        devices_path = urljoin(API_PATH, API_DEVICES)
        _LOGGER.info(f"Getting devices, url: {devices_path}")
        devices_headers = {
            'X-Auth-Token': self._token,
            'User-Agent': 'evo-mobile',
            'Device-Id': str(uuid.uuid4()),
            'Content-Type': 'application/json'
        }
        resp = requests.get(devices_path, headers=devices_headers, timeout=API_TIMEOUT)
        if (
            resp.status_code == 200
            and "application/json" in resp.headers.get("content-type")
            and resp.json().get("data", {}).get("presentation", {}).get("layout", {}).get('scrollContainer', [])
        ):
            _LOGGER.debug(resp.text)
            data = resp.json().get("data", {})
            containers = data.get("presentation", {}).get("layout", {}).get('scrollContainer', [])
            self._subscription = HaierSubscription(self)
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
                        device = HaierAC(device_mac, device_serial, device_title, self)
                        self._subscription.add_device(device)
                        self.devices.append(device)
                    break
            if len(self.devices) > 0:
                self._subscription.connect_in_thread()
        else:
            _LOGGER.error(f"Failed to get devices, response was: {resp}")
            raise InvalidDevicesList()


class InvalidAuth(exceptions.HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidDevicesList(exceptions.HomeAssistantError):
    """Error to indicate we cannot connect."""


class SocketStatus(Enum):
    PRE_INITIALIZATION = 0
    INITIALIZING = 1
    INITIALIZED = 2
    NOT_INITIALIZED = 3


class HaierSubscription(object):

    def __init__(self, haier: Haier):
        self._haier: Haier = haier
        self._devices: dict = {}
        self._disconnect_requested = False
        self._socket_status: SocketStatus = SocketStatus.PRE_INITIALIZATION

    def add_device(self, device: "HaierAC") -> None:
        self._devices[device.get_device_id] = device

    def _init_ws(self) -> None:
        self._haier.auth()
        self._socket_app = WebSocketApp(
            urljoin(API_WS_PATH, self._haier.token),
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
        device = self._devices.get(message_device)
        if device is None:
            _LOGGER.error(f"Got a message for a device we don't know about: {message_device}")
        device.on_message(message_dict)

    # noinspection PyMethodMayBeStatic,PyUnusedLocal
    def _on_open(self, ws: WebSocket) -> None:
        _LOGGER.debug("Websocket opened")

    # noinspection PyUnusedLocal
    def _on_ping(self, ws: WebSocket) -> None:
        self._socket_app.sock.pong()

    # noinspection PyUnusedLocal
    def _on_close(self, ws: WebSocket, close_code: int, close_message: str) -> None:
        _LOGGER.debug(f"Socket closed. Code: {close_code}, message: {close_message}")
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
            _LOGGER.info(f"Connecting to websocket ({API_WS_PATH})")
            self._init_ws()
            try:
                self._socket_app.run_forever()
            except WebSocketException: # socket is already opened
                pass
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

    def __init__(self, device_mac: str, device_serial: str, device_title: str, haier: Haier):
        self._id = device_mac
        self._device_serial = device_serial
        self._device_name = device_title
        self._haier = haier
        # the following values are updated below
        self.model_name = "AC"
        self._current_temperature = 0
        self._target_temperature = 0
        self._status = None
        self._mode = None
        self._fan_mode = None
        self._min_temperature = 7
        self._max_temperature = 35
        self._sw_version = None
        # config values, updated below
        self._config = None
        self._config_current_temperature = None
        self._config_mode = None
        self._config_fan_mode = None
        self._config_status = None
        self._config_target_temperature = None
        self._config_command_name = None
        self._get_status()

    def _get_status(self):
        status_url = API_STATUS.replace("{mac}", self._id)
        _LOGGER.info(f"Getting initial status of device {self._id}, url: {status_url}")
        resp = requests.get(status_url, headers={"X-Auth-token": self._haier.token}, timeout=API_TIMEOUT)
        if (
            resp.status_code == 200
            and resp.json().get("attributes", {})
        ):
            _LOGGER.info(f"Update device {self._id} status code: {resp.status_code}")
            _LOGGER.debug(resp.text)
            device_info = resp.json().get("info", {})
            device_model = device_info.get("model", "AC")
            # consider first symbols only and ignore some others
            device_model = device_model.replace('-','').replace('/', '')[:11]
            _LOGGER.info(f"Device model {device_model}")
            self.model_name = device_model
            self._config = yaml_helper.DeviceConfig(device_model)
            # read config values
            self._config_current_temperature = self._config.get_id_by_name('current_temperature')
            self._config_mode = self._config.get_id_by_name('mode')
            self._config_fan_mode = self._config.get_id_by_name('fan_mode')
            self._config_status = self._config.get_id_by_name('status')
            self._config_target_temperature = self._config.get_id_by_name('target_temperature')
            self._config_command_name = self._config.get_command_name()
            _LOGGER.info(
                f"The following values are used: current temp - {self._config_current_temperature}, "
                f"mode - {self._config_mode}, "
                f"fan speed - {self._config_fan_mode}, "
                f"status - {self._config_status}, "
                f"target temp - {self._config_target_temperature}"
            )
            attributes = resp.json().get("attributes", {})
            for attr in attributes:
                if attr.get('name', '') == self._config_current_temperature: # Температура в комнате
                    self._current_temperature = float(attr.get('currentValue'))
                elif attr.get('name', '') == self._config_mode: # Режимы
                    self._mode = self._config.get_value_from_mappings(self._config_mode, int(attr.get('currentValue')))
                elif attr.get('name', '') == self._config_fan_mode: # Скорость вентилятора
                    self._fan_mode = self._config.get_value_from_mappings(self._config_fan_mode, int(attr.get('currentValue')))
                elif attr.get('name', '') == self._config_status: # Включение/выключение
                    self._status = int(attr.get('currentValue'))
                elif attr.get('name', '') == self._config_target_temperature: # Целевая температура
                    self._target_temperature = float(attr.get('currentValue'))
                    self._min_temperature = float(attr.get('range', {}).get('data', {}).get('minValue', 0))
                    self._max_temperature = float(attr.get('range', {}).get('data', {}).get('maxValue', 0))
            settings = resp.json().get("settings", {})
            firmware = settings.get('firmware', {}).get('value', None)
            self._sw_version = firmware

    @property
    def _hass(self):
        return self._haier.hass

    @property
    def _subscription(self):
        return self._haier.subscription

    def update(self) -> None:
        self._haier.auth()

    def on_message(self, message_dict: dict) -> None:
        message_type = message_dict.get("event", "")
        if message_type == "status":
            self._handle_status_update(message_dict)
        elif message_type == "command_response":
            pass
        elif message_type == "info":
            pass
        else:
            _LOGGER.warning(f"Got unknown message of type: {message_type}")

    def _handle_status_update(self, received_message: dict) -> None:
        message_statuses = received_message.get("payload", {}).get("statuses", [{}])
        message_id = message_statuses[0].get("ts", 0)
        _LOGGER.debug(f"Received status update, message_id {message_id}")
        for key, value in message_statuses[0]['properties'].items():
            if key == self._config_current_temperature: # Температура в комнате
                self._current_temperature = float(value)
            if key == self._config_mode: # Режимы
                self._mode = self._config.get_value_from_mappings(self._config_mode, int(value))
            if key == self._config_fan_mode: # Скорость вентилятора
                self._fan_mode = self._config.get_value_from_mappings(self._config_fan_mode, int(value))
            if key == self._config_status: # Включение/выключение
                self._status = int(value)
            if key == self._config_target_temperature: # Целевая температура
                self._target_temperature = float(value)

    @property
    def get_device_id(self) -> str:
        return self._id

    @property
    def get_device_name(self) -> str:
        return self._device_name

    @property
    def get_sw_version(self) -> str:
        return self._sw_version

    @property
    def get_max_temperature(self) -> float:
        return self._max_temperature

    @property
    def get_min_temperature(self) -> float:
        return self._min_temperature

    @property
    def get_current_temperature(self) -> float:
        return self._current_temperature

    @property
    def get_target_temperature(self) -> float:
        return self._target_temperature

    @property
    def get_mode(self) -> str:
        return self._mode

    @property
    def get_fan_mode(self) -> str:
        return self._fan_mode

    @property
    def get_status(self) -> int:
        return self._status

    def _send_message(self, message: str) -> None:
        self._subscription.send_message(message)

    # noinspection PyPep8Naming
    def setTemperature(self, temp) -> None:
        self._target_temperature = temp
        self._send_message(json.dumps({
            "action": "operation",
            "macAddress": self._id,
            "commandName": self._config_command_name,
            "commands": [
                {
                    "commandName": self._config_target_temperature,
                    "value": str(temp)
                }
            ]
        }))

    # noinspection PyPep8Naming
    def switchOn(self, hvac_mode="auto") -> None:
        hvac_mode_haier = self._config.get_haier_code_from_mappings(self._config_mode, hvac_mode)
        self._send_message(json.dumps({
            "action": "operation",
            "macAddress": self._id,
            "commandName": self._config_command_name,
            "commands": [
                {
                    "commandName": self._config_status,
                    "value": "1"
                },
                {
                    "commandName": self._config_mode,
                    "value": str(hvac_mode_haier)
                }
            ]
        }))
        self._status = 1
        self._mode = hvac_mode

    # noinspection PyPep8Naming
    def switchOff(self) -> None:
        self._send_message(json.dumps({
            "action": "operation",
            "macAddress": self._id,
            "commandName": self._config_command_name,
            "commands": [
                {
                    "commandName": self._config_status,
                    "value": "0"
                }
            ]
        }))
        self._status = 0

    # noinspection PyPep8Naming
    def setFanMode(self, fan_mode) -> None:
        fan_mode_haier = self._config.get_haier_code_from_mappings(self._config_fan_mode, fan_mode)
        self._fan_mode = fan_mode
        self._send_message(json.dumps({
            "action": "operation",
            "macAddress": self._id,
            "commandName": self._config_command_name,
            "commands": [
                {
                    "commandName": self._config_fan_mode,
                    "value": str(fan_mode_haier)
                }
            ]
        }))
