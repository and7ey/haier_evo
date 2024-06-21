import inspect
import requests
import json
import asyncio
import logging
import time
import threading
import uuid
import datetime
from homeassistant.core import HomeAssistant
from homeassistant import config_entries, exceptions
from urllib.parse import urlparse, urljoin, parse_qs
from . import yaml_helper

import websocket
from websocket._exceptions import WebSocketConnectionClosedException, WebSocketException
from enum import Enum



SST_CLOUD_API_URL = "https://api.sst-cloud.com/"
API_PATH = "https://evo.haieronline.ru"
API_LOGIN = "v1/users/auth/sign-in"
API_TOKEN_REFRESH = "v1/users/auth/refresh"
API_DEVICES = "v2/ru/pages/sduiRawPaginated/smartHome?part=1&partitionWeight=6"
API_STATUS = "https://iot-platform.evo.haieronline.ru/mobile-backend-service/api/v1/config/{mac}?type=DETAILED"

API_WS_PATH = "wss://iot-platform.evo.haieronline.ru/gateway-ws-service/ws/"

_LOGGER = logging.getLogger(__name__)

class Haier:

    def __init__(self, hass: HomeAssistant, email: str, password: str) -> None:
        self._email = email
        self._password = password
        self.devices = []
        self.hass:HomeAssistant = hass
        self._token = None
        self._refreshtoken = None
        self._tokenexpire = None
        self._refreshexpire = None
        self._subscription: HaierSubscription = None



    def login(self, refresh=False):
        if not refresh: # initial login
            login_path = urljoin(API_PATH, API_LOGIN)
            _LOGGER.debug(f"Logging in to {login_path} with email {self._email}")

            resp = requests.post(login_path, data={'email': self._email, 'password': self._password})
            _LOGGER.debug(f"Login ({self._email}) status code: {resp.status_code}")
        else: # token refresh
            refresh_path = urljoin(API_PATH, API_TOKEN_REFRESH)
            _LOGGER.debug(f"Refreshing token in to {refresh_path} with email {self._email}")

            resp = requests.post(refresh_path, data={'refreshToken': self._refreshtoken})
            _LOGGER.debug(f"Refresh ({self._email}) status code: {resp.status_code}")

        if resp: _LOGGER.debug(f"{resp.json()}")

        if (
            resp.status_code == 200
            and "application/json" in resp.headers.get("content-type")
            and "data" in resp.json()
            and "token" in resp.json().get("data")
            and "accessToken" in resp.json().get("data").get("token")
            and "refreshToken" in resp.json().get("data").get("token")
        ):

            token = resp.json().get("data").get("token")
            self._token = token.get("accessToken")
            self._refreshtoken = token.get("refreshToken")
            self._tokenexpire = datetime.datetime.strptime(token.get("expire"), "%Y-%m-%dT%H:%M:%S%z")
            self._refreshexpire = datetime.datetime.strptime(token.get("refreshExpire"), "%Y-%m-%dT%H:%M:%S%z")
            if refresh:
                _LOGGER.debug(f"Successful refreshed token for email {self._email}")
            else:
                _LOGGER.debug(f"Successful login for email {self._email}")
        else:
            _LOGGER.error(
                f"Failed to login/refresh token for email {self._email}, response was: {resp}"
            )
            raise InvalidAuth()

    def auth(self):
        timezone_offset = +3.0 # Moscow
        tzinfo = datetime.timezone(datetime.timedelta(hours=timezone_offset))
        now = datetime.datetime.now(tzinfo)
        # _LOGGER.debug(f"Tokens statuses: now {now}, token expire {self._tokenexpire}, refresh expire {self._refreshexpire}")
        if self._tokenexpire and self._tokenexpire > now:
            # _LOGGER.debug(f"Tokens are valid")
            pass # token is still valid
        elif self._refreshexpire and self._refreshexpire < now:
            _LOGGER.debug(f"Token to be refreshed")
            self.refresh()
        else:
            _LOGGER.debug(f"Refresh token expired or empty")
            self.login()


    def pull_data(self):
        self.auth()

        devices_path = urljoin(API_PATH, API_DEVICES)
        _LOGGER.debug(f"Getting devices, url: {devices_path}")
        devices_headers = {
            'X-Auth-Token': self._token,
            'User-Agent': 'evo-mobile',
            'Device-Id': str(uuid.uuid4()),
            'Content-Type': 'application/json'
        }
        resp = requests.get(devices_path, headers=devices_headers)
        if (
            resp.status_code == 200
            and "application/json" in resp.headers.get("content-type")
            and resp.json().get("data", {}).get("presentation", {}).get("layout", {}).get('scrollContainer', [])
        ):
            _LOGGER.debug(resp.text)
            containers = resp.json().get("data", {}).get("presentation", {}).get("layout", {}).get('scrollContainer', [])
            self._subscription = HaierSubscription(self)
            for item in containers:
                component_id = item.get("trackingData", {}).get("component", {}).get("componentId", "")
                _LOGGER.debug(component_id)
                if item.get("contractName", "") == "deviceList" and component_id == "72a6d224-cb66-4e6d-b427-2e4609252684": # check for smart devices only
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
                        _LOGGER.debug(f"Received device successfully, device title {device_title}, device mac {device_mac}, device serial {device_serial}")
                        device = HaierAC(device_mac, device_serial, device_title, self)
                        self._subscription.add_device(device_mac, device)
                        self.devices.append(device)
                    break
            if len(devices) > 0:
                self._subscription.connect_in_thread()

        else:
            _LOGGER.error(
                f"Failed to get devices, response was: {resp}"
            )
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

