"""The config flow and re-authentication through Home Assistant's flow manager."""
from __future__ import annotations

import socket

import pytest
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.haier_evo import api
from custom_components.haier_evo import const as C

from .conftest import DOMAIN, EMAIL, PASSWORD, FakeCloud, store_key

USER_INPUT = {"email": f"  {EMAIL}  ", "password": PASSWORD, "region": "ru"}


async def _start(hass: HomeAssistant) -> dict:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return result


async def test_sign_in_creates_an_entry_without_the_password(
    hass: HomeAssistant, cloud: FakeCloud, hass_storage
) -> None:
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    entry = result["result"]
    assert entry.unique_id == f"{EMAIL}_ru"
    assert entry.title == f"{EMAIL} (ru)"
    assert entry.state is ConfigEntryState.LOADED
    # the tokens went to storage at the first start, the password nowhere
    assert dict(entry.data) == {"email": EMAIL, "region": "ru"}
    assert PASSWORD not in str(hass_storage)
    assert hass_storage[store_key(entry)]["data"]["refreshtoken"] in cloud.refresh
    assert len(cloud.seen("sign-in")) == 1


async def test_wrong_password(hass: HomeAssistant, cloud: FakeCloud) -> None:
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, "password": "wrong"})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}
    # the form keeps what was typed, except the password
    suggested = {str(k): k.description.get("suggested_value") for k in result["data_schema"].schema
                 if k.description}
    assert suggested.get("password") is None
    assert suggested.get("email") == USER_INPUT["email"]

    # and the next try with the right one goes through
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()


async def test_too_many_requests_is_not_a_wrong_password(hass: HomeAssistant, cloud: FakeCloud) -> None:
    cloud.plan("sign-in", (429, {"error": "Too Many Requests"}))
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "too_many_requests"}


async def test_cloud_unreachable(hass: HomeAssistant, cloud: FakeCloud, monkeypatch: pytest.MonkeyPatch) -> None:
    with socket.socket() as probe:  # a port nobody listens on
        probe.bind(("127.0.0.1", 0))
        closed = probe.getsockname()[1]
    monkeypatch.setattr(C, "API_PATH", f"http://127.0.0.1:{closed}")
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_same_account_twice_is_refused(hass: HomeAssistant, cloud: FakeCloud, make_entry) -> None:
    make_entry()
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, "email": EMAIL.upper()})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert not cloud.seen("sign-in"), "no sign-in for an account that is already there"


async def test_reauth_with_a_new_password(hass: HomeAssistant, cloud: FakeCloud, make_entry, hass_storage) -> None:
    """The cloud refused the saved sign-in; the user types the password again."""
    entry = make_entry()
    tokens = cloud.tokens(minutes=-10)
    tokens["refreshtoken"] = "ref-revoked"
    hass_storage[store_key(entry)] = {"version": 1, "minor_version": 1, "key": store_key(entry), "data": tokens}
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    [flow] = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert flow["step_id"] == "reauth_confirm"

    cloud.password = "new-password"
    result = await hass.config_entries.flow.async_configure(flow["flow_id"], {"password": "wrong"})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}

    result = await hass.config_entries.flow.async_configure(flow["flow_id"], {"password": "new-password"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert "password" not in entry.data and "tokens" not in entry.data
    assert hass_storage[store_key(entry)]["data"]["refreshtoken"] in cloud.refresh
    # the sign-in of the reauth carried the entry's own Device-Id
    [sign_in] = [r for r in cloud.seen("sign-in") if r["form"].get("password") == "new-password"]
    assert sign_in["headers"]["Device-Id"] == api.entry_device_id(entry.entry_id)
