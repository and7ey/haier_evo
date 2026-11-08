"""Config flow: sign in once, keep only the tokens (no password in the config).

The password is used to get tokens and is not stored. When the cloud rejects
the saved refresh token, Home Assistant asks for the password again (reauth).
"""
from __future__ import annotations
import voluptuous as vol
from typing import Any
from requests.exceptions import ConnectionError as RequestsConnectionError, Timeout, TooManyRedirects
from homeassistant import config_entries, exceptions
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from .const import CONF_LAUNDRY_CONTROL, DOMAIN
from .logger import _LOGGER
from . import api


REGIONS = ["ru", "kz", "by"]
EMAIL = TextSelector(TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username"))
PASSWORD = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password"))
REGION = SelectSelector(SelectSelectorConfig(options=REGIONS))

USER_SCHEMA = vol.Schema({
    vol.Required("email"): EMAIL,
    vol.Required("password"): PASSWORD,
    vol.Required("region", default="ru"): REGION,
})
REAUTH_SCHEMA = vol.Schema({vol.Required("password"): PASSWORD})
OPTIONS_SCHEMA = vol.Schema({vol.Required(CONF_LAUNDRY_CONTROL, default=False): bool})


class InvalidCredentials(exceptions.HomeAssistantError):
    """The cloud rejected the e-mail or the password."""


class CannotConnect(exceptions.HomeAssistantError):
    """The cloud could not be reached."""


class TooManyRequests(exceptions.HomeAssistantError):
    """The cloud limits sign-ins (HTTP 429): not a wrong password."""


async def sign_in(hass: HomeAssistant, email: str, password: str, region: str,
                  device_id: str | None = None) -> dict:
    """Sign in once and return the tokens; the password is not kept anywhere.

    device_id: the entry's own on re-authentication (a new entry has no id yet).
    """
    def run() -> dict:
        client = api.Haier(hass, email, password, region, device_id=device_id)
        try:
            client.login()
            return client.tokens_dict()
        finally:
            client.stop()
    try:
        return await hass.async_add_executor_job(run)
    except api.AuthUserError as e:
        raise InvalidCredentials from e
    except api.ManyRequestsError as e:
        raise TooManyRequests from e
    except api.InvalidAuth as e:
        if isinstance(e.__cause__, api.ManyRequestsError):
            raise TooManyRequests from e
        # a redirect is not followed (api.Haier._build_session): not a wrong password
        if isinstance(e.__cause__, (RequestsConnectionError, Timeout, TooManyRedirects)):
            raise CannotConnect from e
        raise InvalidCredentials from e
    except (RequestsConnectionError, Timeout, TooManyRedirects) as e:
        raise CannotConnect from e


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> config_entries.OptionsFlow:
        return OptionsFlowHandler()

    async def _try_sign_in(self, email: str, password: str, region: str, errors: dict,
                           device_id: str | None = None) -> dict | None:
        try:
            return await sign_in(self.hass, email, password, region, device_id)
        except InvalidCredentials:
            errors["base"] = "invalid_auth"
        except CannotConnect:
            errors["base"] = "cannot_connect"
        except TooManyRequests:
            errors["base"] = "too_many_requests"
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("Unexpected exception")
            errors["base"] = "unknown"
        return None

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            email = user_input["email"].strip()
            region = user_input["region"]
            await self.async_set_unique_id(f"{email.lower()}_{region}")
            self._abort_if_unique_id_configured()
            tokens = await self._try_sign_in(email, user_input["password"], region, errors)
            if tokens is not None:
                return self.async_create_entry(
                    title=f"{email} ({region})",
                    data={"email": email, "region": region, "tokens": tokens},
                )
        suggested = {k: v for k, v in (user_input or {}).items() if k != "password"}
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(USER_SCHEMA, suggested),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]):
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None):
        entry = self._get_reauth_entry()
        email = entry.data.get("email", "")
        errors: dict[str, str] = {}
        if user_input is not None:
            tokens = await self._try_sign_in(
                email, user_input["password"], entry.data.get("region", "ru"), errors,
                api.entry_device_id(entry.entry_id),
            )
            if tokens is not None:
                return self.async_update_reload_and_abort(entry, data_updates={"tokens": tokens})
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=REAUTH_SCHEMA,
            description_placeholders={"email": email},
            errors=errors,
        )


class OptionsFlowHandler(config_entries.OptionsFlowWithReload):
    """Settings of an entry: the control of washers and dryers (beta), off by
    default. Saving them reloads the integration."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            return self.async_create_entry(data={CONF_LAUNDRY_CONTROL: bool(user_input.get(CONF_LAUNDRY_CONTROL))})
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(OPTIONS_SCHEMA, dict(self.config_entry.options)),
        )
