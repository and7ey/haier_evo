from __future__ import annotations

import filecmp
import os
import re
from os.path import dirname, exists, join
from typing import Any

from homeassistant.util.yaml import load_yaml

from . import devices as config_dir
from .logger import _LOGGER

# The model comes from the cloud and becomes a part of a file name (1.7).
SAFE_MODEL = re.compile(r"[A-Za-z0-9()_.-]{1,32}")

# The Haier app draws its louver screens from modes.itemsV2 of the device
# description: groups (auto / direction / comfort flow) with the angle or the
# side of every position. The flat attribute list only has firmware names such
# as "Второе положение поворота", so the positions are named from these
# screens (1.3).
LOUVER_SCREENS = {
    "windDirectionVertical": "swing_mode",
    "windDirectionHorizontal": "swing_horizontal_mode",
}
LOUVER_SIDES = {
    "вверх": "up",
    "вниз": "down",
    "крайнее левое": "far_left",
    "левое": "left",
    "центральная": "center",
    "центральное": "center",
    "по центру": "center",
    "правое": "right",
    "крайнее правое": "far_right",
}
LOUVER_ANGLE = re.compile(r"\d{1,3}")


def _louver_group(kind: str, body: Any) -> list[tuple[str, str | None]]:
    """(value code, key) of one group of a louver screen; key None: no name for it."""
    if not isinstance(body, dict):
        return []
    if body.get("value") not in (None, ""):
        return [(str(body["value"]), "auto" if kind == "auto" else None)]
    angles, others = [], []
    for mode in body.get("modes") or []:
        if not isinstance(mode, dict) or mode.get("value") in (None, ""):
            continue
        code = str(mode["value"])
        name = str(mode.get("name") or "").strip().lower()
        if LOUVER_ANGLE.fullmatch(name):
            angles.append((int(name), code))
        elif side := LOUVER_SIDES.get(name):
            others.append((code, f"comfort_{side}" if kind == "comfort" else side))
        else:
            others.append((code, None))
    return [(code, f"angle_{angle}") for angle, code in sorted(angles)] + others


def _screen_blocks(data: Any) -> list:
    modes = data.get("modes") if isinstance(data, dict) and isinstance(data.get("modes"), dict) else {}
    blocks = modes.get("itemsV2") or modes.get("items") or []
    return list(blocks) if isinstance(blocks, list) else []


def app_screens(data: Any) -> dict[str, str]:
    """Attributes the Haier app has a control for: {attribute code: the app's title}.

    The app builds its screens from modes.itemsV2 (louvers, cleaning, display
    light...) and top-level blocks such as muteStatus ("Тихий"). Empty when
    the description has no screens: then nothing is known to be missing, and
    everything is shown. The flat attribute list sometimes names an attribute
    otherwise, e.g. 31 "Авто влажность" is "Самоочистка" in the app.
    """
    screens: dict[str, str] = {}
    blocks = _screen_blocks(data)
    if not blocks:
        return screens
    blocks += [v for k, v in data.items() if k != "modes" and isinstance(v, dict)]
    for block in blocks:
        if not isinstance(block, dict) or not isinstance(block.get("value"), dict):
            continue
        code = block["value"].get("name")
        title = block.get("title")
        if code not in (None, "") and isinstance(title, str) and title.strip():
            screens.setdefault(str(code), title.strip())
    return screens


def louver_layout(data: Any) -> dict[str, list[tuple[str, str | None]]]:
    """Louver positions the app offers: {attribute code: [(value code, key)]} in app order."""
    layout: dict[str, list[tuple[str, str | None]]] = {}
    for item in _screen_blocks(data):
        try:
            screen = item["value"]["description"]
            attr_code = str(item["value"]["name"])
            groups = item["description"][screen]["types"]
        except (KeyError, TypeError):
            continue
        if screen not in LOUVER_SCREENS or not isinstance(groups, list):
            continue
        positions, codes, keys = [], set(), set()
        for group in groups:
            for kind, body in (group.items() if isinstance(group, dict) else ()):
                for code, key in _louver_group(kind, body):
                    if code in codes:
                        continue
                    if key in keys:
                        key = None
                    codes.add(code)
                    keys.add(key)
                    positions.append((code, key))
        if positions:
            layout[attr_code] = positions
    return layout


