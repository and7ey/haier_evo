"""Auth, tokens, 401/429 handling of the REST client (#87)."""
from __future__ import annotations

import logging
from unittest.mock import patch

import pytest
from aiohttp import (
    ClientConnectorError,
    ClientError,
    ConnectionTimeoutError,
    ServerDisconnectedError,
    SocketTimeoutError,
)
from aiohttp.client_reqrep import ConnectionKey
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMockResponse

from custom_components.haier_evo import const as C  # noqa: N812
from custom_components.haier_evo.client import (
    HaierApiError,
    HaierAuthError,
    HaierClient,
    HaierConnectionError,
    HaierRateLimitError,
)

from .conftest import DEVICES_URL, EMAIL, LOGIN_URL, PASSWORD, REFRESH_URL, load_fixture

STORE_KEY = "haier_evo.test"


def make_client(hass: HomeAssistant, store: bool = True) -> HaierClient:
    return HaierClient(
        async_get_clientsession(hass),
        EMAIL,
        PASSWORD,
        "ru",
        store=Store(hass, 1, STORE_KEY) if store else None,
    )


async def test_login_saves_tokens_in_storage(hass, aioclient_mock, hass_storage):
    aioclient_mock.post(LOGIN_URL, json=load_fixture("login.json"))
    client = make_client(hass)
    assert await client.async_get_token() == "test-access-token"
    saved = hass_storage[STORE_KEY]["data"]
    assert saved["token"] == "test-access-token"
    assert saved["refreshtoken"] == "test-refresh-token"
    # login form, not JSON, exactly like the mobile app
    assert aioclient_mock.mock_calls[0][2] == {"email": EMAIL, "password": PASSWORD}


async def test_tokens_are_saved_as_1x_reads_them(hass, aioclient_mock, hass_storage):
    """A downgrade to 1.7 through HACS keeps the sign-in: the same keys in the same Store."""
    aioclient_mock.post(LOGIN_URL, json=load_fixture("login.json"))
    client = make_client(hass)
    await client.async_get_token()
    saved = hass_storage[STORE_KEY]["data"]
    # what 1.7.2's Haier.set_tokens reads
    assert {"token", "tokenexpire", "refreshtoken", "refreshexpire"} <= saved.keys()
    assert not {"token_expire", "refresh_token", "refresh_expire"} & saved.keys()
    assert saved["tokenexpire"] and saved["refreshexpire"]
    assert set(client.export_tokens()) == {"token", "tokenexpire", "refreshtoken", "refreshexpire"}


async def test_refresh_answer_without_refresh_token_keeps_the_old_one(hass, aioclient_mock, hass_storage):
    """1.7: without a password a lost refresh token is a reauth once the access token expires."""
    hass_storage[STORE_KEY] = {
        "version": 1, "key": STORE_KEY,
        "data": {"token": "old", "tokenexpire": "2000-01-01T00:00:00+03:00", "refreshtoken": "kept",
                 "refreshexpire": "2099-02-01T00:00:00+03:00"},
    }
    login = load_fixture("login.json")
    login["data"]["token"].pop("refreshToken")
    login["data"]["token"].pop("refreshExpire", None)
    aioclient_mock.post(REFRESH_URL, json=login)
    client = make_client(hass)
    await client.async_load_tokens()
    assert await client.async_get_token() == "test-access-token"
    saved = hass_storage[STORE_KEY]["data"]
    assert saved["refreshtoken"] == "kept"
    assert saved["refreshexpire"].startswith("2099-02-01")


async def test_tokens_are_reused_from_storage(hass, aioclient_mock, hass_storage):
    hass_storage[STORE_KEY] = {
        "version": 1,
        "key": STORE_KEY,
        "data": {
            "token": "stored-token",
            "token_expire": "2099-01-01T00:00:00+03:00",
            "refresh_token": "stored-refresh",
            "refresh_expire": "2099-02-01T00:00:00+03:00",
        },
    }
    client = make_client(hass)
    await client.async_load_tokens()
    assert await client.async_get_token() == "stored-token"
    assert aioclient_mock.call_count == 0


