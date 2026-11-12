"""Config flow: login check, errors, reauth, reconfigure (#96, #98, #87)."""
from __future__ import annotations

from unittest.mock import patch

from aiohttp import ClientError
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.haier_evo import entry_device_id
from custom_components.haier_evo.client import HaierClient
from custom_components.haier_evo.const import DOMAIN

from .conftest import EMAIL, LOGIN_URL, PASSWORD, load_fixture

USER_INPUT = {"email": f"  {EMAIL}  ", "password": PASSWORD, "region": "ru"}


def _saved_token(hass_storage, entry) -> str | None:
    if "tokens" in entry.data:
        return entry.data["tokens"]["token"]
    return hass_storage[f"{DOMAIN}.{entry.entry_id}"]["data"]["token"]


async def _start(hass: HomeAssistant):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    return result


async def test_user_flow_creates_entry(hass, aioclient_mock):
    aioclient_mock.post(LOGIN_URL, json=load_fixture("login.json"))
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == f"{EMAIL} (ru)"
    # no password in the entry: only the tokens of this sign-in, moved to .storage at setup
    data = result["data"]
    assert data.keys() == {"email", "region", "tokens"}
    assert (data["email"], data["region"]) == (EMAIL, "ru")
    assert data["tokens"]["token"] == load_fixture("login.json")["data"]["token"]["accessToken"]
    assert PASSWORD not in str(data)
    assert result["result"].unique_id == f"{EMAIL}_ru"


async def test_entry_reuses_the_flow_login(hass, mock_cloud, ws_factory):
    """One sign-in, not two in a row: the endpoint is quick to answer 429."""
    aioclient_mock = mock_cloud()
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    await ws_factory.wait_for(1)
    logins = [c for c in aioclient_mock.mock_calls if str(c[1]).endswith("/auth/sign-in")]
    assert len(logins) == 1
    entry = result["result"]
    assert "tokens" not in entry.data and "password" not in entry.data
    await hass.config_entries.async_unload(result["result"].entry_id)
    await hass.async_block_till_done()


async def test_user_flow_invalid_auth(hass, aioclient_mock):
    aioclient_mock.post(
        LOGIN_URL, status=400, json={"error": {"validation": {"password": "Неверный пароль"}}}
    )
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_user_flow_too_many_requests(hass, aioclient_mock):
    aioclient_mock.post(LOGIN_URL, status=429, text="")
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["errors"] == {"base": "too_many_requests"}


async def test_user_flow_cannot_connect(hass, aioclient_mock):
    aioclient_mock.post(LOGIN_URL, exc=ClientError())
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_flow_invalid_email(hass, aioclient_mock):
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, "email": "abc"}
    )
    assert result["errors"] == {"email": "invalid_email"}
    assert aioclient_mock.call_count == 0


async def test_user_flow_duplicate(hass, aioclient_mock, config_entry):
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_options_flow_sets_laundry_control(hass, config_entry):
    """The options hold the control of washers (1.7); the old flow answered 500 (#96)."""
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    assert config_entry.supports_options is True
    assert config_entry.supports_reconfigure is True
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"laundry_control": True})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.options == {"laundry_control": True}


async def test_reauth(hass, aioclient_mock, config_entry, hass_storage):
    aioclient_mock.post(LOGIN_URL, json=load_fixture("login.json"))
    result = await config_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    with patch.object(hass.config_entries, "async_reload", return_value=True) as reload_entry:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"password": "new-password"}
        )
        await hass.async_block_till_done()
        reload_entry.assert_awaited_once_with(config_entry.entry_id)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert "password" not in config_entry.data
    # handed over in the entry data; the reload moves them to .storage
    assert _saved_token(hass_storage, config_entry) == "test-access-token"
    assert config_entry.data["email"] == EMAIL
    # the login is made as the entry: its tokens are bound to the same Device-Id
    assert aioclient_mock.mock_calls[0][3]["Device-Id"] == entry_device_id(config_entry.entry_id)


async def test_reconfigure(hass, aioclient_mock, config_entry, hass_storage):
    aioclient_mock.post(LOGIN_URL, json=load_fixture("login.json"))
    result = await config_entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"
    with patch.object(hass.config_entries, "async_reload", return_value=True) as reload_entry:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"email": EMAIL, "password": "other-password", "region": "ru"}
        )
        await hass.async_block_till_done()
        reload_entry.assert_awaited_once_with(config_entry.entry_id)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert "password" not in config_entry.data
    # handed over in the entry data; the reload moves them to .storage
    assert _saved_token(hass_storage, config_entry) == "test-access-token"


async def test_reconfigure_other_account_is_refused(hass, aioclient_mock, config_entry):
    result = await config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"email": "other@example.com", "password": "x" * 8, "region": "ru"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"


async def test_new_sign_in_has_a_device_id_of_its_own(hass, aioclient_mock):
    """As 1.x: a random Device-Id, not one every installation shares."""
    aioclient_mock.post(LOGIN_URL, json=load_fixture("login.json"))
    result = await _start(hass)
    await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    sent = aioclient_mock.mock_calls[0][3]["Device-Id"]
    assert sent != "00000000-0000-4000-8000-000000000000"
    other = HaierClient(async_get_clientsession(hass), EMAIL, PASSWORD, "ru")
    assert other.device_id not in (sent, "00000000-0000-4000-8000-000000000000")
    await hass.async_block_till_done()