class HaierDeviceConfig:

    def __init__(self, model: str, userpath: str = "") -> None:
        self._model = model
        self._userpath = userpath
        self._config = {}
        self._load_config_file()
        self._command_name = self._config.get('command_name')
        self._attrs_cache = {}
        self.attrs = []
        self.constraint = Constraint([])

    def __getitem__(self, item) -> str | None:
        return self.get_code_by_name(str(item))

    @property
    def command_name(self) -> str:
        return self._command_name

    @command_name.setter
    def command_name(self, value: str) -> None:
        self._command_name = str(value)

    def _find_config_file(self) -> str:
        _CONFIG_DIR = dirname(config_dir.__file__)
        if not SAFE_MODEL.fullmatch(self._model or "") or ".." in self._model:
            _LOGGER.warning("Unexpected device model %r, using the default profile", self._model)
            return join(_CONFIG_DIR, 'default') + '.yaml'
        fname = join(_CONFIG_DIR, self._model) + '.yaml'
        if not exists(fname):
            fname = join(_CONFIG_DIR, 'default') + '.yaml'
        # A user override (/config/haier_evo.<MODEL>.yaml) still wins, but it is
        # no longer created automatically: the copy froze the bundled profile
        # forever, so fixes in new releases never reached the user.
        userfname = f"{self._userpath}.{self._model}.yaml"
        if not self._userpath or not exists(userfname):
            return fname
        try:
            if filecmp.cmp(userfname, fname, shallow=False):
                # Such an automatic copy of versions up to 0.3.15 / 1.5.2: nothing
                # of the user's is lost, and the bundled profile gets its fixes.
                os.remove(userfname)
                _LOGGER.info("Removed %s, a copy of the bundled profile", userfname)
                return fname
        except OSError as err:
            _LOGGER.warning("Failed to compare %s with the bundled profile: %s", userfname, err)
        _LOGGER.info("Using the profile %s instead of the bundled one", userfname)
        return userfname

    def _load_config_file(self) -> None:
        fname = self._find_config_file()
        try:
            self._config = load_yaml(fname) or {}
        except Exception as e:
            _LOGGER.error("Failed to load config %s: %s", fname, e)
            self._config = {}
        else:
            _LOGGER.debug("Loaded device config %s", fname)

    def to_dict(self) -> dict:
        return {
            "command_name": self.command_name,
            "attributes": [a.to_dict() for a in self.attrs],
        }

    def get_attr_by_code(self, code: str | int) -> Attribute | None:
        return next(filter(
            lambda a: str(a.code) == str(code),
            self.attrs
        ), None)

    def get_attrs_by_code(self, code: str | int) -> list[Attribute]:
        return [a for a in self.attrs if str(a.code) == str(code)]

    def get_attr_by_name(self, name: str) -> Attribute | None:
        if name in self._attrs_cache:
            return self._attrs_cache[name]
        attr = next(filter(lambda a: a.name == name, self.attrs), None)
        if attr is not None:
            self._attrs_cache[name] = attr
        return attr

    def get_value(self, name: str, code: str) -> str | None:
        attr = self.get_attr_by_name(name)
        return {str(i.value): i.name for i in attr.list}.get(str(code)) if attr else None

    def get_value_code(self, name: str, value: str) -> str | None:
        attr = self.get_attr_by_name(name)
        return {i.name: i.value for i in attr.list}.get(value) if attr else None

    def get_values(self, name: str) -> list[str]:
        attr = self.get_attr_by_name(name)
        # hidden: a position the app does not offer (louver_layout), still decoded
        return [i.name for i in attr.list if i.name != "unknown" and not i.hidden] if attr else []

    def get_code_by_name(self, name: str) -> str | None:
        attr = self.get_attr_by_name(name)
        return str(attr.code) if attr else None

    def get_command_by_name(self, name: str) -> list[dict] | None:
        try:
            commands = self._config.get('commands') or {}
            return commands.get(name)
        except Exception:
            return None

    def get_config_attributes(self) -> list[Attribute]:
        attrs = []
        for attr in (a for a in self._config.get('attributes', []) if a.get("id")):
            attrs.append(Attribute({
                "attrname": attr.get("name"),
                "description": attr.get('description'),
                "name": attr.get('id'),
                "currentValue": attr.get('current'),
                "list": {
                    "data": [{
                        "data": str(m.get('haier')),
                        "name": str(m.get('value')),
                        "attrname": m.get('value')
                    } for m in attr.get("mappings", []) if (
                        m.get('value') not in ("", None)
                        and m.get('haier') not in ("", None)
                    )],
                }
            }))
        return attrs

    def merge_attributes(self) -> None:
        attributes = {a.name: a for a in self.get_config_attributes()}
        for i, attr in enumerate(self.attrs[:]):
            config_attr = attributes.pop(attr.name, None)
            if config_attr and attr.name == config_attr.name:
                config_attr.description = attr.description
                config_attr.current = attr.current
                config_attr.range = attr.range
                config_attr.list = config_attr.list or attr.list
                self.attrs[i] = config_attr
            if attr.command_name and not self.command_name:
                self.command_name = attr.command_name
        self.attrs.extend(attributes.values())
        self.extend_attributes()
        self.attrs.sort(key=lambda a: a.code)

    def extend_attributes(self) -> None:
        pass


