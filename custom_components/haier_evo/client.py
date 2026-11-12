"""Async REST client for the Haier Evo cloud: auth, token storage, requests."""
from __future__ import annotations

import asyncio
import math
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any, TypeVar
from urllib.parse import quote, urljoin

from aiohttp import (
    ClientConnectorError,
    ClientError,
    ClientOSError,
    ClientPayloadError,
    ClientResponse,
    ClientSession,
    ClientTimeout,
    ConnectionTimeoutError,
    ServerDisconnectedError,
    SocketTimeoutError,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import const as C
from .logger import _LOGGER

TOKEN_MARGIN = timedelta(seconds=60)
# Used when the auth answer has no parsable "expire": without it every
# request would refresh the token.
TOKEN_FALLBACK_TTL = timedelta(hours=1)
STORAGE_VERSION = 1
_T = TypeVar("_T")
# Not connected: the request never left, trying again is safe for any method.
_CONNECT_ERRORS = (ClientConnectorError, ConnectionTimeoutError)
# Connected, but the answer was lost on the way: safe to repeat for a GET only.
_READ_ERRORS = (ServerDisconnectedError, ClientPayloadError, SocketTimeoutError, ClientOSError)


def _timeout() -> ClientTimeout:
    """5 s to connect, 15 s between reads of the answer: requests' (5, 15) of 1.7."""
    return ClientTimeout(total=None, sock_connect=C.API_CONNECT_TIMEOUT, sock_read=C.API_READ_TIMEOUT)


class HaierError(Exception):
    """Base error of the Haier Evo client."""


class HaierAuthError(HaierError):
    """Credentials are rejected: user has to re-enter them."""


class HaierTokenError(HaierError):
    """Refresh token is rejected: a full login is needed."""


class HaierConnectionError(HaierError):
    """Network error, timeout or 5xx: retry later."""


class HaierRateLimitError(HaierError):
    """HTTP 429 or our own login throttling."""

    def __init__(self, retry_after: float) -> None:
        super().__init__(f"Too many requests, retry after {retry_after:.0f} s")
        self.retry_after = retry_after


class HaierApiError(HaierError):
    """Unexpected 4xx answer of the API."""


def parse_expire(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt_util.parse_datetime(value)
    except ValueError:
        return None  # "9999-99-99T..." passes the pattern, not the calendar
    if parsed is None:
        try:
            parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        # The backend answers in Moscow time; keep the old assumption (UTC+3).
        parsed = parsed.replace(tzinfo=dt_util.get_time_zone("Europe/Moscow"))
    return parsed


def _retry_after(resp: ClientResponse, default: float) -> float:
    """Seconds to wait after a 429, between 1 s and LOGIN_LIMIT_MAX.

    The header is not trusted beyond that: "864000" from a proxy in front of
    the cloud would close the login for ten days, "inf" until a restart.
    """
    try:
        value = float(resp.headers.get("Retry-After", default))
    except (TypeError, ValueError):
        value = float(default)
    if not math.isfinite(value):
        value = float(default)
    return min(max(value, 1.0), float(C.LOGIN_LIMIT_MAX))


def read_tokens(data: Any) -> dict[str, Any]:
    """Tokens as saved by any version.

    1.x (and the old /config/haier_evo) name the keys "tokenexpire",
    "refreshtoken", "refreshexpire", and so does 2.0, under the same Store key:
    a downgrade to 1.7 keeps the sign-in. The temandroid/haier_evo_2 fork (0.x)
    saved "token_expire", "refresh_token", "refresh_expire".
    """
    data = data if isinstance(data, dict) else {}

    def pick(*keys: str) -> Any:
        return next((data[key] for key in keys if data.get(key)), None)

    return {
        "token": pick("token"),
        "token_expire": parse_expire(pick("token_expire", "tokenexpire")),
        "refresh_token": pick("refresh_token", "refreshtoken"),
        "refresh_expire": parse_expire(pick("refresh_expire", "refreshexpire")),
    }


class HaierClient:
    """Talks to evo.haieronline.ru. Never logs passwords or tokens.

    The password is optional: entries keep only tokens (as 1.2+
    does). Without it a refresh token the cloud rejects ends in
    re-authentication, the only way to get a new one.
    """

    def __init__(
        self,
        session: ClientSession,
        email: str,
        password: str | None,
        region: str,
        store: Store | None = None,
        device_id: str | None = None,
    ) -> None:
        self._session = session
        self.email = email
        self._password = password
        self.region = region
        self._store = store
        # A sign-in without an entry (a new one) gets a random id, as in 1.x:
        # a fixed one would be the same for every installation.
        self.device_id = device_id or str(uuid.uuid4())
        self._lock = asyncio.Lock()
        self._token: str | None = None
        self._token_expire: datetime | None = None
        self._refresh_token: str | None = None
        self._refresh_expire: datetime | None = None
        # time.monotonic() of the last sign-in or refresh (see token_rejected)
        self._token_issued = -math.inf
        self._login_blocked_until: float = 0.0
        self._login_backoff: float = C.LOGIN_LIMIT_429
        # Set once the cloud rejects the password: logging in again with the same
        # one until reauth only risks a lockout of the account.
        self._auth_failed = False

    @property
    def has_token(self) -> bool:
        return bool(self._token)

    @property
    def has_refresh_token(self) -> bool:
        return bool(self._refresh_token)

    def _headers(self, token: str | None = None) -> dict[str, str]:
        headers = {
            "User-Agent": "evo-mobile",
            "Platform": "android",
            "Accept": "*/*",
            "Device-Id": self.device_id,
        }
        if token:
            headers["X-Auth-Token"] = token
        return headers

    def _url(self, path: str) -> str:
        return urljoin(C.API_PATH, path.format(region=self.region))

    def ws_url(self, token: str) -> str:
        # As in 1.x: the token is the last path segment, sent as is.
        return urljoin(C.API_WS_PATH, token)

    # --- token storage (.storage/haier_evo.<entry_id>) ---

    async def async_load_tokens(self) -> None:
        if self._store is None:
            return
        data = await self._store.async_load()
        if not isinstance(data, dict):
            return
        self._apply_tokens(data)
        self.device_id = data.get("device_id") or self.device_id

    def _apply_tokens(self, data: dict) -> None:
        tokens = read_tokens(data)
        self._token = tokens["token"]
        self._token_expire = tokens["token_expire"]
        self._refresh_token = tokens["refresh_token"]
        self._refresh_expire = tokens["refresh_expire"]

    def _tokens_1x(self) -> dict:
        """The tokens in the format of 1.x: 1.7 reads the same file after a downgrade."""
        return {
            "token": self._token,
            "tokenexpire": self._token_expire.isoformat() if self._token_expire else None,
            "refreshtoken": self._refresh_token,
            "refreshexpire": self._refresh_expire.isoformat() if self._refresh_expire else None,
        }

    async def _async_save_tokens(self) -> None:
        if self._store is None:
            return
        await self._store.async_save({**self._tokens_1x(), "device_id": self.device_id})

    def invalidate_access_token(self, rejected: str | None = None) -> None:
        """The server rejected the access token: refresh it on next use.

        With `rejected` given, only that token is dropped: a request that was in
        flight with an older token must not throw away a token another request
        has just refreshed (each refresh rotates the pair on the server).
        """
        if rejected is not None and rejected != self._token:
            return
        self._token_expire = None

    def token_rejected(self, token: str | None) -> bool:
        """The cloud refused this access token: renew it on the next use.

        Not when another request has renewed it already, nor right after it
        was issued: the IoT backend may not know a new token for a few seconds,
        and renewing it then only rotates the pair again. True when the caller
        should wait TOKEN_RETRY_DELAY s and try the same token again (1.7).
        """
        if not token or token != self._token:
            return False
        if time.monotonic() - self._token_issued < C.TOKEN_FRESH:
            return True
        self._token_expire = None
        return False

    def export_tokens(self) -> dict:
        """Tokens of a fresh login, handed from the config flow to the entry (1.x format)."""
        return self._tokens_1x()

    async def async_import_tokens(self, data: dict) -> None:
        """Tokens handed over by the config flow or found in an old file (any format)."""
        self._apply_tokens(data)
        await self._async_save_tokens()

    def _set_tokens(self, token: dict) -> None:
        self._token = token["accessToken"]
        self._token_expire = parse_expire(token.get("expire"))
        if self._token_expire is None:
            _LOGGER.warning("Haier Evo sent no token expiry, assuming %s", TOKEN_FALLBACK_TTL)
            self._token_expire = dt_util.utcnow() + TOKEN_FALLBACK_TTL
        if refresh_token := token.get("refreshToken"):
            # An answer without a new refresh token keeps the old one (1.7):
            # with no password kept, dropping it would end in reauth.
            self._refresh_token = refresh_token
            self._refresh_expire = parse_expire(token.get("refreshExpire"))
        self._token_issued = time.monotonic()

    def _clear_tokens(self) -> None:
        self._token = None
        self._token_expire = None
        self._refresh_token = None
        self._refresh_expire = None

    # --- auth ---

    async def _async_retrying(self, request: Callable[[], Awaitable[_T]], *, idempotent: bool) -> _T:
        """Run a request, repeating it after a lost connection as 1.7 did (urllib3 Retry).

        A connection that was not established is tried API_CONNECT_RETRIES
        times more for any request; an answer lost after the request left is
        tried API_READ_RETRIES times more, for an idempotent request only.
        """
        connects = reads = 0
        while True:
            try:
                return await request()
            except _CONNECT_ERRORS as err:
                if connects >= C.API_CONNECT_RETRIES:
                    raise HaierConnectionError(f"Request failed: {type(err).__name__}") from None
                connects += 1
                error: Exception = err
            except _READ_ERRORS as err:
                if not idempotent or reads >= C.API_READ_RETRIES:
                    raise HaierConnectionError(f"Request failed: {type(err).__name__}") from None
                reads += 1
                error = err
            except (ClientError, TimeoutError) as err:
                raise HaierConnectionError(f"Request failed: {type(err).__name__}") from None
            _LOGGER.debug("Request failed (%s), trying again", type(error).__name__)
            await asyncio.sleep(C.API_RETRY_BACKOFF * 2 ** (connects + reads - 1))

    async def _async_post_auth(self, path: str, data: dict[str, str]) -> dict:
        url = self._url(path)

        async def post() -> tuple[int, Any]:
            async with self._session.post(
                url,
                data=data,
                headers=self._headers(),
                timeout=_timeout(),
                # The sign-in form carries the password: never follow it elsewhere.
                allow_redirects=False,
            ) as resp:
                status = resp.status
                if 300 <= status < 400:
                    raise HaierConnectionError(f"Auth endpoint redirected (HTTP {status})")
                if status == 429:
                    raise HaierRateLimitError(_retry_after(resp, self._login_backoff))
                if status >= 500:
                    raise HaierConnectionError(f"Auth endpoint answered HTTP {status}")
                try:
                    return status, await resp.json(content_type=None)
                except ValueError:
                    return status, None

        status, body = await self._async_retrying(post, idempotent=False)

        if not isinstance(body, dict):
            # A bare 4xx page (proxy, WAF, maintenance) is no verdict on the
            # password or the refresh token: retrying later is safe, dropping
            # the tokens is not (with no password kept that is a reauth).
            raise HaierConnectionError(f"Auth endpoint answered HTTP {status} without details")
        error = body.get("error")
        if error:
            validation = (error.get("validation") or {}) if isinstance(error, dict) else {}
            if validation.get("refreshToken"):
                raise HaierTokenError("Refresh token rejected")
            if validation.get("email") or validation.get("password"):
                raise HaierAuthError("Email or password rejected")
            if status in (401, 403):
                raise HaierAuthError(f"HTTP {status}")
            message = error.get("message") if isinstance(error, dict) else error
            if status >= 400:
                raise HaierApiError(f"Auth error from server: {message}")
            raise HaierConnectionError(f"Auth error from server: {message}")
        if status in (401, 403):
            raise HaierAuthError(f"HTTP {status}")
        if status >= 400:
            raise HaierApiError(f"Auth endpoint answered HTTP {status}")
        token = (body.get("data") or {}).get("token") or {}
        if not isinstance(token.get("accessToken"), str) or not token["accessToken"]:
            raise HaierConnectionError("No access token in auth answer")
        return token

    async def async_login(self) -> None:
        if self._auth_failed:
            raise HaierAuthError("Credentials were rejected, waiting for re-authentication")
        if not self._password:
            # Entries keep no password: signing in again needs the user.
            raise HaierAuthError("The saved sign-in is not valid any more, re-authentication is needed")
        loop_time = asyncio.get_running_loop().time()
        if loop_time < self._login_blocked_until:
            raise HaierRateLimitError(self._login_blocked_until - loop_time)
        _LOGGER.debug("Logging in to Haier Evo (%s)", self.region)
        try:
            token = await self._async_post_auth(
                C.API_LOGIN, {"email": self.email, "password": self._password}
            )
        except HaierRateLimitError as err:
            # #87: 429 is not a wrong password. Back off, double every time.
            self._login_blocked_until = loop_time + err.retry_after
            self._login_backoff = min(self._login_backoff * 2, C.LOGIN_LIMIT_MAX)
            raise
        except HaierAuthError:
            self._auth_failed = True
            raise
        except HaierConnectionError:
            self._login_blocked_until = loop_time + C.LOGIN_LIMIT_PERIOD
            raise
        except HaierApiError as err:
            # An unknown 4xx on login is not a verdict on the password: retry later.
            self._login_blocked_until = loop_time + C.LOGIN_LIMIT_PERIOD
            raise HaierConnectionError(str(err)) from None
        self._login_backoff = C.LOGIN_LIMIT_429
        self._login_blocked_until = loop_time + C.LOGIN_LIMIT_PERIOD
        self._set_tokens(token)
        await self._async_save_tokens()
        _LOGGER.debug("Login ok, token valid till %s", self._token_expire)

    async def _async_refresh(self) -> None:
        _LOGGER.debug("Refreshing Haier Evo token")
        token = await self._async_post_auth(
            C.API_TOKEN_REFRESH, {"refreshToken": self._refresh_token or ""}
        )
        self._set_tokens(token)
        await self._async_save_tokens()

    async def async_get_token(self) -> str:
        async with self._lock:
            now = dt_util.utcnow()
            if self._token and self._token_expire and self._token_expire - TOKEN_MARGIN > now:
                return self._token
            if self._refresh_token and (
                self._refresh_expire is None or self._refresh_expire - TOKEN_MARGIN > now
            ):
                try:
                    await self._async_refresh()
                except (HaierTokenError, HaierAuthError, HaierApiError) as err:
                    # Only validation.refreshToken is a verdict on the token
                    # (as in 1.7): any other refusal (a bare page, a network
                    # error or a 429 raise past this) keeps the tokens, since
                    # with no password kept dropping them asks the user. With
                    # the password at hand (a config or reauth flow) signing
                    # in costs nothing, and a dead token must not survive it.
                    if not isinstance(err, HaierTokenError) and not self._password:
                        raise HaierConnectionError(f"Token refresh refused: {err}") from None
                    _LOGGER.info("Refresh token rejected (%s), signing in again", err)
                    self._clear_tokens()
                    await self._async_save_tokens()
                else:
                    return self._token  # type: ignore[return-value]
            await self.async_login()
            return self._token  # type: ignore[return-value]

    # --- data ---

    async def async_get(self, url: str) -> Any:
        """GET with the access token.

        401 renews the token and retries. A token issued seconds ago and
        refused anyway is tried once more as is after TOKEN_RETRY_DELAY s (the
        IoT backend learns new tokens with a delay, 1.7): renewing it then only
        rotates the pair again. Still 401 after that is a cloud problem, not a
        wrong password: it is raised as a connection error, so setup retries
        instead of asking the user to re-authenticate. 403 is the answer for
        stale entries of the device list: it is final and does not touch the
        token, otherwise every start rotated the token once per stale entry.
        """
        for attempt in range(3):
            token = await self.async_get_token()
            unauthorized, data = await self._async_get_once(url, token)
            if not unauthorized:
                return data
            fresh = self.token_rejected(token)
            if attempt == 2:
                break
            if fresh:
                _LOGGER.debug("A just issued token was refused, trying it again in %s s", C.TOKEN_RETRY_DELAY)
                await asyncio.sleep(C.TOKEN_RETRY_DELAY)
            else:
                _LOGGER.debug("Access token rejected, renewing it")
        raise HaierConnectionError("Access token rejected even after renewing it")

    async def _async_get_once(self, url: str, token: str) -> tuple[bool, Any]:
        """(True, None) for a 401, else (False, the JSON answer); other errors raise."""

        async def get() -> tuple[bool, Any]:
            async with self._session.get(
                url,
                headers={**self._headers(token), "Content-Type": "application/json"},
                timeout=_timeout(),
                # X-Auth-Token and Device-Id would go along to the redirect
                # target; the cloud's endpoints never redirect.
                allow_redirects=False,
            ) as resp:
                status = resp.status
                if 300 <= status < 400:
                    raise HaierConnectionError(f"HTTP {status} redirect")
                if status == 401:
                    return True, None
                if status == 429:
                    raise HaierRateLimitError(_retry_after(resp, C.LOGIN_LIMIT_429))
                if status >= 500:
                    raise HaierConnectionError(f"HTTP {status}")
                if status >= 400:
                    raise HaierApiError(f"HTTP {status}")
                try:
                    return False, await resp.json(content_type=None)
                except ValueError:
                    raise HaierConnectionError("Answer is not JSON") from None

        return await self._async_retrying(get, idempotent=True)

    async def async_get_devices_page(self) -> dict:
        data = await self.async_get(self._url(C.API_DEVICES))
        data = (data or {}).get("data") if isinstance(data, dict) else None
        if not isinstance(data, dict):
            raise HaierConnectionError("Devices page has no data")
        return data

    async def async_get_device_status(self, mac: str) -> dict:
        data = await self.async_get(C.API_STATUS.format(mac=quote(mac, safe=":")))
        if not isinstance(data, dict):
            raise HaierConnectionError("Device status is not an object")
        return data