async def test_expired_token_is_refreshed(hass, aioclient_mock, hass_storage):
    hass_storage[STORE_KEY] = {
        "version": 1,
        "key": STORE_KEY,
        "data": {
            "token": "old",
            "token_expire": "2000-01-01T00:00:00+03:00",
            "refresh_token": "stored-refresh",
            "refresh_expire": "2099-02-01T00:00:00+03:00",
        },
    }
    aioclient_mock.post(REFRESH_URL, json=load_fixture("login.json"))
    client = make_client(hass)
    await client.async_load_tokens()
    assert await client.async_get_token() == "test-access-token"
    assert aioclient_mock.mock_calls[0][1].path.endswith("/auth/refresh")
    assert aioclient_mock.mock_calls[0][2] == {"refreshToken": "stored-refresh"}


async def test_rejected_refresh_falls_back_to_login(hass, aioclient_mock, hass_storage):
    hass_storage[STORE_KEY] = {
        "version": 1,
        "key": STORE_KEY,
        "data": {"token": "old", "token_expire": "2000-01-01T00:00:00+03:00", "refresh_token": "bad"},
    }
    aioclient_mock.post(
        REFRESH_URL, status=400, json={"error": {"validation": {"refreshToken": "invalid"}}}
    )
    aioclient_mock.post(LOGIN_URL, json=load_fixture("login.json"))
    client = make_client(hass)
    await client.async_load_tokens()
    assert await client.async_get_token() == "test-access-token"
    assert aioclient_mock.call_count == 2


@pytest.mark.parametrize(
    ("status", "body"),
    [(401, {"error": {"message": "Unauthorized"}}), (400, {"error": "token expired"})],
)
async def test_any_refresh_refusal_falls_back_to_login(hass, aioclient_mock, hass_storage, status, body):
    """Otherwise a dead refresh token survives reauth and loops it forever."""
    hass_storage[STORE_KEY] = {
        "version": 1,
        "key": STORE_KEY,
        "data": {"token": "old", "token_expire": "2000-01-01T00:00:00+03:00", "refresh_token": "dead"},
    }
    aioclient_mock.post(REFRESH_URL, status=status, json=body)
    aioclient_mock.post(LOGIN_URL, json=load_fixture("login.json"))
    client = make_client(hass)
    await client.async_load_tokens()
    assert await client.async_get_token() == "test-access-token"
    assert aioclient_mock.call_count == 2


@pytest.mark.parametrize(
    ("status", "body"),
    [(401, {"error": {"message": "Unauthorized"}}), (400, {"error": "token expired"})],
)
async def test_other_refresh_refusal_without_password_keeps_the_tokens(
    hass, aioclient_mock, hass_storage, status, body
):
    """As in 1.7: only validation.refreshToken drops the tokens; without a password kept
    that is a reauth, which any other 4xx of the auth endpoint must not cause."""
    hass_storage[STORE_KEY] = {
        "version": 1,
        "key": STORE_KEY,
        "data": {"token": "old", "token_expire": "2000-01-01T00:00:00+03:00", "refresh_token": "good"},
    }
    aioclient_mock.post(REFRESH_URL, status=status, json=body)
    client = HaierClient(async_get_clientsession(hass), EMAIL, None, "ru", store=Store(hass, 1, STORE_KEY))
    await client.async_load_tokens()
    with pytest.raises(HaierConnectionError):
        await client.async_get_token()
    assert aioclient_mock.call_count == 1
    assert hass_storage[STORE_KEY]["data"]["refresh_token"] == "good"  # not rewritten


async def test_rejected_refresh_token_without_password_asks_the_user(hass, aioclient_mock, hass_storage):
    hass_storage[STORE_KEY] = {
        "version": 1,
        "key": STORE_KEY,
        "data": {"token": "old", "token_expire": "2000-01-01T00:00:00+03:00", "refresh_token": "dead"},
    }
    aioclient_mock.post(REFRESH_URL, status=400, json={"error": {"validation": {"refreshToken": "expired"}}})
    client = HaierClient(async_get_clientsession(hass), EMAIL, None, "ru", store=Store(hass, 1, STORE_KEY))
    await client.async_load_tokens()
    with pytest.raises(HaierAuthError):
        await client.async_get_token()
    assert hass_storage[STORE_KEY]["data"]["refreshtoken"] is None


