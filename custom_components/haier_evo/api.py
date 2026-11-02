from __future__ import annotations
import requests
import asyncio
import json
import time
import threading
import uuid
import socket
import weakref
from aiohttp import web
from enum import Enum
from datetime import datetime, timezone, timedelta
from tenacity import retry, stop_after_attempt, retry_if_exception_type, wait_fixed
from websocket import WebSocketApp, WebSocket
from requests.exceptions import ConnectionError, Timeout, HTTPError
from urllib.parse import urlparse, urljoin, parse_qs
from urllib3.exceptions import NewConnectionError
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.components.climate.const import ClimateEntityFeature, HVACMode, SWING_OFF, PRESET_NONE
from homeassistant.components.http import HomeAssistantView
from .programs import active_program_details, build_catalogue, program_settings, value_valid as _laundry_value_valid
from .logger import _LOGGER
from .limits import ResettableLimits
from . import config as CFG # noqa
from . import const as C # noqa


class InvalidAuth(HomeAssistantError):
    """Error to indicate we cannot connect."""

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


class SocketStatus(Enum):
    PRE_INITIALIZATION = 0
    INITIALIZING = 1
    INITIALIZED = 2
    NOT_INITIALIZED = 3


class HaierAPI(HomeAssistantView):
    url = "/api/haier_evo"
    name = "/api:haier_evo"
    requires_auth = False

    def __init__(self) -> None:
        self.haier = None

    # noinspection PyUnusedLocal
    async def get(self, request):
        if not getattr(self.haier, "allow_http", False):
            return web.Response(text="404: Not found", status=404, content_type="text/plain")
        return self.json(self.haier.to_dict())

    async def post(self, request):
        if not getattr(self.haier, "allow_http_post", False):
            return web.Response(text="404: Not found", status=404, content_type="text/plain")
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

    http = HaierAPI()
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
        password: str,
        region: str,
        http: bool = C.API_HTTP_ROUTE
    ) -> None:
        self._lock = threading.Lock()
        self._laundry_poll_lock = asyncio.Lock()
        self._laundry_retry_until = 0.0
        self._laundry_next_request = 0.0
        self._pull_data = None
        self._device_id = str(uuid.uuid4())
        self.hass: HomeAssistant = hass
        self.devices: list[HaierDevice] = []
        self.email: str = email
        self.password: str = password
        self.region: str = region
        self.allow_http: bool = http
        self.allow_http_post: bool = False
        self.token: str | None = None
        self.tokenexpire: datetime | None = None
        self.refreshtoken: str | None = None
        self.refreshexpire: datetime | None = None
        self.socket_app: WebSocketApp | None = None
        self.disconnect_requested = False
        self.socket_status: SocketStatus = SocketStatus.PRE_INITIALIZATION
        self.socket_thread = None
        self.reset_limits()
        self.register_view()

    def to_dict(self) -> dict:
        return {
            "socket_status": getattr(self.socket_status, "value", None),
            "backend_data": self._pull_data,
            "devices": [device.to_dict() for device in self.devices]
        }

    def load_tokens(self) -> None:
        filename = self.hass.config.path(C.DOMAIN)
        try:
            with open(filename, "r") as f:
                data = json.load(f)
            assert isinstance(data, dict), "Bad saved tokens file"
            self.token = data.get("token", None)
            tokenexpire = data.get("tokenexpire")
            self.tokenexpire = datetime.fromisoformat(tokenexpire) if tokenexpire else None
            self.refreshtoken = data.get("refreshtoken", None)
            refreshexpire = data.get("refreshexpire")
            self.refreshexpire = datetime.fromisoformat(refreshexpire) if refreshexpire else None
            _LOGGER.info(f"Loaded tokens file: {filename}")
        except FileNotFoundError:
            _LOGGER.warning(f"No tokens file: {filename}")
        except Exception as e:
            _LOGGER.error(f"Failed to load tokens file: {e}")

    def save_tokens(self) -> None:
        try:
            filename = self.hass.config.path(C.DOMAIN)
            with open(filename, "w") as f:
                json.dump({
                    "token": self.token,
                    "tokenexpire": str(self.tokenexpire) if self.tokenexpire else None,
                    "refreshtoken": self.refreshtoken,
                    "refreshexpire": str(self.refreshexpire) if self.refreshexpire else None,
                }, f)
        except Exception as e:
            _LOGGER.error(f"Failed to save tokens file: {e}")
        else:
            _LOGGER.debug(f"Saved tokens file: {filename}")

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

    def get_http_resources(self) -> list:
        http = getattr(self.hass, "http", None)
        app = getattr(http, "app", None)
        router = getattr(app, "router", None)
        resources = getattr(router, "resources", None)
        return resources() if resources else []

    def register_view(self) -> None:
        if self.http.url not in (r.canonical for r in self.get_http_resources()):
            self.hass.http.register_view(self.http)
        self.http.haier = weakref.proxy(self)

    def unregister_view(self) -> None:
        self.http.haier = None

    def stop(self) -> None:
        self.disconnect_requested = True
        self.reset_limits()
        if self.socket_app is not None:
            self.socket_app.close()
        self.unregister_view()

    @common_limits.sleep_and_retry
    @common_limits
    def make_request(self, method: str, url: str, **kwargs) -> requests.Response:
        try:
            assert self.disconnect_requested is False, 'Service already stoped'
            # Setting a default timeout for requests
            kwargs.setdefault('timeout', C.API_TIMEOUT)
            headers = kwargs.setdefault('headers', {})
            headers.setdefault('User-Agent', "evo-mobile")
            headers.setdefault('Platform', "android")
            headers.setdefault('Accept', "*/*")
            resp = requests.request(method, url, **kwargs)
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

    @auth_login_limits.sleep_and_retry
    @auth_login_limits
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

    @auth_refresh_limits.sleep_and_retry
    @auth_refresh_limits
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

    @retry(
        retry=retry_if_exception_type(AuthValidationError),
        stop=stop_after_attempt(2),
    )
    def login(self, refresh: bool = False) -> None:
        resp = None
        try:
            if refresh and self.refreshtoken:  # token refresh
                resp = self.auth_refresh()
            else:  # initial login
                resp = self.auth_login()
            assert resp, "No response from login"
            self.token = resp.access_token
            self.tokenexpire = resp.access_expire
            self.refreshtoken = resp.refresh_token
            self.refreshexpire = resp.refresh_expire
            self.save_tokens()
        except AuthValidationError as e:
            raise e
        except AssertionError as e:
            _LOGGER.error(f"Assertion error: {e}")
        except Exception as e:
            _LOGGER.error(
                f"Failed to login/refresh token, "
                f"response was: {resp}, "
                f"err: {e}"
            )
            raise InvalidAuth()
        else:
            _LOGGER.debug(f"Successful update tokens")

    def auth(self) -> None:
        with self._lock:
            tzinfo = timezone(timedelta(hours=+3.0))
            # tzinfo = datetime.now(timezone.utc).astimezone().tzinfo
            now = datetime.now(tzinfo)
            tokenexpire = self.tokenexpire or now
            refreshexpire = self.refreshexpire or now
            if self.token:
                if tokenexpire > now:
                    return None
                elif self.refreshtoken and refreshexpire > now:
                    # _LOGGER.debug(f"Token to be refreshed")
                    return self.login(refresh=True)
            # _LOGGER.debug(f"Token expired or empty")
            return self.login()

    def pull_data_from_api(self) -> dict:
        self.auth()
        response = None
        try:
            devices_path = urljoin(C.API_PATH, C.API_DEVICES.format(region=self.region))
            _LOGGER.debug(f"Getting devices, url: {devices_path}")
            response = requests.get(devices_path, headers={
                'X-Auth-Token': self.token,
                'User-Agent': 'evo-mobile',
                'Platform': 'android',
                'Device-Id': self._device_id,
                'Content-Type': 'application/json'
            }, timeout=C.API_TIMEOUT)
            # _LOGGER.debug(response.text)
            response.raise_for_status()
            data = response.json().get("data", {})
            assert isinstance(data, dict), f"Data is not dict: {data}"
            return data
        except Exception as e:
            _LOGGER.error(f"Failed to get devices {e}, response was: {response}")
            return {}

    def read_device_snapshot(self, device_mac: str) -> dict:
        self.auth()
        token = self.token
        response = requests.get(C.API_STATUS.format(mac=device_mac), headers={
            "X-Auth-Token": token, "User-Agent": "evo-mobile", "Platform": "android",
            "Device-Id": self._device_id, "Content-Type": "application/json",
        }, timeout=C.API_TIMEOUT)
        if response.status_code == 401:
            with self._lock:
                if self.token == token:
                    self.tokenexpire = datetime.now(timezone.utc)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("Invalid Haier snapshot")
        return data

    @retry(retry=retry_if_exception_type(HTTPError), stop=stop_after_attempt(2))
    def pull_device_data(self, device_mac: str) -> dict:
        return self.read_device_snapshot(device_mac)

    def pull_data(self) -> None:
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
            state_json = item['state'] = (
                json.loads(state_data)
                if isinstance(state_data, str)
                else state_data
            )
            devices = state_json.setdefault("items", [])
            for d in devices:
                device_title = d.get('title', '')
                device_link = d.get('action', {}).get('link', '')
                parsed_link = urlparse(device_link)
                query_params = parse_qs(parsed_link.query)
                device_type = query_params.setdefault('type', ['UNKNOWN'])[0]
                device_mac = query_params.get('deviceId', [''])[0]
                device_mac = device_mac.replace('%3A', ':')
                device_serial = query_params.get('serialNum', [''])[0]
                device = HaierDevice.create(
                    haier=self,
                    device_type=device_type,
                    device_mac=device_mac,
                    device_serial=device_serial,
                    device_title=device_title,
                )
                self.devices.append(device)
                _LOGGER.info(f"Added device: {device}")
        if len(self.devices) > 0:
            self.connect_in_thread()

    def get_device_by_id(self, id_: str) -> HaierDevice | None:
        return next(filter(
            lambda d: d.device_id == id_,
            self.devices
        ), None)

    def _init_ws(self) -> None:
        self.auth()
        url = urljoin(C.API_WS_PATH, self.token)
        if self.socket_app is None:
            self.socket_app = WebSocketApp(
                url=url,
                on_message=self._on_message,
                on_open=self._on_open,
                on_close=self._on_close,
            )
        else:
            self.socket_app.url = url

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
        self.socket_status = SocketStatus.INITIALIZED
        _LOGGER.debug("Websocket opened")
        for device in self.devices:
            device.init_if_needed()
            if isinstance(device, HaierLaundry):
                device.write_ha_state()
                device.request_snapshot()

    # noinspection PyMethodMayBeStatic,PyUnusedLocal
    def _on_close(self, ws: WebSocket, close_code: int, close_message: str) -> None:
        _LOGGER.debug(f"Websocket closed. Code: {close_code}, message: {close_message}")
        self.socket_status = SocketStatus.NOT_INITIALIZED
        for device in self.devices:
            if isinstance(device, HaierLaundry):
                device.mark_stale()

    def _wait_websocket(self, timeout: float) -> None:
        current = time.time()
        while time.time() <= (current + timeout):
            if self.socket_status == SocketStatus.INITIALIZED:
                return
            time.sleep(0.1)

    def write_ha_state(self) -> None:
        for device in self.devices:
            device.write_ha_state()

    def connect_if_needed(self, timeout: float = 4.0) -> None:
        if self.socket_thread and self.socket_thread.is_alive():
            return self._wait_websocket(timeout)
        return self.connect_in_thread()

    def connect(self) -> None:
        self.socket_status = SocketStatus.NOT_INITIALIZED
        while not self.disconnect_requested:
            self.run_forever()
        _LOGGER.debug("Connection stoped")

    def connect_in_thread(self) -> None:
        self.socket_thread = thread = threading.Thread(target=self.connect)
        thread.daemon = True
        thread.start()

    @connect_limits.sleep_and_retry
    @connect_limits
    def run_forever(self) -> None:
        _LOGGER.debug(f"Connecting to websocket ({C.API_WS_PATH})")
        try:
            self.socket_status = SocketStatus.INITIALIZING
            self._init_ws()
            self.socket_app.run_forever(ping_interval=30, ping_timeout=15)
        except Exception as e:
            _LOGGER.error(f"Error connecting to websocket: {e}")

    @retry(
        retry=retry_if_exception_type(Exception),
        stop=stop_after_attempt(2),
        retry_error_callback=lambda _: None,
        wait=wait_fixed(0.5),
    )
    def send_message(self, payload: str) -> None:
        _LOGGER.debug(f"Sending message: {payload}")
        try:
            self.socket_app.send(payload)
        except Exception as e:
            _LOGGER.warning(f"Failed to send message: {e}")
            self.connect_if_needed()
            raise e


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
        self.sw_version = None
        self._write_ha_state_callbacks = []
        self._available = True
        self._config = None
        self._status_data = backend_data

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}("
            f"{self.device_id!r},"
            f"name={self.device_name!r},"
            f"serial={self.device_serial!r},"
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
        # return self._available
        # this works very bad
        return True

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
            "device_id": self.device_id,
            "device_mac": self.device_mac,
            "device_name": self.device_name,
            "device_serial": self.device_serial,
            "sw_version": self.sw_version,
            "config": self.config.to_dict() if self.config else None,
            "backend_data": self.status_data,
        }

    def _get_status(self, data: dict) -> dict:
        self._status_data = data = (data or {})
        info = data.setdefault("info", {})
        self.device_serial = info.setdefault("serialNumber", self.device_serial)
        device_model = info.setdefault("model", "AC")
        device_model = device_model.replace('-','').replace('/', '')[:11]
        self.device_model = device_model
        self.available = data.setdefault("status", "ONLINE")
        settings = data.setdefault("settings", {})
        self.device_name = settings.setdefault("name", {}).setdefault("name", self.device_name)
        self.sw_version = settings.setdefault('firmware', {}).setdefault('value', None)
        # read config and current values
        self._load_config_from_attributes(data)
        return data

    def _load_config_from_attributes(self, data: dict) -> None:
        pass

    def _set_attribute_value(self, code: str, value: str) -> None:
        pass

    def _handle_status_update(self, received_message: dict) -> None:
        message_statuses = received_message.get("payload", {}).get("statuses", [{}])
        for key, value in message_statuses[0]['properties'].items():
            self._set_attribute_value(key, value)
        self.available = True
        self.write_ha_state()

    def _handle_device_status_update(self, received_message: dict) -> None:
        status = received_message.get("payload", {}).get("status")
        self.available = status
        self.write_ha_state()

    def _handle_info(self, received_message: dict) -> None:
        payload = received_message.get("payload", {})
        self.sw_version = payload.get("swVersion") or self.sw_version

    def _send_message(self, message: dict) -> None:
        self._haier.send_message(json.dumps(message))

    def _send_commands(self, commands: list[dict]) -> None:
        self._send_group_command(commands)

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
        return self.constraint.apply([{
            "commandName": str(attr.code),
            "value": attr.get_item_code(value),
        }] if attr else [])

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

    def write_ha_state(self) -> None:
        for callback in self._write_ha_state_callbacks:
            self.hass.loop.call_soon_threadsafe(callback)

    def add_write_ha_state_callback(self, callback) -> None:
        if callback not in self._write_ha_state_callbacks:
            self._write_ha_state_callbacks.append(callback)

    def remove_write_ha_state_callback(self, callback) -> None:
        if callback in self._write_ha_state_callbacks:
            self._write_ha_state_callbacks.remove(callback)

    # noinspection PyMethodMayBeStatic
    def create_entities_climate(self) -> list:
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

    @classmethod
    def create(
        cls,
        haier: Haier,
        device_type: str,
        device_mac: str,
        device_serial: str = None,
        device_title: str = None,
    ) -> HaierDevice:
        backend_data = haier.pull_device_data(device_mac)
        device_cls = {
            "AC": HaierAC,
            "REF": HaierREF,
            "WM": HaierWM,
        }.get(device_type, cls)
        if (
            device_type in ("WM", "DRY")
            and isinstance(backend_data, dict)
            and isinstance(backend_data.get("smartDeviceControl"), dict)
            and (device_type == "DRY" or not backend_data.get("attributes"))
        ):
            device_cls = HaierLaundry
        if device_cls is cls:
            _LOGGER.warning(f"Unknown device type: {device_type}")
        return device_cls(
            haier=haier,
            device_mac=device_mac,
            device_serial=device_serial,
            device_title=device_title,
            backend_data=backend_data,
        )


