"""Diagnostics: replaces the unauthenticated /api/haier_evo endpoint (#100).

Users attach the file to public issues, so a value is rather hidden than not:
credentials and identifiers go by the keys the cloud uses for them, by key
patterns for keys it may use one day, and by the form of a value (MAC, IP,
e-mail, phone, JSON web token, link queries), as in 1.7.1.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from homeassistant.components.diagnostics import REDACTED, async_redact_data
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant

from .const import CONF_TOKENS

TO_REDACT = {
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_TOKENS,
    "token",
    "accessToken",
    "refreshToken",
    "refresh_token",
    "refreshtoken",
    "device_id",
    "device_mac",
    "macAddress",
    "mac",
    "deviceId",
    "iotDeviceId",
    "device_serial",
    "serialNumber",
    "serialNum",
    "link",
    "unique_id",
    "ip",
}
# The entry title is the account email; inside the backend data "title" is
# only a UI label and is worth keeping.
TO_REDACT_ENTRY = TO_REDACT | {"title"}

# Keys the cloud does not use today but may (sharing, Wi-Fi, the place of the
# home): their values are hidden as well, whatever the case of the key.
_KEY = re.compile(
    r"^(?:.*e-?mail.*|.*phone.*|login|.*user_?id|.*owner.*|.*b?ssid|lat|lon|latitude|longitude"
    r"|.*address|.*token.*|.*secret.*|.*passw.*|.*serial.*|sn|imei|.*mac|ip)$",
    re.IGNORECASE,
)
_HEX = "[0-9A-Fa-f]"
_SCRUB: list[tuple[re.Pattern[str], str | Callable[[re.Match[str]], str]]] = [
    # Icon links are presigned storage URLs: the query carries a credential
    # and a signature that have nothing to do with debugging.
    (re.compile(r"(https?://[^\s?\"'<>#]+)\?[^\s\"'<>#]*", re.IGNORECASE), r"\1"),
    # identifiers inside deep links ("haierevo://...?macAddress=...") and texts
    (re.compile(r"((?:serialNum|serialNumber|iotDeviceId|deviceId|macAddress|mac|ip)=)[^&\s\"']+"),
     r"\1" + REDACTED),
    # MACs: colons, dashes or %3A between the groups, or 12 hex digits alone
    # (not the last group of a UUID)
    (re.compile(rf"\b{_HEX}{{2}}(?:(?::|-|%3[Aa]){_HEX}{{2}}){{5}}\b"), REDACTED),
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


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry) -> dict[str, Any]:
    entry_data = _scrub(async_redact_data(entry.as_dict(), TO_REDACT_ENTRY))
    # No runtime_data when setup failed, which is exactly when diagnostics matter.
    haier = getattr(entry, "runtime_data", None)
    if haier is None:
        return {"entry": entry_data, "haier": None}
    return {
        "entry": entry_data,
        "haier": _scrub(async_redact_data(haier.to_dict(), TO_REDACT)),
    }
