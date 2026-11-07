"""Model profiles (devices/*.yaml), translations and icons agree with each other
and with the code: every profile is found and read, names only what the
integration knows, every option a device offers is translated, en and ru say
the same things with the same placeholders, every key the code uses exists.
"""
import sys, json, os, re, copy, tempfile
from pathlib import Path
_HERE = Path(__file__).resolve().parent
root = Path(sys.argv[1] if len(sys.argv) > 1 else _HERE.parent)
sys.path.insert(0, str(root))
from homeassistant.components.climate import const as climate_const
from homeassistant.components.climate.const import HVACMode
from homeassistant.const import STATE_OFF
from homeassistant.exceptions import HomeAssistantError
from custom_components.haier_evo import api, config as CFG

PKG = root / "custom_components" / "haier_evo"
PROFILES = sorted((PKG / "devices").glob("*.yaml"))
EN = json.loads((PKG / "translations" / "en.json").read_text(encoding="utf-8"))
RU = json.loads((PKG / "translations" / "ru.json").read_text(encoding="utf-8"))
ICONS = json.loads((PKG / "icons.json").read_text(encoding="utf-8"))
CODE = {p.name: p.read_text(encoding="utf-8") for p in PKG.glob("*.py")}
dump = json.loads((_HERE / "fixtures" / "evo_dump.json").read_text(encoding="utf-8"))

# Profiles nobody's model name can reach (see "every profile is reachable");
# kept here until the profile is renamed or removed, so that a new one fails.
UNREACHABLE = {
    # 12 characters, but a model name is cut to 11 (api._get_status): the
    # lookup is for CASWD129C1C.yaml
    "CASWD129C1CU",
}

fails = 0
def check(name, got, want=True):
    global fails
    ok = got == want
    fails += not ok
    print(("OK  " if ok else "FAIL"), name, "" if ok else f"\n      got:  {got!r}\n      want: {want!r}")

def leaves(d, path=()):
    for k, v in d.items():
        if isinstance(v, dict):
            yield from leaves(v, path + (k,))
        else:
            yield path + (k,), v

def states(lang, *path):
    node = lang
    for key in path:
        node = node.get(key, {}) if isinstance(node, dict) else {}
    return set(node.get("state", {})) if isinstance(node, dict) else set()

def model_name(raw):
    """What the integration looks a profile up by (api.HaierDevice._get_status)."""
    return raw.replace("-", "").replace("/", "")[:11]

# Home Assistant translates its own climate values itself
HA_CLIMATE = {v for k, v in vars(climate_const).items()
              if k.startswith(("FAN_", "SWING_", "PRESET_")) and isinstance(v, str)}
# what the code looks attributes up by; a profile naming anything else is ignored
CODE_NAMES = set(re.findall(
    r"""(?:config\[|get_attr_by_name\(|get_values\(|get_value\(|get_value_code\(|get_code_by_name\(|"""
    r"""_check_writable\(|get_commands\(|attr\.name == |feature, fallback = )["']([a-z_0-9]+)["']""",
    "\n".join(CODE.values())))
ON_OFF = {"health", "light", "sound", "antifreeze", "cleaning", "autohumidity", "comfort", "quiet", "turbo",
          "preset_mode_sleep", "preset_mode_boost", "status", "sterilization", "super_cooling", "super_freeze",
          "vacation_mode"}
TOP_KEYS, ATTR_KEYS, MAP_KEYS = {"command_name", "attributes", "commands", "preset_modes"}, {"name", "id", "mappings"}, {"haier", "value"}

# ---- profiles
check("there are profiles, default.yaml among them", (len(PROFILES) > 1, "default" in [p.stem for p in PROFILES]), (True, True))
configs = {}
for path in PROFILES:
    cfg = CFG.HaierDeviceConfig(path.stem)
    configs[path.stem] = cfg
check("every profile is read: a mapping with an attribute list",
      [p for p, c in configs.items() if not isinstance(c._config, dict) or not isinstance(c._config.get("attributes"), list)], [])