class HaierAC(HaierDevice):

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
        })
        return data

    def _set_attribute_value(self, code: str, value: str) -> None:
        attr = self.config.get_attr_by_code(code)
        if not (attr and value is not None):
            return
        elif attr.name == "current_temperature":
            self.current_temperature = float(value)
        elif attr.name == "status":
            self.status = int(value)
        elif attr.name == "target_temperature":
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
        elif attr.name == "quiet":
            self.quiet_on = parsebool(attr.get_item_name(value))
        elif attr.name == "turbo":
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
                continue
            self.config.attrs.append(attr)
        self.config.merge_attributes()
        for attr in self.config.attrs:
            self._set_attribute_value(str(attr.code), attr.current)
            if attr.name == "target_temperature":
                self.min_temperature = float(attr.range.min_value)
                self.max_temperature = float(attr.range.max_value)
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
            return super().get_commands(name, value)
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
        self._send_commands([
            {
                "commandName": self.config['target_temperature'],
                "value": str(value)
            }
        ])
        self.target_temperature = value

    def switch_on(self, value: str = None) -> None:
        value = value or self.mode or HVACMode.AUTO
        self._send_commands([
            *(self.get_commands("status", "on") if not self.status else []),
            *self.get_commands("mode", value),
        ])
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
        from . import switch
        entities = []
        if self.config['light'] is not None:
            entities.append(switch.HaierACLightSwitch(self))
        if self.config['sound'] is not None:
            entities.append(switch.HaierACSoundSwitch(self))
        if self.config['quiet'] is not None:
            entities.append(switch.HaierACQuietSwitch(self))
        if self.config['turbo'] is not None:
            entities.append(switch.HaierACTurboSwitch(self))
        if self.config['health'] is not None:
            entities.append(switch.HaierACHealthSwitch(self))
        if self.config['comfort'] is not None:
            entities.append(switch.HaierACComfortSwitch(self))
        if self.config['cleaning'] is not None:
            entities.append(switch.HaierACCleaningSwitch(self))
        if self.config['antifreeze'] is not None:
            entities.append(switch.HaierACAntiFreezeSwitch(self))
        if self.config['autohumidity'] is not None:
            entities.append(switch.HaierACAutoHumiditySwitch(self))
        return entities

    def create_entities_select(self) -> list:
        from . import select
        entities = []
        if self.config['eco_sensor'] is not None:
            entities.append(select.HaierACEcoSensorSelect(self))
        return entities


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
            self._set_attribute_value(str(attr.code), attr.current)
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
            self._set_attribute_value(str(attr.code), attr.current)
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


