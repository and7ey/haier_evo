from __future__ import annotations
import voluptuous as vol
from typing import Any
from homeassistant import config_entries, exceptions
from homeassistant.core import HomeAssistant
from homeassistant.core import callback
from .const import DOMAIN
from .logger import _LOGGER


REGIONS = ["ru", "kz", "by"]
DATA_SCHEMA = vol.Schema({
    vol.Required("email"): str,
    vol.Required("password"): str,
    vol.Required("region", default="ru"): vol.In(REGIONS),
})


async def validate_input(hass: HomeAssistant, data: dict) -> dict[str, Any]:
    if len(data["email"]) < 3:
        raise InvalidEmail
    if len(data["password"]) < 3:
        raise InvalidPassword
    if data["region"] not in REGIONS:
        raise InvalidRegion
    return {"title": f'{data["email"]} ({data["region"]})'}


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_CLOUD_POLL

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                info = await validate_input(self.hass, user_input)
                return self.async_create_entry(title=info["title"], data=user_input)
            except InvalidEmail:
                errors["email"] = "invalid_email"
            except InvalidPassword:
                errors["password"] = "invalid_password"
            except InvalidRegion:
                errors["region"] = "invalid_region"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
        return self.async_show_form(
            step_id="user",
            data_schema=DATA_SCHEMA,
            errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return OptionsFlowHandler(config_entry)


class OptionsFlowHandler(config_entries.OptionsFlow):

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self.config_entry = config_entry

    async def async_step_init(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                info = await validate_input(self.hass, user_input)
                self.hass.config_entries.async_update_entry(
                    self.config_entry,
                    title=info["title"],
                    data={**self.config_entry.data, **user_input},
                )
                return self.async_create_entry(title="", data={})
            except InvalidEmail:
                errors["email"] = "invalid_email"
            except InvalidPassword:
                errors["password"] = "invalid_password"
            except InvalidRegion:
                errors["region"] = "invalid_region"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
        current = self.config_entry.data
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required("email", default=current.get("email", "")): str,
                vol.Required("password", default=current.get("password", "")): str,
                vol.Required("region", default=current.get("region", "ru")): vol.In(REGIONS),
            }),
            errors=errors
        )


class InvalidEmail(exceptions.HomeAssistantError):
    """Error to indicate we cannot connect."""

class InvalidPassword(exceptions.HomeAssistantError):
    """Error to indicate there is an invalid hostname."""

class InvalidRegion(exceptions.HomeAssistantError):
    """Error to indicate there is an invalid region."""