@pytest.mark.parametrize("status", [400, 401, 403])
async def test_bare_refresh_refusal_keeps_the_tokens(hass, aioclient_mock, hass_storage, status):
    """A proxy or maintenance page is no verdict on the refresh token: with no
    password kept, dropping the tokens would be a reauth."""
    hass_storage[STORE_KEY] = {
        "version": 1,
        "key": STORE_KEY,
        "data": {"token": "old", "token_expire": "2000-01-01T00:00:00+03:00", "refresh_token": "good"},
    }
    aioclient_mock.post(REFRESH_URL, status=status, text="<html>Forbidden</html>")
    client = HaierClient(async_get_clientsession(hass), EMAIL, None, "ru", store=Store(hass, 1, STORE_KEY))
    await client.async_load_tokens()
    with pytest.raises(HaierConnectionError):
        await client.async_get_token()
    assert aioclient_mock.call_count == 1  # no sign-in
    assert hass_storage[STORE_KEY]["data"]["refresh_token"] == "good"  # not rewritten
    assert client._refresh_token == "good"


async def test_wrong_password_is_auth_error(hass, aioclient_mock):
    aioclient_mock.post(
        LOGIN_URL, status=400, json={"error": {"validation": {"password": "Неверный пароль"}}}
    )
    client = make_client(hass, store=False)
    with pytest.raises(HaierAuthError):
        await client.async_login()
    # no more guesses with the same password until reauth (account lockout)
    with pytest.raises(HaierAuthError):
        await client.async_login()
    assert aioclient_mock.call_count == 1


async def test_missing_expiry_does_not_refresh_every_time(hass, aioclient_mock):
    token = load_fixture("login.json")
    token["data"]["token"].pop("expire", None)
    aioclient_mock.post(LOGIN_URL, json=token)
    client = make_client(hass, store=False)
    await client.async_get_token()
    await client.async_get_token()
    assert aioclient_mock.call_count == 1


async def test_429_is_not_a_wrong_password_and_backs_off(hass, aioclient_mock):
    """#87: 429 was reported as invalid credentials and retried in a loop."""
    aioclient_mock.post(LOGIN_URL, status=429, headers={"Retry-After": "120"}, text="")
    client = make_client(hass, store=False)
    with pytest.raises(HaierRateLimitError) as err:
        await client.async_login()
    assert err.value.retry_after == 120
    # the second attempt does not even hit the server
    with pytest.raises(HaierRateLimitError):
        await client.async_login()
    assert aioclient_mock.call_count == 1


async def test_server_error_is_connection_error(hass, aioclient_mock):
    aioclient_mock.post(LOGIN_URL, status=503, text="maintenance")
    with pytest.raises(HaierConnectionError):
        await make_client(hass, store=False).async_login()


async def test_network_error_is_connection_error(hass, aioclient_mock):
    aioclient_mock.post(LOGIN_URL, exc=ClientError("boom"))
    with pytest.raises(HaierConnectionError):
        await make_client(hass, store=False).async_login()


async def test_401_on_request_refreshes_and_retries(hass, aioclient_mock):
    aioclient_mock.post(LOGIN_URL, json=load_fixture("login.json"))
    aioclient_mock.post(REFRESH_URL, json=load_fixture("login.json"))
    client = make_client(hass, store=False)
    await client.async_login()
    client._token_issued -= C.TOKEN_FRESH  # issued a while ago
    answers = [
        AiohttpClientMockResponse("GET", DEVICES_URL, status=401, text=""),
        AiohttpClientMockResponse("GET", DEVICES_URL, json=load_fixture("devices_page.json")),
    ]

    async def next_answer(*args):
        return answers.pop(0)

    aioclient_mock.get(DEVICES_URL, side_effect=next_answer)
    data = await client.async_get_devices_page()
    assert "presentation" in data
    paths = [call[1].path for call in aioclient_mock.mock_calls]
    assert paths[-1].endswith("/smartHome")
    assert any(p.endswith("/auth/refresh") for p in paths)