def _laundry_selected_values(program) -> dict | None:
    config = program.get("config") if isinstance(program, dict) else None
    values = config.get("selectedValues") if isinstance(config, dict) else None
    if not isinstance(values, list):
        return None
    return {str(item["attrName"]): item["attrValue"] for item in values
            if isinstance(item, dict) and isinstance(item.get("attrName"), (str, int))
            and isinstance(item.get("attrValue"), (str, int, float, bool))}


def _laundry_programs(data: dict) -> dict:
    """Build only complete commands described by Evo; never send them here."""
    control = data.get("smartDeviceControl") or {}
    raw_groups = control.get("groupCommands") or []
    raw_business = control.get("businessAttributes") or []
    if not isinstance(raw_groups, list) or not isinstance(raw_business, list):
        return {}
    groups = {}
    for item in raw_groups:
        if not isinstance(item, dict) or item.get("name") is None or not isinstance(item.get("attrNameList"), list):
            continue
        name = str(item["name"])
        groups[name] = set(map(str, item["attrNameList"])) if name not in groups else None
    business = {}
    for item in raw_business:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            name = item["name"]
            business[name] = item.get("commandParameters") if name not in business else None
    delay = ((data.get("controlBlock") or {}).get("delayStart") or {})
    delay = delay if isinstance(delay, dict) else {}
    delay_link = delay.get("link")
    status_link = delay.get("statusLink")
    delay_key = str(delay_link.get("name", "")) if isinstance(delay_link, dict) else ""
    delay_status = str(status_link.get("name", "")) if isinstance(status_link, dict) else ""
    status_attributes = [attr for attr in control.get("attributes", [])
                         if isinstance(attr, dict) and str(attr.get("name")) == delay_status]
    status_attribute = status_attributes[0] if len(status_attributes) == 1 else {}
    status_list = status_attribute.get("list") or {}
    can_disable_delay = (status_attribute.get("writable") is True
                         and isinstance(status_list, dict)
                         and isinstance(status_list.get("data"), list)
                         and any(isinstance(item, dict) and item.get("data") == "false"
                                 for item in status_list["data"]))
    entries = []
    dryer = (data.get("device") or {}).get("programDetails") or {}
    for item in dryer.get("details", []):
        if isinstance(item, dict):
            entries.append(((item.get("detail") or {}).get("name"), item))
    for block in (data.get("allProgram") or {}).get("blocks", []):
        if not isinstance(block, dict):
            continue
        for item in block.get("programs", []):
            if not isinstance(item, dict):
                continue
            name = (item.get("preview") or {}).get("name")
            if name and block.get("title"):
                entries.append((f"{block['title']} - {name}", item))
    counts = {}
    for title, _ in entries:
        if isinstance(title, str):
            counts[title] = counts.get(title, 0) + 1
    result = {}
    for title, item in entries:
        if not isinstance(title, str) or not title or title in ("Не выбрана", "Not selected") or len(title) > 255 or counts[title] != 1:
            continue
        config = item.get("programConfig") or {}
        if not isinstance(config, dict) or not isinstance(config.get("link"), dict):
            continue
        param = business.get(config["link"].get("name"))
        if not isinstance(param, dict):
            continue
        group = str(param.get("name", ""))
        fields = param.get("attrNameList") or []
        if not isinstance(groups.get(group), set) or not isinstance(fields, list) or not fields or not all(isinstance(f, dict) and f.get("name") is not None for f in fields):
            continue
        field_names = [str(f["name"]) for f in fields]
        if len(set(field_names)) != len(field_names) or not set(field_names) <= groups[group]:
            continue
        selected = config.get("selectedValues") or []
        if not isinstance(selected, list) or not all(isinstance(v, dict) and v.get("attrName") is not None and "attrValue" in v for v in selected):
            continue
        override = {str(v["attrName"]): v["attrValue"] for v in selected}
        if len(override) != len(selected) or not set(override) <= set(field_names):
            continue
        delay_field = next((field for field in fields if str(field["name"]) == delay_key), {})
        delay_value = override.get(delay_key, delay_field.get("defaultValue"))
        commands = []
        for field in fields:
            name = str(field["name"])
            value = override.get(name, field.get("defaultValue"))
            if (value is None and field.get("valueType") == "2"
                    and name == delay_status and can_disable_delay
                    and str(delay_value) in ("0", "0.0")
                    and _laundry_value_valid(delay_field, delay_value)):
                # Immediate start has no scheduled finish time; Evo leaves this slot dynamic.
                value = "false"
            if not _laundry_value_valid(field, value):
                break
            commands.append({"commandName": name, "value": value})
        else:
            result[title] = {"commandName": group, "commands": commands}
    return result


