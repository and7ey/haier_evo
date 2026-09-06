
import logging


logging.getLogger("websocket").setLevel(logging.CRITICAL)
# Do not force a level here: it would override the `logger:` setting
# in the user's configuration.yaml. Control verbosity from HA config:
# logger:
#   logs:
#     custom_components.haier_evo: debug
_LOGGER = logging.getLogger("custom_components.haier_evo")