class HaierSubscription:

    def __init__(self, haier: Haier):
        self._haier: Haier = haier
        self._devices: dict = {}
        self._disconnect_requested = False
        self._socket_status: SocketStatus = SocketStatus.PRE_INITIALIZATION


    def add_device(self, mac:str, device) -> None:
        self._devices[device._id] = device

    def _init_ws(self) -> None:
        self._haier.auth()
        self._socket_app = websocket.WebSocketApp(
            urljoin(API_WS_PATH, self._haier._token),
            on_message=self._on_message,
            on_open=self._on_open,
            on_ping=self._on_ping,
            on_close=self._on_close,
        )

    def _on_message(self, ws: websocket.WebSocket, message: str) -> None:
        _LOGGER.debug(f"Received WSS message: {message}")

        message_dict: dict = json.loads(message)

        # {"event":"status","macAddress":"12:34:56:78:90:12","payload":{"statuses":[{"properties":{"22":"0","23":"0","24":"0","25":"0","26":"0","27":"0","28":"0","29":"30","31":"24","10":"0","11":"0","12":"0","13":"1","14":"0","15":"0","16":"0","17":"0","18":"0","19":"0","0":"28","1":"0","2":"18","3":"1","5":"1","6":"2","7":"0","8":"0","9":"0","20":"0","21":"0"},"ts":"1715780063611"}]}}

        message_device = message_dict.get("macAddress")

        device = self._devices.get(message_device)
        if device is None:
            _LOGGER.error(f"Got a message for a device we don't know about: {message_device}")

        device.on_message(message_dict)

    def _on_open(self, ws: websocket.WebSocket) -> None:
        _LOGGER.debug("Websocket opened")

    def _on_ping(self, ws: websocket.WebSocket) -> None:
        self._socket_app.sock.pong()

    def _on_close(self, ws: websocket.WebSocket, close_code: int, close_message: str):
        _LOGGER.debug(
            f"Socket closed. Code: {close_code}, message: {close_message}"
        )
        self._auto_reconnect_if_needed()


    def _auto_reconnect_if_needed(self, command: str = None):
        self._socket_status = SocketStatus.NOT_INITIALIZED
        if not self._disconnect_requested:
            _LOGGER.debug(
                f"Automatically reconnecting on unwanted closed socket. {command}"
            )
            self.connect_in_thread()
        else:
            _LOGGER.debug(
                "Disconnect was explicitly requested, not attempting to reconnect"
            )

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
            except WebSocketException: # websocket._exceptions.WebSocketException: socket is already opened
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