def _laundry_actions(data: dict) -> dict:
    """Read pause, resume and cancel values from the appliance's descriptors."""
    control = data.get("smartDeviceControl") or {}
    writable = {str(a.get("name")): a for a in control.get("attributes", [])
                if isinstance(a, dict) and a.get("writable") is True}
    dryer = (data.get("device") or {}).get("mainControl") or {}
    washer = data.get("controlBlock") or {}
    pause = (dryer.get("resumePause") or {}).get("pause") or washer.get("pause") or {}
    resume = washer.get("resumeProgram") or {}
    cancel = dryer.get("cancel") or washer.get("cancel") or {}
    main = pause.get("mainBtn") or {}
    candidates = {
        "pause": ((pause.get("link") or {}).get("name"), pause.get("pauseValue", (main.get("onButton") or {}).get("commandValue"))),
        "resume": ((resume.get("link") or pause.get("link") or {}).get("name"), resume.get("resumeValue", (main.get("offButton") or {}).get("commandValue"))),
        "cancel": ((cancel.get("link") or {}).get("name"), cancel.get("cancelValue")),
    }
    result = {}
    for action, (name, value) in candidates.items():
        if name is None or str(name) not in writable or not isinstance(value, (str, int, float, bool)):
            continue
        choices = ((writable[str(name)].get("list") or {}).get("data") or [])
        if choices and str(value) not in {str(item.get("data")) for item in choices if isinstance(item, dict)}:
            continue
        result[action] = {"commandName": str(name), "value": value}
    return result



