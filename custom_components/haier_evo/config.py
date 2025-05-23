"""
Config parser for Haier Evo devices.
"""

import shutil
from os.path import dirname, exists, join
from homeassistant.util.yaml import load_yaml
from .logger import _LOGGER
from . import devices as config_dir


class DeviceConfig(object):
    """Representation of a device config."""

    def __init__(self, model: str) -> None:
        """Initialize the device config.
        Args:
            model (string): The filename of the yaml config to load."""
        self._model = model
        self._config = None
        self._load_config_file()

    def _find_config_file(self) -> str:
        _CONFIG_DIR = dirname(config_dir.__file__)
        fname = join(_CONFIG_DIR, self._model) + '.yaml'
        if not exists(fname):
            fname = join(_CONFIG_DIR, 'default') + '.yaml'
        return fname

    def _load_config_file(self) -> None:
        fname = self._find_config_file()
        self._config = load_yaml(fname)
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

    def to_dict(self) -> dict:
        pass


class HaierACConfig(DeviceConfig):

    def __init__(self, model: str, userpath: str) -> None:
        self._userpath = userpath
        super().__init__(model)
        self.attrs = []
        self._command_name = self.get_command_name()

    def _find_config_file(self) -> str:
        userfname = f"{self._userpath}.{self._model}.yaml"
        fname = super()._find_config_file()
        if not exists(userfname):
            try:
                shutil.copy(fname, userfname)
            except Exception:
                pass
        return userfname if exists(userfname) else fname

    def __repr__(self):
        return (
            f"{self.__class__.__name__}("
            f"command_name={self.command_name!r},"
            f"current_temperature={self['current_temperature']!r},"
            f"target_temperature={self['target_temperature']!r},"
            f"status={self['status']!r},"
            f"mode={self['mode']!r},"
            f"fan_mode={self['fan_mode']!r},"
            f"swing_mode={self['swing_mode']!r},"
            f"swing_horizontal_mode={self['swing_horizontal_mode']!r}"
            # f"preset_mode_sleep={self['preset_mode_sleep']!r},"
            f")"
        )

    def __getitem__(self, item):
        return self.get_code_by_name(item)

    def to_dict(self) -> dict:
        return {
            "command_name": self.command_name,
            "attributes": [a.to_dict() for a in self.attrs],
        }

    @property
    def command_name(self):
        return self._command_name

    @command_name.setter
    def command_name(self, value):
        self._command_name = value

    @property
    def preset_mode(self):
        return bool(self.get_preset_modes())

    def get_command_name(self) -> str:
        return self._config.get('command_name')

    def get_haier_code(self, id_: str, value: str) -> int | None:
        attr = self.get_attr_by_name(id_)
        return {i.name: i.value for i in attr.list}.get(value) if attr else None

    def get_value(self, id_: str, haier_value: str) -> str | None:
        attr = self.get_attr_by_name(id_)
        return {str(i.value): i.name for i in attr.list}.get(haier_value) if attr else None

    def get_mapping_values(self, name: str) -> list[str]:
        attr = self.get_attr_by_name(name)
        return [i.name for i in attr.list if i.name != "unknown"] if attr else []

    def get_custom_preset_modes(self):
        try:
            modes = self._config.get('preset_modes')
            assert isinstance(modes, list)
            assert len(modes) > 0
            assert all(((m and isinstance(m, str)) for m in modes))
            return modes
        except AssertionError:
            return []

    def get_preset_modes(self):
        return self.get_custom_preset_modes() + [
            a.name.replace("preset_mode_", "")
            for a in filter(lambda a: a.name.startswith("preset_mode"), self.attrs)
        ]

    def get_attr_by_name(self, name):
        return next(filter(lambda a: a.name == name, self.attrs), None)

    def get_code_by_name(self, name):
        attr = self.get_attr_by_name(name)
        return str(attr.code) if attr else None

    @property
    def attributes(self):
        attrs = []
        for attr in (a for a in self._config.get('attributes', []) if a.get("id")):
            attrs.append(Attribute({
                "attrname": attr.get("name"),
                "description": attr.get('description'),
                "name": attr.get('id'),
                "list": {
                    "data": [{
                        "data": m.get('haier'),
                        "name": m.get('value'),
                        "attrname": m.get('value')
                    } for m in attr.get("mappings", [])]
                }
            }))
        return attrs

    def merge_attributes(self) -> None:
        attributes = {a.name: a for a in self.attributes}
        for i, attr in enumerate(self.attrs[:]):
            config_attr = attributes.pop(attr.name, None)
            if config_attr and attr.name == config_attr.name:
                config_attr.description = attr.description
                config_attr.current = attr.current
                self.attrs[i] = config_attr
        self.attrs.extend(attributes.values())
        self.attrs.sort(key=lambda a: a.code)
        if (attr := self.get_attr_by_name("preset_mode_sleep")) and not self["quiet"]:
            attr_copy = attr.copy()
            attr_copy.update(attrname="quiet")
            self.attrs.append(Attribute(attr_copy))
        elif (attr := self.get_attr_by_name("quiet")) and not self["preset_mode_sleep"]:
            attr_copy = attr.copy()
            attr_copy.update(attrname="preset_mode_sleep")
            self.attrs.append(Attribute(attr_copy))
        if (attr := self.get_attr_by_name("preset_mode_boost")) and not self["turbo"]:
            attr_copy = attr.copy()
            attr_copy.update(attrname="turbo")
            self.attrs.append(Attribute(attr_copy))
        elif (attr := self.get_attr_by_name("turbo")) and not self["preset_mode_boost"]:
            attr_copy = attr.copy()
            attr_copy.update(attrname="preset_mode_boost")
            self.attrs.append(Attribute(attr_copy))
        if (attr := self.get_attr_by_name("preset_mode_comfort")) and not self["comfort"]:
            attr_copy = attr.copy()
            attr_copy.update(attrname="comfort")
            self.attrs.append(Attribute(attr_copy))
        elif (attr := self.get_attr_by_name("comfort")) and not self["preset_mode_comfort"]:
            attr_copy = attr.copy()
            attr_copy.update(attrname="preset_mode_comfort")
            self.attrs.append(Attribute(attr_copy))
        if (attr := self.get_attr_by_name("preset_mode_health")) and not self["health"]:
            attr_copy = attr.copy()
            attr_copy.update(attrname="health")
            self.attrs.append(Attribute(attr_copy))

    def get_command_by_name(self, name: str) -> list[dict] | None:
        try:
            commands = self._config.get('commands') or {}
            return commands.get(name)
        except Exception:
            return None