async def test_secrets_never_logged(hass, aioclient_mock, caplog):
    caplog.set_level(logging.DEBUG, logger="custom_components.haier_evo")
    aioclient_mock.post(LOGIN_URL, json=load_fixture("login.json"))
    aioclient_mock.get(DEVICES_URL, json=load_fixture("devices_page.json"))
    client = make_client(hass, store=False)
    await client.async_get_devices_page()
    assert PASSWORD not in caplog.text
    assert "test-access-token" not in caplog.text
    assert "test-refresh-token" not in caplog.text


def _answers(url: str, aioclient_mock, *answers):
    queue = [AiohttpClientMockResponse("GET", url, **a) for a in answers]

    async def next_answer(*args):
        return queue.pop(0)

    aioclient_mock.get(url, side_effect=next_answer)


def _paths(aioclient_mock) -> list[str]:
    return [call[1].path for call in aioclient_mock.mock_calls]


async def test_403_is_final_and_keeps_the_token(hass, aioclient_mock):
    """Stale device-list entries answer 403; that used to rotate the token."""
    aioclient_mock.post(LOGIN_URL, json=load_fixture("login.json"))
    aioclient_mock.post(REFRESH_URL, json=load_fixture("login.json"))
    aioclient_mock.get(DEVICES_URL, status=403, text="")
    client = make_client(hass, store=False)
    await client.async_login()
    with pytest.raises(HaierApiError):
        await client.async_get_devices_page()
    paths = _paths(aioclient_mock)
    assert not any(p.endswith("/auth/refresh") for p in paths)
    assert sum(p.endswith("/smartHome") for p in paths) == 1


async def test_401_for_a_token_just_issued_retries_it_after_a_pause(hass, aioclient_mock):
    """1.7: the IoT backend learns new tokens with a delay; renewing would only rotate the pair."""
    aioclient_mock.post(LOGIN_URL, json=load_fixture("login.json"))
    aioclient_mock.post(REFRESH_URL, json=load_fixture("login.json"))
    _answers(DEVICES_URL, aioclient_mock, {"status": 401, "text": ""}, {"json": load_fixture("devices_page.json")})
    client = make_client(hass, store=False)
    await client.async_login()
    with patch("custom_components.haier_evo.client.asyncio.sleep") as sleep:
        assert "presentation" in await client.async_get_devices_page()
    sleep.assert_awaited_once_with(C.TOKEN_RETRY_DELAY)
    assert [p.rsplit("/", 1)[-1] for p in _paths(aioclient_mock)] == ["sign-in", "smartHome", "smartHome"]
    tokens = [call[3]["X-Auth-Token"] for call in aioclient_mock.mock_calls if call[3] and "smartHome" in str(call[1])]
    assert tokens == ["test-access-token", "test-access-token"]


async def test_refreshed_token_rejected_is_tried_again_not_renewed(hass, aioclient_mock):
    """401 → refresh; the fresh token refused too gets a pause and a second try, no third pair."""
    aioclient_mock.post(LOGIN_URL, json=load_fixture("login.json"))
    aioclient_mock.post(REFRESH_URL, json=load_fixture("login.json"))
    _answers(
        DEVICES_URL, aioclient_mock,
        {"status": 401, "text": ""},
        {"status": 401, "text": ""},
        {"json": load_fixture("devices_page.json")},
    )
    client = make_client(hass, store=False)
    await client.async_login()
    client._token_issued -= C.TOKEN_FRESH
    assert "presentation" in await client.async_get_devices_page()
    assert [p.rsplit("/", 1)[-1] for p in _paths(aioclient_mock)] == [
        "sign-in", "smartHome", "refresh", "smartHome", "smartHome"
    ]


@pytest.mark.parametrize("error", [ClientConnectorError(ConnectionKey("h", 443, True, True, None, None, None),
                                                        OSError("no SYN-ACK")),
                                   ConnectionTimeoutError("connect timeout")])
async def test_connect_errors_are_retried_three_times(hass, aioclient_mock, error):
    """1.7: ~20% of SYNs to iot-platform get no answer; urllib3 retried connects 3 times."""
    aioclient_mock.post(LOGIN_URL, json=load_fixture("login.json"))
    aioclient_mock.get(DEVICES_URL, exc=error)
    client = make_client(hass, store=False)
    with pytest.raises(HaierConnectionError):
        await client.async_get_devices_page()
    assert sum("smartHome" in str(c[1]) for c in aioclient_mock.mock_calls) == 1 + C.API_CONNECT_RETRIES


