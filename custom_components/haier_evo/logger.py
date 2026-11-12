import logging
import re

# The level is left to the `logger:` section of configuration.yaml. It used to
# be forced to INFO here, which silently ignored `custom_components.haier_evo: debug`.
_LOGGER = logging.getLogger("custom_components.haier_evo")

# MAC addresses keep the vendor part and the last byte: enough to tell one's
# own units apart in a log, not enough to identify a unit in a log pasted into
# an issue. Debug lines carried them in full ("Received WSS message: ...").
_MAC = re.compile(
    r"\b([0-9A-Fa-f]{2})([:-])([0-9A-Fa-f]{2})\2([0-9A-Fa-f]{2})\2"
    r"[0-9A-Fa-f]{2}\2[0-9A-Fa-f]{2}\2([0-9A-Fa-f]{2})\b"
)
_MAC_URL = re.compile(
    r"\b([0-9a-f]{2})%3a([0-9a-f]{2})%3a([0-9a-f]{2})%3a[0-9a-f]{2}%3a[0-9a-f]{2}%3a([0-9a-f]{2})\b",
    re.IGNORECASE,
)


def mask_macs(text: str) -> str:
    """The text with MAC addresses masked: 00:00:5e:**:**:01."""
    text = _MAC.sub(r"\1\2\3\2\4\2**\2**\2\5", text)
    return _MAC_URL.sub(r"\1%3A\2%3A\3%3A**%3A**%3A\4", text)


class _MaskMacs(logging.Filter):
    """Masks MAC addresses in every line the integration logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a broken format is logging's business
            return True
        masked = mask_macs(message)
        if masked != message:
            record.msg, record.args = masked, None
        return True


if not any(isinstance(f, _MaskMacs) for f in _LOGGER.filters):
    _LOGGER.addFilter(_MaskMacs())