class HaierACConfig(HaierDeviceConfig):

    def __repr__(self) -> str:
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
            f")"
        )

    @property
    def preset_mode(self) -> bool:
        return bool(self.get_preset_modes())

    def get_custom_preset_modes(self) -> list[str]:
        try:
            modes = self._config.get('preset_modes')
            assert isinstance(modes, list)
            assert len(modes) > 0
            assert all((m and isinstance(m, str)) for m in modes)
            return modes
        except AssertionError:
            return []

    def get_preset_modes(self) -> list[str]:
        return self.get_custom_preset_modes() + [
            a.name.replace("preset_mode_", "")
            for a in filter(lambda a: a.name.startswith("preset_mode"), self.attrs)
        ]

    def apply_louver_layout(self, data: Any) -> None:
        """Name louver positions as the app does and hide the ones it does not offer.

        Positions of a model profile with its own mappings stay as the profile
        sets them. A hidden position still decodes, so a unit left there (say,
        by the IR remote) shows it.
        """
        profile = {a.name for a in self.get_config_attributes() if a.list}
        for code, positions in louver_layout(data).items():
            attr = self.get_attr_by_code(code)
            if attr is None or attr.name not in LOUVER_SCREENS.values() or attr.name in profile:
                continue
            # copies: the cloud's description is kept (and saved) as it came
            values = attr.get("list") if isinstance(attr.get("list"), dict) else {}
            raw = {
                str(i.get("data")): dict(i)
                for i in values.get("data") or [] if isinstance(i, dict)
            }
            shown = []
            for value, key in positions:
                if (item := raw.pop(value, None)) is None:
                    continue
                if key:
                    item["key"] = key
                shown.append(item)
            if not shown:
                continue
            for item in raw.values():
                item["hidden"] = True
            attr["list"] = dict(values, data=shown + list(raw.values()))

    def extend_attributes(self) -> None:
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


class HaierREFConfig(HaierDeviceConfig):

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}("
            f"current_temperature={self['current_temperature']!r},"
            f"current_fridge_temperature={self['current_fridge_temperature']!r},"
            f"current_freezer_temperature={self['current_freezer_temperature']!r},"
            f"fridge_mode={self['fridge_mode']!r},"
            f"freezer_mode={self['freezer_mode']!r},"
            f"super_cooling={self['super_cooling']!r},"
            f"super_freeze={self['super_freeze']!r},"
            f"vacation_mode={self['vacation_mode']!r},"
            f"door_open={self['door_open']!r}"
            f")"
        )


class HaierWMConfig(HaierDeviceConfig):

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}("
            f"status={self['status']!r},"
            f"program={self['program']!r},"
            f"temperature={self['temperature']!r},"
            f"spin_speed={self['spin_speed']!r},"
            f"remaining_time={self['remaining_time']!r}"
            f")"
        )


class HaierWHConfig(HaierDeviceConfig):

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}("
            f"current_temperature={self['current_temperature']!r},"
            f"target_temperature={self['target_temperature']!r},"
            f"status={self['status']!r},"
            f"heating_mode={self['heating_mode']!r},"
            f"heating_status={self['heating_status']!r},"
            f"sterilization={self['sterilization']!r}"
            f")"
        )


