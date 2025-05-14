"""
Config parser for Haier Evo devices.
"""

import logging
from os.path import dirname, exists, join
from homeassistant.util.yaml import load_yaml
import custom_components.haier_evo.devices as config_dir


_LOGGER = logging.getLogger(__name__)


class DeviceConfig:
    """Representation of a device config."""

    def __init__(self, fname):
        """Initialize the device config.
        Args:
            fname (string): The filename of the yaml config to load."""
        _CONFIG_DIR = dirname(config_dir.__file__)
        self._fname = fname
        filename = join(_CONFIG_DIR, fname) + '.yaml'
        if not exists(filename):
            filename = join(_CONFIG_DIR, 'default') + '.yaml'
        self._config = load_yaml(filename)
        _LOGGER.debug("Loaded device config %s", fname)

    def get_command_name(self):
        return self._config['command_name']

    def get_name_by_id(self, id):
        attributes = self._config['attributes']
        for attr in attributes:
            if attr.get('id') == id:
                return attr.get('name')
        return None

    def get_id_by_name(self, name):
        attributes = self._config['attributes']
        for attr in attributes:
            if attr.get('name') == name:
                return attr.get('id')
        return None

    def get_value_from_mappings(self, id, haier_value):
        attributes = self._config['attributes']
        for attr in attributes:
            if attr.get('id') == id:
                mappings = attr.get('mappings')
                for mapping in mappings:
                    if mapping.get('haier') == haier_value:
                        return mapping.get('value')
        return None

    def get_haier_code_from_mappings(self, id, value):
        attributes = self._config['attributes']
        for attr in attributes:
            if attr.get('id') == id:
                mappings = attr.get('mappings')
                for mapping in mappings:
                    if mapping.get('value') == value:
                        return mapping.get('haier')
        return None