check("every profile is found by its own name",
      [p for p, c in configs.items() if Path(c._find_config_file()).stem != p], [])
check("every profile is reachable: its name is what a model name becomes (no - or /, at most 11 characters)",
      sorted(p for p in configs if p != "default" and (model_name(p) != p or not CFG.SAFE_MODEL.fullmatch(p))), sorted(UNREACHABLE))
check("the model names of the fixture find their profiles",
      sorted({model_name(d["backend_data"]["info"]["model"]) for d in dump["devices"]} - set(configs)), [])

def schema_errors(stem, data):
    out = []
    out += [f"{stem}: key {k}" for k in set(data) - TOP_KEYS]
    for attr in data.get("attributes") or []:
        out += [f"{stem}.{attr.get('name')}: key {k}" for k in set(attr) - ATTR_KEYS]
        for m in attr.get("mappings") or []:
            out += [f"{stem}.{attr.get('name')}: mapping key {k}" for k in set(m) - MAP_KEYS]
    return out
check("profiles use only the keys the loader reads", [e for s, c in configs.items() for e in schema_errors(s, c._config)], [])
check("profiles name only attributes the code uses",
      sorted({f"{s}.{a.get('name')}" for s, c in configs.items() for a in c._config["attributes"] if a.get("name") not in CODE_NAMES}), [])

models = {s: c for s, c in configs.items() if s != "default"}
def bad_ids(stem, cfg):
    attrs = cfg._config["attributes"]
    ids = [str(a.get("id")) for a in attrs]
    out = [f"{stem}.{a.get('name')}={a.get('id')!r}" for a in attrs if not re.fullmatch(r"\d+", str(a.get("id")))]
    out += [f"{stem}: id {i} twice" for i in set(ids) if ids.count(i) > 1]
    out += [f"{stem}: {n} twice" for n in {a.get("name") for a in attrs} if [a.get("name") for a in attrs].count(n) > 1]
    return out
check("model profiles: every attribute has a numeric id, no id or name twice", [e for s, c in models.items() for e in bad_ids(s, c)], [])
check("model profiles: command_name, where set, is a number",
      [s for s, c in models.items() if "command_name" in c._config and not re.fullmatch(r"\d+", str(c._config["command_name"]))], [])

def bad_mappings(stem, cfg):
    out = []
    for attr in cfg._config["attributes"]:
        maps = attr.get("mappings") or []
        codes = [str(m.get("haier")) for m in maps]
        values = [str(m.get("value")) for m in maps]
        # true / false are codes too: they go out as 1 / 0 (config.Item.value)
        out += [f"{stem}.{attr['name']}: code {m.get('haier')!r}" for m in maps if not re.fullmatch(r"-?\d+|true|false", str(m.get("haier")))]
        out += [f"{stem}.{attr['name']}: empty value" for v in values if v in ("", "None")]
        out += [f"{stem}.{attr['name']}: code {c} twice" for c in set(codes) if codes.count(c) > 1]
        out += [f"{stem}.{attr['name']}: {v} twice" for v in set(values) if values.count(v) > 1]
    return out
check("model profiles: codes are numbers, values are set, neither twice in an attribute",
      [e for s, c in models.items() for e in bad_mappings(s, c)], [])
check("model profiles: the attributes the loader takes are all of them (none dropped as empty)",
      [s for s, c in models.items() if len(c.get_config_attributes()) != len(c._config["attributes"])], [])

def values_of(cfg, name):
    return [str(m.get("value")) for a in cfg._config["attributes"] if a.get("name") == name for m in a.get("mappings") or []]
check("model profiles: modes are Home Assistant's HVAC modes",
      sorted({f"{s}:{v}" for s, c in models.items() for v in values_of(c, "mode") if v not in {m.value for m in HVACMode} or v == "off"}), [])
check("model profiles: switches map to on / off",
      sorted({f"{s}.{a['name']}:{m.get('value')}" for s, c in models.items() for a in c._config["attributes"] if a["name"] in ON_OFF
              for m in a.get("mappings") or [] if str(m.get("value")) not in ("on", "off")}), [])