async def test_connect_error_of_the_sign_in_is_retried(hass, aioclient_mock):
    """Not connected, the form never left: safe to repeat for a POST too (as urllib3 did)."""
    error = ConnectionTimeoutError("connect timeout")
    aioclient_mock.post(LOGIN_URL, exc=error)
    client = make_client(hass, store=False)
    with pytest.raises(HaierConnectionError):
        await client.async_login()
    assert aioclient_mock.call_count == 1 + C.API_CONNECT_RETRIES


@pytest.mark.parametrize("error", [ServerDisconnectedError(), SocketTimeoutError("read timeout")])
async def test_dropped_answer_of_a_get_is_retried_twice(hass, aioclient_mock, error):
    aioclient_mock.post(LOGIN_URL, json=load_fixture("login.json"))
    _answers(DEVICES_URL, aioclient_mock, {"exc": error}, {"exc": error},
             {"json": load_fixture("devices_page.json")})
    client = make_client(hass, store=False)
    assert "presentation" in await client.async_get_devices_page()
    aioclient_mock.clear_requests()
    aioclient_mock.post(LOGIN_URL, json=load_fixture("login.json"))
    aioclient_mock.get(DEVICES_URL, exc=error)
    with pytest.raises(HaierConnectionError):
        await client.async_get_devices_page()
    assert aioclient_mock.call_count == 1 + C.API_READ_RETRIES


async def test_dropped_answer_of_the_sign_in_is_not_repeated(hass, aioclient_mock):
    """The form may have reached the server: a second sign-in risks 429 (#87)."""
    aioclient_mock.post(LOGIN_URL, exc=ServerDisconnectedError())
    client = make_client(hass, store=False)
    with pytest.raises(HaierConnectionError):
        await client.async_login()
    assert aioclient_mock.call_count == 1


async def test_timeouts_are_those_of_1_7(hass, aioclient_mock):
    aioclient_mock.post(LOGIN_URL, json=load_fixture("login.json"))
    aioclient_mock.get(DEVICES_URL, json=load_fixture("devices_page.json"))
    client = make_client(hass, store=False)
    session = client._session
    with patch.object(session, "get", wraps=session.get) as get:
        await client.async_get_devices_page()
    timeout = get.call_args.kwargs["timeout"]
    assert (timeout.sock_connect, timeout.sock_read) == (5, 15)


async def test_token_rejected_after_login_is_a_connection_error(hass, aioclient_mock):
    aioclient_mock.post(LOGIN_URL, json=load_fixture("login.json"))
    aioclient_mock.post(REFRESH_URL, json=load_fixture("login.json"))
    aioclient_mock.get(DEVICES_URL, status=401, text="")
    client = make_client(hass, store=False)
    await client.async_login()
    client._login_blocked_until = 0  # the test does not wait out the login throttle
    with pytest.raises(HaierConnectionError):
        await client.async_get_devices_page()
    assert not client._auth_failed


async def test_stale_rejection_keeps_a_newer_token(hass, aioclient_mock):
    """A request in flight with the old token must not drop a just refreshed one."""
    aioclient_mock.post(LOGIN_URL, json=load_fixture("login.json"))
    client = make_client(hass, store=False)
    await client.async_login()
    client.invalidate_access_token("an-older-token")
    assert await client.async_get_token() == "test-access-token"
    assert aioclient_mock.call_count == 1


@pytest.mark.parametrize("status", [401, 403])
async def test_bare_401_403_on_sign_in_is_not_a_wrong_password(hass, aioclient_mock, status):
    """A proxy or maintenance page is no reason for reauth."""
    aioclient_mock.post(LOGIN_URL, status=status, text="<html>Forbidden</html>")
    client = make_client(hass, store=False)
    with pytest.raises(HaierConnectionError):
        await client.async_login()
    assert not client._auth_failed


async def test_json_401_on_sign_in_is_a_wrong_password(hass, aioclient_mock):
    aioclient_mock.post(LOGIN_URL, status=401, json={"error": {"message": "Unauthorized"}})
    with pytest.raises(HaierAuthError):
        await make_client(hass, store=False).async_login()
