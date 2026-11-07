"""Home Assistant 2026.11 names: no deprecation report for the climate and water
heater entities (the old names still serve 2026.10), checked where 2026.11 runs."""
import sys, logging
from pathlib import Path
_HERE = Path(__file__).resolve().parent
root = sys.argv[1] if len(sys.argv) > 1 else str(_HERE.parent)
sys.path.insert(0, root)
from homeassistant.const import MAJOR_VERSION, MINOR_VERSION

fails = 0
def check(name, got, want=True):
    global fails
    ok = got == want
    fails += not ok
    print(("OK  " if ok else "FAIL"), name, "" if ok else f"\n      got:  {got!r}\n      want: {want!r}")

if (MAJOR_VERSION, MINOR_VERSION) < (2026, 11):
    print(f"OK   Home Assistant {MAJOR_VERSION}.{MINOR_VERSION}: nothing to check before 2026.11")
    print("FAILS: 0")
    sys.exit(0)

records = []
class Grab(logging.Handler):
    def emit(self, record): records.append(record.getMessage())
logging.getLogger().addHandler(Grab())
logging.getLogger().setLevel(logging.DEBUG)

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.water_heater import WaterHeaterEntity
from custom_components.haier_evo import climate, water_heater  # noqa: F401  classes are built on import
ours = [m for m in records if "deprecated" in m.lower() and ("HaierACEntity" in m or "HaierWHEntity" in m)]
check("HA 2026.11: no deprecation report for HaierACEntity / HaierWHEntity", ours, [])

# the check would see one: a class with the old names only is reported
records.clear()
class OldClimate(ClimateEntity):
    @property
    def current_temperature(self): return 1.0
class OldHeater(WaterHeaterEntity):
    _attr_temperature_unit = "°C"
check("control: old names alone are reported",
      (any("OldClimate" in m and "deprecated" in m.lower() for m in records),
       any("OldHeater" in m and "deprecated" in m.lower() for m in records)), (True, True))

print("FAILS:", fails)
sys.exit(1 if fails else 0)