check("default.yaml overrides nothing: the cloud's description fills every code",
      (configs["default"].get_config_attributes(), str(configs["default"]._config.get("command_name"))), ([], ""))

# ---- translations
en_leaves, ru_leaves = dict(leaves(EN)), dict(leaves(RU))
check("en and ru have the same strings", (sorted(set(en_leaves) - set(ru_leaves)), sorted(set(ru_leaves) - set(en_leaves))), ([], []))
check("no empty string", [".".join(k) for k, v in {**en_leaves, **ru_leaves}.items() if not str(v).strip()], [])
placeholders = lambda s: sorted(set(re.findall(r"\{(\w+)\}", str(s))))
check("en and ru use the same placeholders in every string",
      [".".join(k) for k in en_leaves if k in ru_leaves and placeholders(en_leaves[k]) != placeholders(ru_leaves[k])], [])

entity_keys = {(Path(f).stem, k) for f, text in CODE.items() for k in re.findall(r"""_attr_translation_key = ["'](\w+)["']""", text)}
check("every entity translation key of the code is translated",
      sorted(f"{p}.{k}" for p, k in entity_keys if k not in EN.get("entity", {}).get(p, {})), [])
check("every translated entity is in the code",
      sorted(f"{p}.{k}" for p, ks in EN.get("entity", {}).items() for k in ks if (p, k) not in entity_keys), [])
exception_keys = set(re.findall(r"""translation_key=["'](\w+)["']""", CODE["api.py"])) | set(
    re.findall(r"""key, extra = ["'](\w+)["']""", CODE["api.py"]))
check("every exception the code raises is translated", sorted(exception_keys - set(EN.get("exceptions", {}))), [])
check("every translated exception is raised somewhere", sorted(set(EN.get("exceptions", {})) - exception_keys), [])

flow = CODE["config_flow.py"]
errors = set(re.findall(r"""errors\["base"\] = ["'](\w+)["']""", flow))
steps = set(re.findall(r"""step_id=["'](\w+)["']""", flow))
aborts = ({"already_configured"} if "_abort_if_unique_id_configured" in flow else set()) | (
    {"reauth_successful"} if "async_update_reload_and_abort" in flow else set())
fields = set(re.findall(r"""vol\.(?:Required|Optional)\(["'](\w+)["']""", flow))
config = EN.get("config", {})
check("config flow: every error, step and abort reason is translated",
      (sorted(errors - set(config.get("error", {}))), sorted(steps - set(config.get("step", {}))), sorted(aborts - set(config.get("abort", {})))),
      ([], [], []))
check("config flow: every form field has a label",
      sorted(fields - {f for s in config.get("step", {}).values() for f in s.get("data", {})}), [])
check("config flow: the reauth text shows the email it is given",
      ("{email}" in config["step"]["reauth_confirm"]["description"], "{email}" in RU["config"]["step"]["reauth_confirm"]["description"],
       'description_placeholders={"email"' in flow), (True, True, True))

# ---- icons
icon_errors = []
for platform, keys in ICONS.get("entity", {}).items():
    for key, body in keys.items():
        if key not in EN.get("entity", {}).get(platform, {}):
            icon_errors.append(f"{platform}.{key}")
            continue
        for attr, spec in (body.get("state_attributes") or {}).items():
            icon_errors += [f"{platform}.{key}.{attr}.{s}" for s in spec.get("state", {})
                            if s not in states(EN, "entity", platform, key, "state_attributes", attr) and s not in HA_CLIMATE]
        icon_errors += [f"{platform}.{key}.state.{s}" for s in body.get("state", {}) if s not in states(EN, "entity", platform, key)]
check("icons only for translated entities and their values", icon_errors, [])

# ---- what devices offer: every option is translated
tmp = tempfile.mkdtemp()
class Conf:
    def path(self, x): return os.path.join(tmp, x)
class Loop:
    def call_soon_threadsafe(self, cb): pass
