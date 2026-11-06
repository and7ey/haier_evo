"""Diagnostics (Settings → Devices & services → Haier Evo → Download diagnostics).

Replaces the debug HTTP endpoint removed in 1.2.0. Home Assistant serves it
to administrators only; credentials, tokens and identifiers are redacted.
"""
from __future__ import annotations
import re
from typing import Any
from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from .const import DOMAIN

REDACTED = "**REDACTED**"
TO_REDACT = {
    "email", "password", "tokens", "token", "refreshtoken", "accessToken", "refreshToken",
    "serialNumber", "serialNum", "device_serial", "macAddress", "device_mac", "device_id",
    "iotDeviceId", "ip",
}
_SCRUB = [
    # identifiers inside deep links and texts
    (re.compile(r"\b[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}\b"), REDACTED),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), REDACTED),
    (re.compile(r"((?:serialNum|serialNumber|iotDeviceId|deviceId|macAddress|ip)=)[^&\s\"']+"), r"\1" + REDACTED),
    # signed storage links (icons) carry credentials in the query
    (re.compile(r"(X-Amz-(?:Credential|Signature|Security-Token)=)[^&\s\"']+"), r"\1" + REDACTED),
]


def _scrub(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    if isinstance(value, str):
        for pattern, repl in _SCRUB:
            value = pattern.sub(repl, value)
    return value


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    haier = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    data = {
        "entry": {"title": REDACTED, "data": dict(entry.data)},
        "integration": haier.to_dict() if haier is not None else None,
    }
    return _scrub(async_redact_data(data, TO_REDACT))