class Attribute(dict):

    def __init__(self, data: dict) -> None:
        super().__init__(data)
        self.name = {
            # Кондиционеры:
            "Режимы": "mode",
            "Целевая температура": "target_temperature",
            "Температура в комнате": "current_temperature",
            "Скорость вентилятора": "fan_mode",
            "Включение/выключение": "status",
            "Горизонтальные жалюзи": "swing_horizontal_mode",
            "Вертикальные жалюзи": "swing_mode",
            "Тихий": "preset_mode_sleep",
            "Турбо": "preset_mode_boost",
            "Комфортный сон": "comfort",
            "Здоровый режим": "health",
            "Звуковой сигнал": "sound",
            "Подсветка блока": "light",
            "10 градусов": "antifreeze",
            "Эко-датчик": "eco_sensor",
            "Стерильная очистка": "cleaning",
            "Авто влажность": "autohumidity",
            # Холодильники:
            "Температура холодильника (℃)": "current_fridge_temperature",
            "Температура морозильной камеры (°C)": "current_freezer_temperature",
            "Температура в помещении": "current_temperature",
            "Холодильное отделение": "fridge_mode",
            "Морозильное отделение": "freezer_mode",
            "Супер-охлаждение": "super_cooling",
            "Супер-заморозка": "super_freeze",
            "Режим Отпуск": "vacation_mode",
            "Состояние дверцы холодильника": "door_open",
            "My Zone": "my_zone",
            # Стиральные машины:
            "Статус": "status",
            "Программа": "program",
            "Температура": "temperature",
            "Скорость отжима": "spin_speed",
            "Оставшееся время": "remaining_time",
            # Водонагреватели (1.0):
            "Текущая температура": "current_temperature",
            "Режим нагрева": "heating_mode",
            "Поддержка температуры/обогрев": "heating_status",
            "Дезинфекция": "sterilization",
        }.get(data.get("attrname", self.description), data.get("attrname") or "unknown")

    def __repr__(self) -> str:
        list_str = ",".join(repr(i) for i in self.list)
        return (
            f"{self.__class__.__name__}("
            f"{self.name}({self.code}),"
            f"desc={self.description!r},"
            f"current={self.current!r},"
            f"range={self.range!r},"
            f"list=[{list_str}]"
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
            "list": [item.to_dict() for item in self.list],
        }

    @property
    def description(self) -> str:
        return (self.get("description") or "").strip()

    @description.setter
    def description(self, value: str) -> None:
        self["description"] = value

    @property
    def code(self) -> int | None:
        try:
            return int(self.get("name"))
        except Exception:
            return -1

    @property
    def current(self) -> int | float | str | None:
        return self.get("currentValue")

    @current.setter
    def current(self, value: int | float | str) -> None:
        self["currentValue"] = value

    @property
    def command_name(self) -> str | None:
        return self.get("commandName")

    @property
    def type(self) -> str:
        return str(self.get("type") or "").lower()

    @property
    def list(self) -> list[Item]:
        # "list": null and junk entries read as "no list".
        values = self.get("list")
        data = values.get("data") if isinstance(values, dict) else None
        if not isinstance(data, list):
            return []
        # read for every value of every push: the items are made once per list
        key = (id(data), len(data), self.name)
        cached = self.__dict__.get("_items")
        if cached is None or cached[0] != key or cached[1] is not data:
            cached = (key, data, [Item.create(self.name, v) for v in data if isinstance(v, dict)])
            self.__dict__["_items"] = cached
        return list(cached[2])

    @list.setter
    def list(self, value) -> None:
        if not isinstance(self.get("list"), dict):
            self["list"] = {}
        self["list"]["data"] = value

    @property
    def range(self) -> Range | None:
        value = self.get("range")
        return Range(value) if value and isinstance(value, dict) else None

    @range.setter
    def range(self, value: dict) -> None:
        self["range"] = value

    def get_item_code(self, name: str, default: str | None = None) -> str | None:
        item = next((i for i in self.list if str(i.name) == str(name)), None)
        if item is None or item.value is None:
            return default
        return str(item.value)

    def get_item_name(self, code: str, default: str | None = None) -> str | None:
        """Name for a raw value; None for an unknown one.

        It used to return the string "None", which ended up in HA as
        `'None' is not a valid HVACMode` (#61, #73).
        """
        items = self.list
        if not items:
            return str(code) if code is not None else default
        item = next((i for i in items if str(i.value) == str(code)), None)
        if item is None:
            # Lists say "1"/"0" where some messages say "true"/"false".
            norm = norm_value(code)
            item = next((i for i in items if str(i.value) == norm), None)
        if item is None or item.name in (None, "unknown"):
            return default
        return str(item.name)


_BOOL_NAMES = {"true": "1", "on": "1", "false": "0", "off": "0"}


def norm_value(value: Any) -> str:
    """A raw value to compare with another: modifiers and some messages say
    "true" or "on" where the lists and the unit say "1"."""
    text = str(value).strip().lower()
    return _BOOL_NAMES.get(text, text)


class Range(dict):

    def __repr__(self) -> str:
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
    def type(self) -> str | None:
        return self.get("type")

    @property
    def data(self) -> dict:
        data = self.get("data")
        return data if isinstance(data, dict) else {}

    @property
    def min_value(self) -> int | float | str | None:
        return self.data.get("minValue")

    @property
    def max_value(self) -> int | float | str | None:
        return self.data.get("maxValue")

    @property
    def step(self) -> int | float | str | None:
        return self.data.get("step")