LAUNDRY_ACTION_STATES = {"start": {"idle"}, "pause": {"running"},
                         "resume": {"paused"}, "cancel": {"running", "paused"}}


class HaierLaundry(HaierDevice):
    """Read laundry data described by the nested Evo response."""

    def __init__(self, backend_data: dict = None, **kwargs) -> None:
        super().__init__(**kwargs)
        data = backend_data or {}
        control = data.get("smartDeviceControl") or {}
        self.fresh = True
        self._state_revision = 0
        self._last_observed = time.monotonic()
        self.recovery = None
        self._start_phase = None
        alarms = control.get("alarms")
        self.alarm_codes = tuple(sorted(set(alarms))) if isinstance(alarms, list) and all(isinstance(x, str) for x in alarms) else None
        self._last_notified_alarms = self.alarm_codes
        errors = control.get("errors")
        descriptions = errors.get("alarmToResult") if isinstance(errors, dict) else None
        self.alarm_descriptions = descriptions if isinstance(descriptions, dict) else {}
        self.machine_entity_id = None
        self._last_finished_trace = None
        self._finish_unsub = None
        self.programs = _laundry_programs(data)
        self._program_data = data
        self.program_catalog = build_catalogue(data, self.programs)
        self._program_parameters = {}
        self.connection_status = None
        self.actions = _laundry_actions(data)
        self.selected_program = None
        self._start_pending = False
        self._laundry_lock = threading.Lock()
        remote = ((data.get("device") or {}).get("mainControl") or {}).get("remoteControlMode") or data.get("remoteControlMode") or {}
        self.remote_key = (remote.get("link") or {}).get("name")
        self.values = {
            str(attr["name"]): attr.get("currentValue")
            for attr in control.get("attributes", [])
            if attr.get("readable") and "name" in attr
        }
        self.is_dryer = "monitoringScreen" in (data.get("device") or {})
        monitor = (data.get("device") or {}).get("monitoringScreen") or (
            data.get("allProgram") or {}
        ).get("monitoringScreen") or {}
        phase = monitor.get("phase") or {}
        self.phase_key = (phase.get("phase") or phase).get("name")
        self.phase_labels = dict(phase.get("cyclePhaseValueToTitle") or {})
        for key, value_key in (("weighing", "weightAttrValue"),
                               ("finishing", "finishingAttrValue"),
                               ("freshAirSystem", "freshAirSystemAttrValue")):
            description = monitor.get(key) or {}
            if isinstance(description, dict) and description.get(value_key) is not None and description.get("title"):
                self.phase_labels[str(description[value_key])] = description["title"]
        timer = monitor.get("timer") or {}
        self.hours_key = (timer.get("remainingTimeHH") or {}).get("name")
        self.minutes_key = (timer.get("remainingTimeMM") or {}).get("name")
        program = data.get("program")
        program = program if isinstance(program, dict) else {}
        self._program_status = program.get("status")
        self._program_name = program.get("title")
        self._active_program_values = _laundry_selected_values(program) or {}
        self.has_program = isinstance(data.get("program"), dict) or "monitoringScreen" in (data.get("device") or {})
        observed_status = control.get("status")
        self._get_status(dict(control))
        self.available = observed_status if observed_status in (True, False, "ONLINE", "OFFLINE") else False
        self._status_data = data
        self._cycle_active = self.program_status in ("running", "paused")

    @property
    def available(self) -> bool:
        return self._available and self.fresh and self._haier.socket_status == SocketStatus.INITIALIZED

    @available.setter
    def available(self, value: bool | str):
        HaierDevice.available.fset(self, value)

    @property
    def phase(self) -> str | None:
        value = self.values.get(str(self.phase_key))
        if value is None:
            return None
        label = self.phase_labels.get(str(value))
        if label is not None:
            return label
        language = getattr(self.hass.config, "language", "en")
        prefix = "Код" if isinstance(language, str) and language.startswith("ru") else "Code"
        return f"{prefix} {value}"

    @property
    def program_status(self) -> str | None:
        if self._start_pending:
            return None
        raw = str(self._program_status or "").upper().removeprefix("PROGRAM_")
        states = {"IN_PROGRESS": "running", "FINISHED": "finished", "PAUSED": "paused",
                  "CANCELED": "cancelled", "CANCELLED": "cancelled"}
        if raw in states:
            return states[raw]
        phase = self.values.get(str(self.phase_key))
        label = str(self.phase_labels.get(str(phase), "")).casefold()
        if not raw and str(phase) == "0" and label in ("режим ожидания", "standby", "idle"):
            return "idle"
        return None

    @property
    def program_name(self) -> str | None:
        if self._program_name:
            return self._program_name
        if self.program_status == "idle":
            language = getattr(self.hass.config, "language", "en")
            return "Нет активной программы" if isinstance(language, str) and language.startswith("ru") else "No active program"
        return None

    @property
    def remaining_time(self) -> int | None:
        try:
            hours = int(self.values[str(self.hours_key)])
            minutes = int(self.values[str(self.minutes_key)])
            return hours * 60 + minutes if hours >= 0 and 0 <= minutes < 60 else None
        except (KeyError, TypeError, ValueError):
            return None

    def request_snapshot(self) -> None:
        if self.recovery is not None:
            self.recovery.request_refresh()

    def _push_updated(self) -> None:
        if self.recovery is not None:
            self.recovery.push_updated()

    def mark_stale(self) -> None:
        with self._laundry_lock:
            self._state_revision += 1
            self.fresh = False
            self.values.clear()
            self._program_status = None
            self._program_name = None
            self.selected_program = None
            self.alarm_codes = None
        self.write_ha_state()

    def apply_snapshot(self, data: dict, revision: int) -> bool:
        control = data.get("smartDeviceControl") if isinstance(data, dict) else None
        if not isinstance(control, dict) or control.get("status") not in (True, False, "ONLINE", "OFFLINE"):
            raise ValueError("Missing Haier availability")
        online = control["status"] in (True, "ONLINE")
        attributes = control.get("attributes")
        if not isinstance(attributes, list):
            raise ValueError("Missing Haier attributes")
        values = {str(item["name"]): item.get("currentValue") for item in attributes
                  if isinstance(item, dict) and item.get("readable") and "name" in item}
        required = {str(key) for key in (self.phase_key, self.remote_key) if key is not None}
        if online and (not values or not required.issubset(values)):
            raise ValueError("Incomplete Haier snapshot")
        alarms = control.get("alarms")
        codes = tuple(sorted(set(alarms))) if isinstance(alarms, list) and all(isinstance(x, str) for x in alarms) else None
        program = data.get("program")
        with self._laundry_lock:
            if revision != self._state_revision:
                return False
            was_active = self._cycle_active or self.program_status in ("running", "paused")
            previous = self.program_status
            previous_phase = self.values.get(str(self.phase_key))
            self._state_revision += 1
            self._last_observed = time.monotonic()
            self.available = online
            self.fresh = online
            self.values = values if online else {}
            self.alarm_codes = codes if online else None
            self._status_data = data
            if not online:
                self._program_status = None
                self._program_name = None
                self.selected_program = None
            elif isinstance(program, dict):
                selected = _laundry_selected_values(program)
                if selected is not None:
                    self._active_program_values = selected
                elif program.get("title") and program["title"] != self._program_name:
                    self._active_program_values = {}
                if program.get("status"):
                    self._program_status = program["status"]
                if program.get("title"):
                    self._program_name = program["title"]
                if str(self._program_status or "").upper().removeprefix("PROGRAM_") in {"IN_PROGRESS", "FINISHED", "PAUSED", "CANCELED", "CANCELLED"}:
                    self._start_pending = False
            elif (str(values.get(str(self.phase_key))) == "0"
                  and str(self.phase_labels.get("0", "")).casefold() in ("режим ожидания", "standby", "idle")):
                self._program_status = None
                self._program_name = None
            elif str(values.get(str(self.phase_key))) != str(previous_phase):
                if previous not in ("running", "paused"):
                    self._program_status = None
                    self._program_name = None
            if online and self._start_pending and str(values.get(str(self.phase_key))) != str(self._start_phase):
                self._start_pending = False
            if online and was_active and self.program_status == "finished":
                self._fire_laundry_event("program_finished")
            if online and self.program_status is not None:
                self._cycle_active = self.program_status in ("running", "paused")
            if online and codes is not None and codes != self._last_notified_alarms:
                had_errors = bool(self._last_notified_alarms)
                self._last_notified_alarms = codes
                if codes or had_errors:
                    self._fire_laundry_event("error" if codes else "error_cleared")
        self.write_ha_state()
        return True

    def _set_attribute_value(self, code: str, value: str) -> None:
        self.values[str(code)] = value

    def _handle_status_update(self, received_message: dict) -> None:
        payload = received_message.get("payload") or {}
        statuses = payload.get("statuses") if isinstance(payload, dict) else None
        if not isinstance(statuses, list) or not statuses or not isinstance(statuses[0], dict):
            return
        properties = statuses[0].get("properties")
        if not isinstance(properties, dict) or not properties:
            return
        was_fresh = self.fresh
        with self._laundry_lock:
            self._state_revision += 1
            self._last_observed = time.monotonic()
            phase = str(self.phase_key)
            if phase in properties and str(properties[phase]) != str(self.values.get(phase)):
                if str(properties[phase]) != str(self._start_phase):
                    self._start_pending = False
                if self.program_status not in ("running", "paused"):
                    self._program_status = None
                    self._program_name = None
            for code, value in properties.items():
                self._set_attribute_value(code, value)
            self.fresh = True
            self.available = True
        self.write_ha_state()
        self._push_updated()
        if not was_fresh:
            self.request_snapshot()

    def _handle_device_status_update(self, received_message: dict) -> None:
        payload = received_message.get("payload") or {}
        status = payload.get("status") if isinstance(payload, dict) else None
        if status not in (True, False, "ONLINE", "OFFLINE"):
            return
        with self._laundry_lock:
            self._state_revision += 1
            if status in (False, "OFFLINE"):
                self.fresh = False
                self.alarm_codes = None
                self.values.clear()
                self._program_status = None
                self._program_name = None
                self.selected_program = None
            self.available = status
        self.write_ha_state()
        if status in (True, "ONLINE"):
            self.request_snapshot()

    @property
    def error_details(self) -> list[dict] | None:
        if self.alarm_codes is None:
            return None
        result = []
        for key in self.alarm_codes:
            definition = self.alarm_descriptions.get(key)
            definition = definition if isinstance(definition, dict) else {}
            text = definition.get("text")
            text = text if isinstance(text, str) and text else key
            code = text[1:].split("]", 1)[0] if text.startswith("[") and "]" in text else key
            result.append({"key": key, "code": code, "text": text,
                           "description": definition.get("description") if isinstance(definition.get("description"), str) else ""})
        return result

    def _fire_laundry_event(self, event_type: str) -> None:
        if not self.machine_entity_id:
            return
        event = {
            "type": event_type, "entity_id": self.machine_entity_id,
            "name": self.device_name, "model": self.device_model,
            "kind": "dryer" if self.is_dryer else "washer",
            "program": self.program_name, "errors": self.error_details or [],
        }
        if event_type == "program_finished":
            self.hass.loop.call_soon_threadsafe(self._schedule_finish_confirmation, event)
        else:
            self.hass.loop.call_soon_threadsafe(self.hass.bus.async_fire, "haier_evo_laundry_event", event)

    def cancel_pending_finish(self) -> None:
        if self._finish_unsub is not None:
            self._finish_unsub()
            self._finish_unsub = None

    def _schedule_finish_confirmation(self, event: dict) -> None:
        from homeassistant.helpers.event import async_call_later
        if self.recovery is None:
            return
        self.cancel_pending_finish()

        @callback
        def confirm(_now):
            self._finish_unsub = None
            # Evo can finish a stage and immediately report the same cycle running again.
            if self.available and not self._cycle_active and self.program_status != "cancelled":
                event["errors"] = self.error_details or []
                self.hass.bus.async_fire("haier_evo_laundry_event", event)

        self._finish_unsub = async_call_later(self.hass, 10, confirm)

    def on_message(self, message_dict: dict) -> None:
        event = message_dict.get("event")
        if event == "deviceConnectionStatusEvent":
            payload = message_dict.get("payload")
            status = payload.get("status") if isinstance(payload, dict) else None
            if isinstance(status, str) and 0 < len(status) <= 64:
                self.connection_status = status
                self.write_ha_state()
            return
        if event == "alarms":
            payload = message_dict.get("payload") or {}
            alarms = payload.get("alarms") if isinstance(payload, dict) else None
            valid = isinstance(alarms, list) and all(isinstance(x, str) for x in alarms)
            codes = tuple(sorted(set(alarms))) if valid else None
            with self._laundry_lock:
                self._state_revision += 1
                self._last_observed = time.monotonic()
                previous = self.alarm_codes
                self.alarm_codes = codes
                if codes is not None and codes != self._last_notified_alarms:
                    had_errors = bool(self._last_notified_alarms)
                    self._last_notified_alarms = codes
                    if codes or had_errors:
                        self._fire_laundry_event("error" if codes else "error_cleared")
            self.write_ha_state()
            if valid:
                self._push_updated()
            return
        if event in ("ProgramDryerBaseEvent", "ProgramWasherBaseEvent"):
            data = message_dict.get("data")
            program = data.get("program") if isinstance(data, dict) else None
            if isinstance(program, dict) or (isinstance(data, dict) and data.get("status") == "PROGRAM_FINISHED"):
                with self._laundry_lock:
                    self._state_revision += 1
                    self._last_observed = time.monotonic()
                    previous = self.program_status
                    event_status = program.get("status") if isinstance(program, dict) else None
                    if not event_status and data.get("status") == "PROGRAM_FINISHED":
                        event_status = "FINISHED"
                    if event_status:
                        self._program_status = event_status
                        if str(event_status).upper().removeprefix("PROGRAM_") in {"IN_PROGRESS", "FINISHED", "PAUSED", "CANCELED", "CANCELLED"}:
                            self._start_pending = False
                    if isinstance(program, dict):
                        selected = _laundry_selected_values(program)
                        if selected is not None:
                            self._active_program_values = selected
                        elif program.get("title") and program["title"] != self._program_name:
                            self._active_program_values = {}
                        self._program_name = program.get("title", self._program_name)
                    trace = message_dict.get("trace")
                    if self.program_status in ("running", "paused"):
                        self._cycle_active = True
                    if (self.program_status == "finished" and previous != "finished"
                            and (not trace or trace != self._last_finished_trace)):
                        self._last_finished_trace = trace
                        self._fire_laundry_event("program_finished")
                    if self.program_status in ("finished", "cancelled", "idle"):
                        self._cycle_active = False
                self.write_ha_state()
                self._push_updated()
            return
        super().on_message(message_dict)

    @property
    def remote_control(self) -> bool | None:
        value = self.values.get(str(self.remote_key))
        if isinstance(value, bool):
            return value
        value = str(value).lower()
        if value in ("1", "true", "on"):
            return True
        if value in ("0", "false", "off"):
            return False
        return None

    def set_selected_program(self, value: str | None) -> None:
        with self._laundry_lock:
            if value is not None and value not in self.programs:
                raise HomeAssistantError("Неизвестная программа")
            self.selected_program = value
            self._program_parameters = {}
        self.write_ha_state()

    @property
    def program_settings(self) -> dict:
        if self.selected_program not in self.programs:
            return {}
        try:
            settings = program_settings(self._program_data, self.selected_program,
                                        self.programs[self.selected_program], self._program_parameters,
                                        self.values, self.alarm_codes)
        except ValueError as err:
            return {"program": self.selected_program, "fields": [], "error": str(err)}
        return {key: value for key, value in settings.items() if key != "commands"}

    @property
    def active_program_details(self) -> dict:
        if not self.available or not self._program_name:
            return {}
        details = active_program_details(self._program_data, self._program_name, self.programs,
                                         self.values, self._active_program_values)
        if self.remaining_time is not None:
            details["remaining_minutes"] = self.remaining_time
        return details

    def configure_program(self, program: str, parameters: dict) -> None:
        with self._laundry_lock:
            if program != self.selected_program or program not in self.programs:
                raise ServiceValidationError("Выбранная программа изменилась; откройте параметры заново")
            if self._start_pending or self.program_status in ("running", "paused"):
                raise ServiceValidationError("Параметры можно изменить до запуска программы")
            settings = self.program_settings
            editable = {field["key"] for field in settings.get("fields", []) if not field.get("disabled")}
            if not isinstance(parameters, dict) or not parameters or not set(parameters) <= editable:
                raise ServiceValidationError("Параметр недоступен для выбранной программы")
            updated = {**self._program_parameters, **parameters}
            try:
                program_settings(self._program_data, program, self.programs[program], updated,
                                 self.values, self.alarm_codes)
            except ValueError as err:
                raise ServiceValidationError(str(err)) from err
            self._program_parameters = updated
        self.write_ha_state()

    def send_laundry_action(self, action: str) -> None:
        try:
            with self._laundry_lock:
                if not self.available or self.remote_control is not True:
                    raise HomeAssistantError("Дистанционное управление недоступно")
                if (self._start_pending or action not in LAUNDRY_ACTION_STATES
                        or (self.program_status is not None
                            and self.program_status not in LAUNDRY_ACTION_STATES[action])):
                    raise HomeAssistantError("Состояние программы не позволяет выполнить команду")
                if action == "start":
                    if self.selected_program not in self.programs:
                        raise HomeAssistantError("Сначала выберите программу")
                    try:
                        settings = program_settings(self._program_data, self.selected_program,
                                                    self.programs[self.selected_program], self._program_parameters,
                                                    self.values, self.alarm_codes)
                    except ValueError as err:
                        raise HomeAssistantError(str(err)) from err
                    message = {"action": "operation", **self.programs[self.selected_program],
                               "commands": settings["commands"]}
                elif action in self.actions:
                    message = {"action": "command", "command": self.actions[action]}
                else:
                    raise HomeAssistantError("Команда не поддерживается")
                socket_app = self._haier.socket_app
                if socket_app is None or socket_app.sock is None or not socket_app.sock.connected:
                    raise HomeAssistantError("Связь с Haier отсутствует")
                message.update({"macAddress": self.device_id, "trace": str(uuid.uuid4())})
                if action == "start":
                    self._start_phase = self.values.get(str(self.phase_key))
                    self.selected_program = None
                    self._start_pending = True
                try:
                    socket_app.send(json.dumps(message))
                except Exception as err:
                    raise HomeAssistantError("Не удалось подтвердить отправку команды Haier; проверьте машину") from err
        finally:
            if action == "start":
                self.write_ha_state()

    def create_entities_select(self) -> list:
        if not self.programs:
            return []
        from .select import HaierLaundryProgramSelect
        return [HaierLaundryProgramSelect(self)]

    @property
    def available_actions(self) -> list[str]:
        if not self.available or self.remote_control is not True or self._start_pending:
            return []
        actions = ["start", *self.actions] if self.programs else list(self.actions)
        return [action for action in actions
                if (self.program_status is None
                    or self.program_status in LAUNDRY_ACTION_STATES[action])
                and (action != "start" or self.selected_program in self.programs)]

    def create_entities_binary_sensor(self) -> list:
        if self.remote_key is None:
            return []
        from .binary_sensor import HaierLaundryRemoteControlSensor
        return [HaierLaundryRemoteControlSensor(self)]

    def create_entities_sensor(self) -> list:
        from .sensor import HaierLaundrySensor
        entities = []
        if self.alarm_codes is not None or self.alarm_descriptions:
            from .sensor import HaierLaundryFaultSensor
            entities.append(HaierLaundryFaultSensor(self))
        if self.phase_key is not None:
            key = "laundry_phase" if self.is_dryer else "laundry_phase_code"
            entities.append(HaierLaundrySensor(self, "phase", key))
        if self.hours_key is not None and self.minutes_key is not None:
            entities.append(HaierLaundrySensor(self, "remaining_time", "laundry_program_time"))
        entities.append(HaierLaundrySensor(self, "program_status", "laundry_program_status"))
        entities.append(HaierLaundrySensor(self, "program_name", "laundry_program_name"))
        return entities


def parsebool(value) -> bool:
    if value in ("on", 1, True, "true", "enable", "1"):
        return True
    return False
