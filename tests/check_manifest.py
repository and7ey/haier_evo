"""manifest.json / hacs.json: what hassfest and HACS look at."""
import sys, json, re
from pathlib import Path
_HERE = Path(__file__).resolve().parent
root = Path(sys.argv[1]) if len(sys.argv) > 1 else _HERE.parent

fails = 0
def check(name, got, want=True):
    global fails
    ok = got == want
    fails += not ok
    print(("OK  " if ok else "FAIL"), name, "" if ok else f"\n      got:  {got!r}\n      want: {want!r}")

manifest = json.loads((root / "custom_components/haier_evo/manifest.json").read_text(encoding="utf-8"))
hacs = json.loads((root / "hacs.json").read_text(encoding="utf-8"))

# keys: domain and name first, the rest sorted (hassfest's order)
keys = list(manifest)
check("manifest keys: domain, name, then sorted", keys, ["domain", "name"] + sorted(k for k in keys if k not in ("domain", "name")))
check("required keys", [k for k in ("domain", "name", "codeowners", "documentation", "issue_tracker", "iot_class", "version") if k not in manifest], [])
check("version is X.Y.Z", bool(re.fullmatch(r"\d+\.\d+\.\d+", manifest["version"])))

# categories: https://contributors.home-assistant.io/core/integrations/categories/
KNOWN = {
    "climate", "climate.air_purifiers_humidifiers", "climate.fans_ventilation",
    "climate.heat_pumps_air_conditioning", "climate.thermostats_heating", "climate.water_heaters",
    "appliances_cleaning", "appliances_cleaning.cleaning", "appliances_cleaning.cooking", "appliances_cleaning.laundry",
    "hubs_protocols", "hubs_protocols.smart_home_hubs",
}
cats = manifest.get("categories")
check("categories: a list of 1 to 3", isinstance(cats, list) and 1 <= len(cats) <= 3)
check("categories: form `group` or `group.sub` (hassfest)", [c for c in cats or [] if not re.fullmatch(r"[a-z0-9_]+(\.[a-z0-9_]+)?", c)], [])
check("categories: unique", len(set(cats or [])) == len(cats or []))
check("categories: from the documented list", [c for c in cats or [] if c not in KNOWN], [])
check("categories: air conditioning first (the main use)", (cats or [None])[0], "climate.heat_pumps_air_conditioning")

# requirements: Home Assistant's own dependencies must not be listed (hassfest)
check("no Home Assistant dependency in requirements", [r for r in manifest.get("requirements", []) if re.match(r"(requests|aiohttp|voluptuous)\b", r)], [])

check("hacs.json: name and country", (bool(hacs.get("name")), bool(hacs.get("country"))), (True, True))
brand = root / "custom_components/haier_evo/brand"
check("brand images present", sorted(p.name for p in brand.glob("*.png")), ["icon.png", "icon@2x.png", "logo.png", "logo@2x.png"])

print("FAILS:", fails)
sys.exit(1 if fails else 0)