class HaierAC:
    def __init__(self, device_mac: str, device_serial: str, device_title: str, haier: Haier):
        self._haier = haier

        self._hass:HomeAssistant = haier.hass
        self._subscription = haier._subscription

        self._id = device_mac
        self._device_name = device_title

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


        status_url = API_STATUS.replace("{mac}", self._id)
        _LOGGER.info(f"Getting initial status of device {self._id}, url: {status_url}")
        resp = requests.get(
            status_url,
            headers={"X-Auth-token": self._haier._token}
            )
        if (
            resp.status_code == 200
            and resp.json().get("attributes", {})
        ):
            _LOGGER.debug(f"Update device {self._id} status code: {resp.status_code}")
            _LOGGER.debug(resp.text)
            device_info = resp.json().get("info", {})
            device_model = device_info.get("model", "AC")
            _LOGGER.debug(f"Device model {device_model}")
            self.model_name = device_model
            self._config = yaml_helper.DeviceConfig(device_model)

            # read config values
            self._config_current_temperature = self._config.get_id_by_name('current_temperature')
            self._config_mode = self._config.get_id_by_name('mode')
            self._config_fan_mode = self._config.get_id_by_name('fan_mode')
            self._config_status = self._config.get_id_by_name('status')
            self._config_target_temperature = self._config.get_id_by_name('target_temperature')
            self._config_command_name = self._config.get_command_name()
            _LOGGER.debug(f"The following values are used: current temp - {self._config_current_temperature}, mode - {self._config_mode}, fan speed - {self._config_fan_mode}, status - {self._config_status}, target temp - {self._config_target_temperature}")

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



    def update(self) -> None:
        self._haier.auth()



    def on_message(self, message_dict: dict) -> None:
        # {"event":"status","macAddress":"12:34:56:78:90:12","payload":{"statuses":[{"properties":{"22":"0","23":"0","24":"0","25":"0","26":"0","27":"0","28":"0","29":"30","31":"24","10":"0","11":"0","12":"0","13":"1","14":"0","15":"0","16":"0","17":"0","18":"0","19":"0","0":"28","1":"0","2":"18","3":"1","5":"1","6":"2","7":"0","8":"0","9":"0","20":"0","21":"0"},"ts":"1715780063611"}]}}

        message_type = message_dict.get("event", "")
        if message_type == "status":
            self._handle_status_update(message_dict)
        elif message_type == "command_response":
            pass
        elif message_type == "info":
            pass
        else:
            _LOGGER.debug(f"Got unknown message of type: {message_type}")



    def _handle_status_update(self, received_message: dict) -> None:
        # {
        #   "event": "status",
        #   "macAddress": "12:34:56:78:90:12",
        #   "payload": {
        #     "statuses": [
        #       {
        #         "properties": {
        #           "22": "0",
        #           "21": "0"
        #         },
        #         "ts": "1715780063611"
        #       }
        #     ]
        #   }
        # }
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
    def get_max_temperature(self) -> str:
        return self._max_temperature

    @property
    def get_min_temperature(self) -> str:
        return self._min_temperature

    @property
    def get_current_temperature(self) -> int:
        return self._current_temperature

    @property
    def get_target_temperature(self) -> int:
        return self._target_temperature

    @property
    def get_mode(self) -> str:
        return self._mode

    @property
    def get_fan_mode(self) -> str:
        return self._fan_mode

    @property
    def get_status(self) -> str:
        return self._status

    def _send_message(self, message: str) -> None:
        self._subscription.send_message(message)

    def setTemperature(self, temp) -> None:
        self._target_temperature = temp

        self._send_message(json.dumps(
            {
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

    def switchOn(self, hvac_mode="auto") -> None:
        hvac_mode_haier = self._config.get_haier_code_from_mappings(self._config_mode, hvac_mode)

        self._send_message(json.dumps(
            {
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

    def switchOff(self) -> None:
        self._send_message(json.dumps(
            {
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

    def setFanMode(self, fan_mode) -> None:
        fan_mode_haier = self._config.get_haier_code_from_mappings(self._config_fan_mode, fan_mode)

        self._fan_mode = fan_mode

        self._send_message(json.dumps(
            {
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
