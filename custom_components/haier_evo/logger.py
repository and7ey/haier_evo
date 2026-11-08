import logging
import re


# websocket-client reports lost connections and callback errors as warnings/errors
logging.getLogger("websocket").setLevel(logging.WARNING)
_LOGGER = logging.getLogger("custom_components.haier_evo")
# INFO by default, but keep a level configured by the user (logger: in configuration.yaml)
if _LOGGER.level == logging.NOTSET:
    _LOGGER.setLevel(logging.INFO)

# MAC addresses in log lines keep the vendor part and the last byte: enough to
# tell one's own devices apart, not enough to identify one in a log pasted to
# an issue (up to 1.7.0 every start logged them in full, with serial numbers)
_MAC = re.compile(r"\b([0-9A-Fa-f]{2}):([0-9A-Fa-f]{2}):([0-9A-Fa-f]{2}):[0-9A-Fa-f]{2}:[0-9A-Fa-f]{2}:([0-9A-Fa-f]{2})\b")
_MAC_URL = re.compile(r"\b([0-9a-f]{2})%3a([0-9a-f]{2})%3a([0-9a-f]{2})%3a[0-9a-f]{2}%3a[0-9a-f]{2}%3a([0-9a-f]{2})\b", re.I)


def mask_ids(text: str) -> str:
    """The text with the MAC addresses in it masked ("30:c9:22:**:**:2c")."""
    text = _MAC.sub(r"\1:\2:\3:**:**:\4", text)
    return _MAC_URL.sub(r"\1%3A\2%3A\3%3A**%3A**%3A\4", text)


class _MaskIds(logging.Filter):
    """Masks MAC addresses in every line the integration logs, the texts of
    exceptions (URLs with a MAC) included."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a broken format is logging's business
            return True
        masked = mask_ids(message)
        if masked != message:
            record.msg, record.args = masked, ()
        return True


if not any(isinstance(f, _MaskIds) for f in _LOGGER.filters):
    _LOGGER.addFilter(_MaskIds())
