"""Model profiles (devices/*.yaml), translations and icons agree with each other
and with the code: every profile is found and read, names only what the
integration knows, every option a device offers is translated, en and ru say
the same things with the same placeholders, every key the code uses exists.

Ported from tests/check_profiles.py of 1.7.2 to this code: the devices live in
device_*.py, the entities name themselves (has_entity_name), the options are
read from devices of a running Home Assistant.
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path

from homeassistant.components.climate import const as climate_const
from homeassistant.components.climate.const import HVACMode
from homeassistant.const import STATE_OFF
from homeassistant.exceptions import HomeAssistantError

from custom_components.haier_evo import config as CFG  # noqa: N812
from custom_components.haier_evo.device_ac import HaierAC
from custom_components.haier_evo.device_wh import HaierWH

from .test_devices import EVO, start, stop

PKG = Path(__file__).resolve().parent.parent / "custom_components" / "haier_evo"
PROFILES = sorted((PKG / "devices").glob("*.yaml"))
EN = json.loads((PKG / "translations" / "en.json").read_text(encoding="utf-8"))
RU = json.loads((PKG / "translations" / "ru.json").read_text(encoding="utf-8"))
ICONS = json.loads((PKG / "icons.json").read_text(encoding="utf-8"))
CODE = {p.name: p.read_text(encoding="utf-8") for p in PKG.glob("*.py")}
ALL_CODE = "\n".join(CODE.values())

# Profiles nobody's model name can reach (see test_every_profile_is_reachable);
# kept here until the profile is renamed or removed, so that a new one fails.
UNREACHABLE = {
    # 12 characters, but a model name is cut to 11 (HaierDevice._get_status):
    # the lookup is for CASWD129C1C.yaml
    "CASWD129C1CU",
}
# Home Assistant translates its own climate values itself
HA_CLIMATE = {
    v for k, v in vars(climate_const).items() if k.startswith(("FAN_", "SWING_", "PRESET_")) and isinstance(v, str)
}
# what the code looks attributes up by; a profile naming anything else is ignored
CODE_NAMES = set(re.findall(
    r"""(?:config\[|get_attr_by_name\(|get_values\(|get_value\(|get_value_code\(|get_code_by_name\(|"""
    r"""_check_writable\(|get_commands\(|attr\.name == |feature, fallback = )["']([a-z_0-9]+)["']""",
    ALL_CODE,
)) | {  # attr.name in ("quiet", "preset_mode_sleep")
    name for group in re.findall(r"attr\.name in \(([^)]*)\)", ALL_CODE)
    for name in re.findall(r"""["']([a-z_0-9]+)["']""", group)
}
ON_OFF = {"health", "light", "sound", "antifreeze", "cleaning", "autohumidity", "comfort", "quiet", "turbo",
          "preset_mode_sleep", "preset_mode_boost", "status", "sterilization", "super_cooling", "super_freeze",
          "vacation_mode"}
TOP_KEYS = {"command_name", "attributes", "commands", "preset_modes"}
ATTR_KEYS, MAP_KEYS = {"name", "id", "mappings"}, {"haier", "value"}


def leaves(d: dict, path: tuple = ()):
    for k, v in d.items():
        if isinstance(v, dict):
            yield from leaves(v, (*path, k))
        else:
            yield (*path, k), v


def states(lang: dict, *path: str) -> set[str]:
    node = lang
    for key in path:
        node = node.get(key, {}) if isinstance(node, dict) else {}
    return set(node.get("state", {})) if isinstance(node, dict) else set()


def model_name(raw: str) -> str:
    """What the integration looks a profile up by (HaierDevice._get_status)."""
    return raw.replace("-", "").replace("/", "")[:11]


def placeholders(text) -> list[str]:
    return sorted(set(re.findall(r"\{(\w+)\}", str(text))))


CONFIGS = {path.stem: CFG.HaierDeviceConfig(path.stem) for path in PROFILES}
MODELS = {s: c for s, c in CONFIGS.items() if s != "default"}
# every profile but those of washing machines and water heaters
AC_MODELS = [
    s for s, c in MODELS.items()
    if not {"program", "heating_mode", "fridge_mode", "freezer_mode", "door_open"} & {a["name"] for a in c._config["attributes"]}
]


# --- profiles ---


def test_profiles_are_read_and_found():
    assert len(PROFILES) > 1 and "default" in CONFIGS
    assert [p for p, c in CONFIGS.items()
            if not isinstance(c._config, dict) or not isinstance(c._config.get("attributes"), list)] == []
    assert [p for p, c in CONFIGS.items() if Path(c._find_config_file()).stem != p] == []


def test_every_profile_is_reachable():
    """Its name is what a model name becomes: no - or /, at most 11 characters."""
    assert sorted(
        p for p in CONFIGS if p != "default" and (model_name(p) != p or not CFG.SAFE_MODEL.fullmatch(p))
    ) == sorted(UNREACHABLE)
    assert sorted({model_name(d["backend_data"]["info"]["model"]) for d in EVO["devices"]} - set(CONFIGS)) == []


def test_profiles_use_only_what_the_loader_and_the_code_read():
    errors = []
    for stem, cfg in CONFIGS.items():
        data = cfg._config
        errors += [f"{stem}: key {k}" for k in set(data) - TOP_KEYS]
        for attr in data.get("attributes") or []:
            errors += [f"{stem}.{attr.get('name')}: key {k}" for k in set(attr) - ATTR_KEYS]
            for m in attr.get("mappings") or []:
                errors += [f"{stem}.{attr.get('name')}: mapping key {k}" for k in set(m) - MAP_KEYS]
    assert errors == []
    assert sorted({
        f"{s}.{a.get('name')}" for s, c in CONFIGS.items() for a in c._config["attributes"]
        if a.get("name") not in CODE_NAMES
    }) == []


def test_model_profiles_are_consistent():
    errors = []
    for stem, cfg in MODELS.items():
        attrs = cfg._config["attributes"]
        ids = [str(a.get("id")) for a in attrs]
        names = [a.get("name") for a in attrs]
        errors += [f"{stem}.{a.get('name')}={a.get('id')!r}" for a in attrs if not re.fullmatch(r"\d+", str(a.get("id")))]
        errors += [f"{stem}: id {i} twice" for i in set(ids) if ids.count(i) > 1]
        errors += [f"{stem}: {n} twice" for n in set(names) if names.count(n) > 1]
        if "command_name" in cfg._config and not re.fullmatch(r"\d+", str(cfg._config["command_name"])):
            errors.append(f"{stem}: command_name")
        for attr in attrs:
            maps = attr.get("mappings") or []
            codes = [str(m.get("haier")) for m in maps]
            values = [str(m.get("value")) for m in maps]
            # true / false are codes too: they go out as 1 / 0 (config.Item.value)
            errors += [f"{stem}.{attr['name']}: code {m.get('haier')!r}" for m in maps
                       if not re.fullmatch(r"-?\d+|true|false", str(m.get("haier")))]
            errors += [f"{stem}.{attr['name']}: empty value" for v in values if v in ("", "None")]
            errors += [f"{stem}.{attr['name']}: code {c} twice" for c in set(codes) if codes.count(c) > 1]
            errors += [f"{stem}.{attr['name']}: {v} twice" for v in set(values) if values.count(v) > 1]
            if attr["name"] == "mode":
                errors += [f"{stem}: mode {v}" for v in values if v not in {m.value for m in HVACMode} or v == "off"]
            if attr["name"] in ON_OFF:
                errors += [f"{stem}.{attr['name']}: {v}" for v in values if v not in ("on", "off")]
        if len(cfg.get_config_attributes()) != len(attrs):
            errors.append(f"{stem}: attributes dropped as empty")
    assert errors == []
    # default.yaml overrides nothing: the cloud's description fills every code
    assert (CONFIGS["default"].get_config_attributes(), str(CONFIGS["default"]._config.get("command_name"))) == ([], "")


# --- translations ---


def test_en_and_ru_say_the_same():
    en, ru = dict(leaves(EN)), dict(leaves(RU))
    assert (sorted(set(en) - set(ru)), sorted(set(ru) - set(en))) == ([], [])
    assert [".".join(k) for k, v in {**en, **ru}.items() if not str(v).strip()] == []
    assert [".".join(k) for k in en if k in ru and placeholders(en[k]) != placeholders(ru[k])] == []


def test_entity_keys_of_the_code_are_translated():
    keys = {
        (Path(f).stem, k) for f, text in CODE.items()
        for k in re.findall(r"""_attr_translation_key = ["'](\w+)["']""", text)
    }
    assert keys
    assert sorted(f"{p}.{k}" for p, k in keys if k not in EN.get("entity", {}).get(p, {})) == []
    assert sorted(f"{p}.{k}" for p, ks in EN.get("entity", {}).items() for k in ks if (p, k) not in keys) == []


def test_exceptions_of_the_code_are_translated():
    raised = set(re.findall(r"""translation_key=["'](\w+)["']""", ALL_CODE))
    raised |= set(re.findall(r"""ProgramValidationError\(["'](\w+)["']""", CODE.get("programs.py", "")))
    # the laundry's errors go through _control_error / _check_control / _act
    raised |= set(re.findall(
        r"""(?:_control_error\(|_parameter_error\(|_check_control\([^,]+, |_act\([^,]+, )["'](\w+)["']""", CODE["device_laundry.py"]
    ))
    # f"locked_{reason}": the reasons of HaierDevice._modifier_reason, "by:<code>" is "by"
    raised |= {f"locked_{r}" for r in ("off", "mode", "alarm", "by")}
    assert all(f'"{r}"' in CODE["device.py"] for r in ("off", "mode", "alarm")) and '"by:' in CODE["device.py"]
    assert sorted(raised - set(EN.get("exceptions", {}))) == []
    assert sorted(set(EN.get("exceptions", {})) - raised) == []


def test_flows_are_translated():
    flow, _, options_flow = CODE["config_flow.py"].partition("class HaierEvoOptionsFlow")
    config = EN["config"]
    errors = set(re.findall(r"""errors\["base"\] = ["'](\w+)["']""", flow))
    steps = set(re.findall(r"""step_id=["'](\w+)["']""", flow))
    aborts = set(re.findall(r"""reason=["'](\w+)["']""", flow))
    aborts |= {"already_configured"} if "_abort_if_unique_id_configured" in flow else set()
    aborts |= {"reauth_successful"} if "async_step_reauth" in flow else set()
    aborts |= {"reconfigure_successful"} if "async_step_reconfigure" in flow else set()
    assert (
        sorted(errors - set(config["error"])), sorted(steps - set(config["step"])), sorted(aborts - set(config["abort"]))
    ) == ([], [], [])
    consts = dict(re.findall(r"""^(CONF_\w+) = ["'](\w+)["']""", CODE["const.py"], re.M))
    consts |= {"CONF_EMAIL": "email", "CONF_PASSWORD": "password", "CONF_REGION": "region"}

    def fields(text: str) -> set[str]:
        return {consts.get(n, n.strip("\"'")) for n in re.findall(r"vol\.(?:Required|Optional)\(([\w\"']+)", text)}

    labelled = {f for s in config["step"].values() for f in s.get("data", {})}
    assert sorted(fields(flow) - labelled) == []
    options = EN["options"]["step"]
    option_fields = fields(options_flow)
    assert option_fields == {"laundry_control"}
    assert sorted(set(re.findall(r"""step_id=["'](\w+)["']""", options_flow)) - set(options)) == []
    assert sorted(option_fields - {f for s in options.values() for f in s.get("data", {})}) == []
    assert sorted(option_fields - {f for s in options.values() for f in s.get("data_description", {})}) == []
    # the reauth text shows the email it is given
    assert "{email}" in config["step"]["reauth_confirm"]["description"]
    assert "{email}" in RU["config"]["step"]["reauth_confirm"]["description"]
    assert 'description_placeholders={"email"' in flow


def test_icons_only_for_translated_entities_and_values():
    errors = []
    for platform, keys in ICONS.get("entity", {}).items():
        for key, body in keys.items():
            if key not in EN.get("entity", {}).get(platform, {}):
                errors.append(f"{platform}.{key}")
                continue
            for attr, spec in (body.get("state_attributes") or {}).items():
                errors += [
                    f"{platform}.{key}.{attr}.{s}" for s in spec.get("state", {})
                    if s not in states(EN, "entity", platform, key, "state_attributes", attr) and s not in HA_CLIMATE
                ]
            errors += [f"{platform}.{key}.state.{s}" for s in body.get("state", {})
                       if s not in states(EN, "entity", platform, key)]
    assert errors == []


# --- what devices offer: every option is translated ---


def _options(ac: HaierAC, where: str) -> set[str]:
    out = set()
    for attr, values in (("fan_mode", ac.get_fan_modes()), ("swing_mode", ac.get_swing_modes()),
                         ("swing_horizontal_mode", ac.get_swing_horizontal_modes()),
                         ("preset_mode", ac.get_preset_modes())):
        known = states(EN, "entity", "climate", "conditioner", "state_attributes", attr) | HA_CLIMATE
        out |= {f"{where}: {attr}={v}" for v in values if v not in known or v in ("unknown", "None")}
    out |= {f"{where}: hvac_mode={m}" for m in ac.get_hvac_modes() if m not in set(HVACMode)}
    out |= {f"{where}: eco_sensor={v}" for v in ac.get_eco_sensor_options()
            if v not in states(EN, "entity", "select", "conditioner_eco_sensor")}
    return out


def _message(lang: dict, err: HomeAssistantError) -> str:
    text = lang.get("exceptions", {}).get(err.translation_key, {}).get("message")
    if text is None:
        return "missing the string"
    try:
        out = text.format(**(err.translation_placeholders or {}))
    except (KeyError, IndexError) as e:
        return f"missing {e}"
    return out if "{" not in out else f"left over in {out!r}"


async def test_every_option_of_a_device_is_translated(hass, aioclient_mock, ws_factory):
    """Air conditioners with their own description and with every AC profile, the water heater."""
    entry = await start(hass, aioclient_mock, ws_factory, EVO)
    devices = entry.runtime_data.devices
    acs = [d for d in devices if isinstance(d, HaierAC)]
    assert acs and AC_MODELS
    untranslated = set()
    seen = False
    for ac in acs:
        own = copy.deepcopy(ac.status_data)
        for model in [None, *AC_MODELS]:
            data = copy.deepcopy(own)
            if model:
                data["info"]["model"] = model
            ac._get_status(data)
            seen |= bool(ac.get_swing_modes())
            untranslated |= _options(ac, f"{ac.device_name}/{model or 'own'}")
        ac._get_status(own)
    assert sorted(untranslated) == []
    assert seen  # the check saw options at all
    heater = next(d for d in devices if isinstance(d, HaierWH))
    assert [v for v in [STATE_OFF, *heater.get_heating_modes()]
            if v not in states(EN, "entity", "water_heater", "water_heater")] == []
    await stop(hass, entry)


async def test_raised_messages_read_in_both_languages(hass, aioclient_mock, ws_factory):
    """Every placeholder of a message the devices raise is given."""
    entry = await start(hass, aioclient_mock, ws_factory, EVO)
    devices = entry.runtime_data.devices
    ac = next(d for d in devices if isinstance(d, HaierAC))
    heater = next(d for d in devices if isinstance(d, HaierWH))
    raised: list[HomeAssistantError] = []

    async def collect(call) -> None:
        try:
            await call
        except HomeAssistantError as err:
            raised.append(err)

    ac._apply_value(ac.config["status"], "0")
    await collect(ac.set_fan_mode("low"))
    await collect(ac.set_preset_mode("boost"))
    ac._apply_value(ac.config["status"], "1")
    fan_only = ac.config.get_attr_by_name("mode").get_item_code("fan_only")
    if fan_only is not None:
        ac._apply_value(ac.config["mode"], fan_only)
        await collect(ac.set_temperature(22))
        await collect(ac.set_fan_mode("auto"))
    await collect(heater.set_heating_mode("junk"))
    keys = {e.translation_key for e in raised}
    assert "value_not_allowed" in keys and len(raised) >= 3, keys
    assert [
        f"{e.translation_key}: {m}" for e in raised for lang in (EN, RU)
        if (m := _message(lang, e)).startswith(("missing", "left over"))
    ] == []
    await stop(hass, entry)
