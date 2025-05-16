"""
Config parser for Haier Evo devices.
"""

from os.path import dirname, exists, join
from homeassistant.util.yaml import load_yaml
from .logger import _LOGGER
from . import devices as config_dir


class DeviceConfig(object):
    """Representation of a device config."""

    def __init__(self, fname: str) -> None:
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

    def get_command_name(self) -> str:
        return self._config['command_name']

    def get_name_by_id(self, id_: str) -> str | None:
        attributes = self._config['attributes']
        for attr in attributes:
            if attr.get('id') == id_:
                return attr.get('name')
        return None

    def get_id_by_name(self, name: str) -> str | None:
        attributes = self._config['attributes']
        for attr in attributes:
            if attr.get('name') == name:
                return attr.get('id')
        return None

    def get_value(self, id_: str, haier_value: int) -> str | None:
        attributes = self._config['attributes']
        for attr in attributes:
            if attr.get('id') == id_:
                mappings = attr.get('mappings')
                for mapping in mappings:
                    if mapping.get('haier') == haier_value:
                        return mapping.get('value')
        return None

    def get_mapping_values(self, name: str) -> list[str]:
        values = []
        attributes = self._config['attributes']
        for attr in attributes:
            if attr.get('name') == name:
                mappings = attr.get('mappings')
                for mapping in mappings:
                    values.append(mapping.get('value'))
                break
        return [str(v) for v in values]

    def get_haier_code(self, id_: str, value: str) -> int | None:
        attributes = self._config['attributes']
        for attr in attributes:
            if attr.get('id') == id_:
                mappings = attr.get('mappings')
                for mapping in mappings:
                    if mapping.get('value') == value:
                        return mapping.get('haier')
        return None