class Attribute(dict):

    def __init__(self, data):
        super().__init__(data)
        self.name = {
            "Режимы": "mode",
            "Целевая температура": "target_temperature",
            "Температура в комнате": "current_temperature",
            "Скорость вентилятора": "fan_mode",
            "Включение/выключение": "status",
            "Горизонтальные жалюзи": "swing_horizontal_mode",
            "Вертикальные жалюзи": "swing_mode",
            "Комфортный сон": "preset_mode_comfort",
            "Тихий": "preset_mode_sleep",
            "Турбо": "preset_mode_boost",
            "Здоровый режим": "health",
            "Звуковой сигнал": "sound",
            "Подсветка блока": "light",
            "10 градусов": "antifreeze",
            "Эко-датчик": "eco_sensor",
            "Стерильная очистка": "cleaning",
            "Авто влажность": "autohumidity",
        }.get(data.get("attrname", self.description), data.get("attrname") or "unknown")

    def __repr__(self):
        return (
            f"{self.__class__.__name__}("
            f"{self.name}({self.code}),"
            f"desc={self.description!r},"
            f"current={self.current!r},"
            f"range={self.range!r},"
            f"list={self.list!r}"
            f")"
        )

    def to_dict(self) -> dict:
        _range = self.range
        return {
            "name": self.name,
            "code": self.code,
            "description": self.description,
            "current": self.current,
            "range": _range.to_dict() if _range else None,
            "list": [l.to_dict() for l in self.list],
        }

    @property
    def description(self):
        return (self.get("description") or "").strip()

    @description.setter
    def description(self, value):
        self["description"] = value

    @property
    def code(self) -> int | None:
        try:
            return int(self.get("name"))
        except Exception:
            return -1

    @property
    def current(self):
        return self.get("currentValue")

    @current.setter
    def current(self, value):
        self["currentValue"] = value

    @property
    def command_name(self):
        return self.get("commandName")

    @property
    def type(self):
        return self.get("type", "").lower()

    @property
    def list(self):
        data = self.get("list", {}).get("data")
        return [Item.create(self.name, v) for v in data] if data else []

    @property
    def range(self):
        value = self.get("range")
        return Range(value) if value else None

    @range.setter
    def range(self, value):
        self["range"] = value


