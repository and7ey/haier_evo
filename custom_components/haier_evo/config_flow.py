"""Config flow: sign in once, keep only the tokens (no password in the entry).

The password is used to get tokens and is not stored, as in 1.2+.
When the cloud rejects the saved refresh token, Home Assistant asks for the
password again (reauth).
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from . import entry_device_id
from .client import (
    HaierAuthError,
    HaierClient,
    HaierConnectionError,
    HaierError,
    HaierRateLimitError,
)
from .const import CONF_LAUNDRY_CONTROL, CONF_REGION, CONF_TOKENS, DOMAIN, REGIONS
from .logger import _LOGGER

# autocomplete lets password managers fill the form (idea of 1.2)
EMAIL = TextSelector(TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username"))
PASSWORD = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password"))
REGION = SelectSelector(SelectSelectorConfig(options=REGIONS))


def _schema(defaults: Mapping[str, Any]) -> vol.Schema:
    return vol.Schema({
        vol.Required(CONF_EMAIL, default=defaults.get(CONF_EMAIL, "")): EMAIL,
        vol.Required(CONF_PASSWORD): PASSWORD,
        vol.Required(CONF_REGION, default=defaults.get(CONF_REGION, "ru")): REGION,
    })


def _entry_data(data: Mapping[str, Any], tokens: dict) -> dict[str, Any]:
    """What an entry keeps: the account and, until its first setup, the tokens."""
    return {
        CONF_EMAIL: str(data[CONF_EMAIL]).strip(),
        CONF_REGION: data.get(CONF_REGION) or "ru",
        CONF_TOKENS: tokens,
    }


class HaierEvoConfigFlow(ConfigFlow, domain=DOMAIN):
    """Login is checked against the cloud before the entry is saved.

    Credentials are changed with "Reconfigure", and a rejected sign-in starts
    the reauth flow. The options hold one setting: the control of washers and
    dryers (1.7). The options flow up to 0.3.15 assigned
    self.config_entry and broke on HA 2025.12+ (#96); this one does not.
    """

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlowWithReload:
        return HaierEvoOptionsFlow()

    async def _async_sign_in(
        self, data: Mapping[str, Any], errors: dict[str, str], entry_id: str | None = None
    ) -> dict | None:
        """Tokens of a fresh sign-in, or None with the reason in errors."""
        email = str(data[CONF_EMAIL]).strip()
        if len(email) < 3 or "@" not in email:
            errors[CONF_EMAIL] = "invalid_email"
            return None
        # Reauth and reconfigure sign in as the entry itself: the tokens handed
        # over to it then match its Device-Id.
        client = HaierClient(
            async_get_clientsession(self.hass),
            email,
            data[CONF_PASSWORD],
            data[CONF_REGION],
            device_id=entry_device_id(entry_id) if entry_id else None,
        )
        try:
            await client.async_login()
        except HaierAuthError:
            errors["base"] = "invalid_auth"
        except HaierRateLimitError:
            errors["base"] = "too_many_requests"
        except HaierConnectionError:
            errors["base"] = "cannot_connect"
        except HaierError:
            errors["base"] = "unknown"
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Unexpected error while checking Haier Evo login")
            errors["base"] = "unknown"
        return None if errors else client.export_tokens()

    @staticmethod
    def _unique_id(data: Mapping[str, Any]) -> str:
        return f"{str(data[CONF_EMAIL]).strip().lower()}_{data[CONF_REGION]}"

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input[CONF_EMAIL] = str(user_input[CONF_EMAIL]).strip()
            await self.async_set_unique_id(self._unique_id(user_input))
            self._abort_if_unique_id_configured()
            tokens = await self._async_sign_in(user_input, errors)
            if tokens is not None:
                return self.async_create_entry(
                    title=f"{user_input[CONF_EMAIL]} ({user_input[CONF_REGION]})",
                    data=_entry_data(user_input, tokens),
                )
        return self.async_show_form(
            step_id="user",
            data_schema=_schema(user_input or {}),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**entry.data, CONF_PASSWORD: user_input[CONF_PASSWORD]}
            data.setdefault(CONF_REGION, "ru")
            tokens = await self._async_sign_in(data, errors, entry.entry_id)
            if tokens is not None:
                return self.async_update_reload_and_abort(entry, data=_entry_data(data, tokens))
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): PASSWORD}),
            description_placeholders={"email": entry.data.get(CONF_EMAIL, "")},
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input[CONF_EMAIL] = str(user_input[CONF_EMAIL]).strip()
            await self.async_set_unique_id(self._unique_id(user_input))
            self._abort_if_unique_id_mismatch(reason="wrong_account")
            tokens = await self._async_sign_in(user_input, errors, entry.entry_id)
            if tokens is not None:
                return self.async_update_reload_and_abort(
                    entry,
                    title=f"{user_input[CONF_EMAIL]} ({user_input[CONF_REGION]})",
                    data=_entry_data(user_input, tokens),
                )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_schema(user_input or entry.data),
            errors=errors,
        )


class HaierEvoOptionsFlow(OptionsFlowWithReload):
    """Settings of an entry: the control of washers and dryers (beta), off by
    default. Saving them reloads the integration."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                data={CONF_LAUNDRY_CONTROL: bool(user_input.get(CONF_LAUNDRY_CONTROL))}
            )
        schema = vol.Schema({vol.Required(CONF_LAUNDRY_CONTROL, default=False): bool})
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(schema, dict(self.config_entry.options)),
        )