class Hass: config = Conf(); loop = Loop()
class FakeHaier:
    def __init__(self): self.hass = Hass(); self.sent = []
    def send_message(self, payload): self.sent.append(json.loads(payload))
    def send_command(self, message): self.sent.append(dict(message))
    def get_device_by_id(self, mac): return None

clients = []    # a device holds its client by a weak reference
def make(cls, dev, model=None):
    data = copy.deepcopy(dev["backend_data"])
    if model:
        data["info"]["model"] = model
    clients.append(FakeHaier())
    return cls(haier=clients[-1], device_mac=dev["device_id"], device_serial="x", device_title="t", backend_data=data)

acs = [d for d in dump["devices"] if not d["device_id"].startswith("5c:01")]
wh_dev = next(d for d in dump["devices"] if d["device_id"].startswith("5c:01"))
# every profile but those of washing machines and water heaters
ac_models = [s for s, c in models.items() if not {"program", "heating_mode"} & {a["name"] for a in c._config["attributes"]}]
untranslated = set()
for dev in acs:
    for model in [None] + ac_models:
        ac = make(api.HaierAC, dev, model)
        where = f"{dev['device_name']}/{model or 'own'}"
        for attr, values in (("fan_mode", ac.get_fan_modes()), ("swing_mode", ac.get_swing_modes()),
                             ("swing_horizontal_mode", ac.get_swing_horizontal_modes()), ("preset_mode", ac.get_preset_modes())):
            known = states(EN, "entity", "climate", "conditioner", "state_attributes", attr) | HA_CLIMATE
            untranslated |= {f"{where}: {attr}={v}" for v in values if v not in known}
        untranslated |= {f"{where}: hvac_mode={m}" for m in ac.get_hvac_modes() if m not in set(HVACMode)}
        untranslated |= {f"{where}: eco_sensor={v}" for v in ac.get_eco_sensor_options()
                         if v not in states(EN, "entity", "select", "conditioner_eco_sensor")}
check("air conditioners, with their own description and with every AC profile: every option is translated",
      sorted(untranslated), [])
check("... and the check saw options at all", bool(make(api.HaierAC, acs[0]).get_swing_modes()), True)
wh = make(api.HaierWH, wh_dev)
check("water heater: every operation is translated",
      [v for v in [STATE_OFF] + wh.get_heating_modes() if v not in states(EN, "entity", "water_heater", "water_heater")], [])
check("no device offers 'unknown' or 'None'",
      sorted({f"{d['device_name']}: {v}" for d in acs for v in (make(api.HaierAC, d).get_fan_modes() + make(api.HaierAC, d).get_swing_modes()
              + make(api.HaierAC, d).get_swing_horizontal_modes() + make(api.HaierAC, d).get_preset_modes()) if v in ("unknown", "None")}), [])

# exceptions the devices raise read in both languages: every placeholder is given
def message(lang, err):
    text = lang.get("exceptions", {}).get(err.translation_key, {}).get("message")
    if text is None:
        return "missing the string"
    try:
        out = text.format(**(err.translation_placeholders or {}))
    except (KeyError, IndexError) as e:
        return f"missing {e}"
    return out if "{" not in out else f"left over in {out!r}"
raised = []
ac = make(api.HaierAC, acs[0])
ac.switch_off()
for call in (lambda: ac.set_fan_mode("low"), lambda: ac.set_preset_mode("boost")):
    try:
        call()
    except HomeAssistantError as e:
        raised.append(e)
ac = make(api.HaierAC, acs[0])
ac.switch_on("fan_only")
for call in (lambda: ac.set_temperature(22), lambda: ac.set_fan_mode("auto")):
    try:
        call()
    except HomeAssistantError as e:
        raised.append(e)
check("the lock messages were raised", sorted({e.translation_key for e in raised}) and len(raised) >= 3, True)
check("every raised message reads in en and ru with all its placeholders",
      [f"{e.translation_key}: {m}" for e in raised for lang in (EN, RU) if (m := message(lang, e)).startswith(("missing", "left over"))], [])

print("FAILS:", fails)
sys.exit(1 if fails else 0)