class Range(dict):

    def __repr__(self):
        return (
            f"["
            f"min={self.min_value},"
            f"max={self.max_value},"
            f"step={self.step}"
            f"]"
        )

    def to_dict(self) -> dict:
        return {
            "min": self.min_value,
            "max": self.max_value,
            "step": self.step,
        }

    @property
    def type(self):
        return self.get("type")

    @property
    def data(self):
        return self.get("data", {})

    @property
    def min_value(self):
        return self.data.get("minValue")

    @property
    def max_value(self):
        return self.data.get("maxValue")

    @property
    def step(self):
        return self.data.get("step")


class Item(dict):
    mappings = {
        "Выключен": "off",
        "Активен": "on",
    }

    def __init__(self, data):
        super().__init__(data)
        self.name = self.mappings.get(self.description, data.get("attrname") or "unknown")

    def __repr__(self):
        return (
            f"{self.name}("
            f"{self.value},"
            f"desc={self.description!r}"
            f")"
        )

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "value": self.value,
        }

    @property
    def description(self):
        return (self.get("name") or "").strip()

    @property
    def value(self):
        value = self.get("data")
        return {
            "true": "1",
            "false": "0",
        }.get(value, value)

    @classmethod
    def create(cls, name, data):
        if name == "mode":
            return Mode(data)
        if name == "fan_mode":
            return FanMode(data)
        if name == "swing_horizontal_mode":
            return SwingHorizontalMode(data)
        if name == "swing_mode":
            return SwingMode(data)
        if name == "eco_sensor":
            return EcoSensor(data)
        return cls(data)


class Mode(Item):
    mappings = {
        "Авто": "auto",
        "Охлаждение": "cool",
        "Нагрев": "heat",
        "Вентилятор": "fan_only",
        "Осушение": "dry",
    }


class FanMode(Item):
    mappings = {
        "Быстрый режим": "high",
        "Средний режим": "medium",
        "Медленный режим": "low",
        "Автоматический режим": "auto",
    }


class SwingHorizontalMode(Item):
    mappings = {
        "Исходная позиция": "position_1",
        "Второе положение поворота": "position_2",
        "Третье положение поворота": "position_3",
        "Четвертая позиция поворота": "position_4",
        "Пятая позиция поворота": "position_5",
        "Шестое положение поворота": "position_6",
        "Седьмая позиция поворота": "position_7",
        "Авто режим": "auto",
    }


class SwingMode(Item):
    mappings = {
        "Фиксированное верхнее и нижнее положение": "off",
        "Верхнее положение": "upper",
        "Первое положение поворота": "position_1",
        "Нижнее положение": "bottom",
        "Второе положение поворота": "position_2",
        "Третье положение поворота": "position_3",
        "Вверх и вниз четвертая позиция": "position_4",
        "Пятая позиция поворота": "position_5",
        "Авто режим": "auto",
        "Автоматический подъем и опускание (только для специальной модели)": "special",
    }


class EcoSensor(Item):
    mappings = {
        "Выключен": "off",
        "Обводящий": "outlining",
        "Сопутствующий": "related",
        "Активен": "on",
    }