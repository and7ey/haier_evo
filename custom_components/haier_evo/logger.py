
import logging


# websocket-client reports lost connections and callback errors as warnings/errors
logging.getLogger("websocket").setLevel(logging.WARNING)
_LOGGER = logging.getLogger("custom_components.haier_evo")
# INFO by default, but keep a level configured by the user (logger: in configuration.yaml)
if _LOGGER.level == logging.NOTSET:
    _LOGGER.setLevel(logging.INFO)
