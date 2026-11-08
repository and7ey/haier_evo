"""Diagnostics (Settings → Devices & services → Haier Evo → Download diagnostics).

Replaces the debug HTTP endpoint removed in 1.2.0. Home Assistant serves it
to administrators only; credentials, tokens and identifiers are redacted: by
the keys the cloud uses for them, by key patterns for ones it may use, and by
the form of a value (MAC, IP, e-mail, phone, JSON web token, link queries).
Users attach the file to public issues, so a value is rather hidden than not.
"""
from __future__ import annotations
import re
from typing import Any, Callable
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
# Keys the cloud does not use today but may (sharing, Wi-Fi, the home's
# place): their values are hidden as well, whatever the case of the key
_KEY = re.compile(
    r"^(?:.*e-?mail.*|.*phone.*|login|.*user_?id|.*owner.*|.*b?ssid|lat|lon|latitude|longitude"
    r"|.*address|.*token.*|.*secret.*|.*passw.*|.*serial.*|sn|imei|.*mac|ip)$",
    re.I,
)
_HEX = "[0-9A-Fa-f]"
_SCRUB: list[tuple[re.Pattern[str], str | Callable[[re.Match[str]], str]]] = [
    # every value in the query of a web link (signed storage links carry
    # credentials there: X-Amz-*, token=, Signature=...); the names stay
    (re.compile(r"\bhttps?://[^\s\"'<>?#]*\?[^\s\"'<>#]*", re.I),
     lambda m: re.sub(r"=[^&#]*", "=" + REDACTED, m.group(0))),
    # identifiers inside deep links and texts
    (re.compile(r"((?:serialNum|serialNumber|iotDeviceId|deviceId|macAddress|mac|ip)=)[^&\s\"']+"), r"\1" + REDACTED),
    # MAC addresses: with colons or dashes, URL-encoded, or 12 hex digits
    # alone (not the last group of a UUID)
    (re.compile(rf"\b{_HEX}{{2}}(?:[:-]{_HEX}{{2}}){{5}}\b"), REDACTED),
    (re.compile(rf"\b{_HEX}{{2}}(?:%3[Aa]{_HEX}{{2}}){{5}}\b"), REDACTED),
    (re.compile(rf"(?<![-\w]){_HEX}{{12}}(?![-\w])"), REDACTED),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), REDACTED),
    # e-mail addresses, JSON web tokens, Russian phone numbers
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), REDACTED),
    (re.compile(r"\beyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]*"), REDACTED),
    (re.compile(r"(?<![\w+])(?:\+7|8)[\s(-]*\d{3}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}(?!\d)"), REDACTED),
]


def _scrub(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: REDACTED if _KEY.match(str(k)) and v not in (None, "", [], {}) else _scrub(v)
            for k, v in value.items()
        }
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