class Item(dict):
    mappings = {
        "Выключен": "off",
        "Активен": "on",
    }

    def __init__(self, data: dict) -> None:
        super().__init__(data)
        # "key" comes from the app's own screens (see louver_layout)
        self.name = data.get("key") or self.mappings.get(
            self.description,
            data.get("attrname")
        ) or self.mappings.get("_") or "unknown"
        # a value the app does not offer: decoded, but not listed as an option
        self.hidden = bool(data.get("hidden"))

    def __repr__(self) -> str:
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
            "hidden": self.hidden,
        }

    @property
    def description(self) -> str:
        return (self.get("name") or "").strip()

    @property
    def value(self) -> str | None:
        value = self.get("data")
        if isinstance(value, dict | list):
            return None  # junk in a list of values: it matches nothing
        return {
            "true": "1",
            "false": "0",
        }.get(value, value)

    @classmethod
    def create(cls, name: str, data: dict) -> Item:
        return {
            "mode": Mode,
            "fan_mode": FanMode,
            "swing_horizontal_mode": SwingHorizontalMode,
            "swing_mode": SwingMode,
            "eco_sensor": EcoSensor,
            "fridge_mode": Temperature,
            "freezer_mode": Temperature,
            "my_zone": Temperature,
            "heating_mode": HeatingMode,
            "heating_status": HeatingStatus,
        }.get(name, cls)(data)


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
        "Выключен": "off",
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


class HeatingMode(Item):
    # Eco heats half of the tank, Max the whole tank (names as in the Evo app).
    # The heater reports "Не установлен" while sterilization (BPS) runs.
    mappings = {
        "Не установлен": "none",
        "Половинка": "eco",
        "Вся емкость": "performance",
    }


class HeatingStatus(Item):
    mappings = {
        "В режиме сохранения тепла": "keep_warm",
        "Режим нагрева": "heating",
    }


class Temperature(Item):
    # Own dict: setdefault() below used to grow the shared Item.mappings.
    # The two shared names stay: fridge zones list "Выключен" among temperatures.
    mappings: dict[str, str] = {"Выключен": "off", "Активен": "on"}

    def __init__(self, data: dict) -> None:
        description = (data.get("name") or "").strip()
        # Per instance: the class dict grew with every description seen, and the
        # next fridge in the process took them over.
        self.mappings = dict(self.mappings)
        self.mappings.setdefault(
            description,
            description.replace("℃", "").strip()
        )
        super().__init__(data)


class Constraint(list):

    def __init__(self, data: list) -> None:
        super().__init__(data)

    def apply(self, commands: list[dict]) -> list[dict]:
        if len(self) == 0 or not commands:
            return commands
        new_commands = []
        for command in commands:
            new_commands.extend(self.merge_commands(
                command,
                self.find_additionals(command)
            ))
        return new_commands

    def format_command(self, command: dict) -> dict:
        value = self.value2code(command.get("value"))
        return {
            "commandName": command.get("name"),
            "value": value,
        }

    def find_additionals(self, command: dict) -> list[dict]:
        command_code = command.get("commandName")
        command_value = command.get("value")
        additionals = []
        for item in self:
            cond = item.get("pendingCondition") or {}
            for cond_command in cond.get("commands") or []:
                if command_code != cond_command.get("commandName"):
                    continue
                values = [self.value2code(x) for x in cond_command.get("values") or []]
                if command_value not in values:
                    continue
                additionals.append(item.get("additionalCommands") or {})
        return additionals

    def merge_commands(self, command: dict, additionals: list[dict]) -> list[dict]:
        commands = {"PREPEND": [], "APPEND": []}
        for additional in additionals:
            merge_type = additional.get("mergeType")
            additional_commands = [self.format_command(x) for x in additional.get("commands") or []]
            target = commands.get(merge_type, [])
            target.extend(filter(
                lambda c: c not in target,
                additional_commands
            ))
        return [*commands["PREPEND"], command, *commands["APPEND"]]

    @staticmethod
    def value2code(value: str) -> str:
        # what is sent, not compared (not norm_value): the cloud's commands say
        # "true"/"false" where the unit wants "1"/"0"; anything else goes as
        # the cloud wrote it
        if isinstance(value, dict | list):
            return value
        return {"true": "1", "false": "0"}.get(value, value)
