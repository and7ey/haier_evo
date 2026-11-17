"""Casarte CASWD129C1CU, a washer-dryer of the old cloud format (HaierWM, CASWD129C1C.yaml).

tests/fixtures/caswd_dump.json is the description the integration saved for
the machine (and7ey/haier_evo#88), MAC and serial number replaced. Every
attribute is described as "not found": the profile names them by code. The
codes of the care program (58), the plasma (35), the delayed start (18) and the
drying level (48) were checked on the machine against the app.
"""
from __future__ import annotations

import copy
from types import SimpleNamespace

from custom_components.haier_evo import binary_sensor, sensor
from custom_components.haier_evo.device_wm import HaierWM

from .conftest import load_fixture

ITEM = load_fixture("caswd_dump.json")["devices"][0]
MAC = ITEM["device_id"]


def washer(hass, backend_data: dict | None = None) -> HaierWM:
    hub = SimpleNamespace(hass=hass, cloud_available=True, laundry_control=False)
    return HaierWM(haier=hub, device_mac=MAC, device_serial="x", device_title=ITEM["device_name"],
                   backend_data=copy.deepcopy(backend_data or ITEM["backend_data"]))


async def test_the_profile_is_found_for_the_cut_model(hass):
    wm = washer(hass)
    assert wm.device_model == "CASWD129C1C"
    assert wm.config._find_config_file().endswith("CASWD129C1C.yaml")


async def test_attributes_are_named_by_code(hass):
    wm = washer(hass)
    names = {str(a.code): a.name for a in wm.config.attrs}
    assert {code: names[code] for code in ("0", "8", "27", "35", "48", "58", "76", "87", "22", "51")} == {
        "0": "wash_program", "8": "status", "27": "dry_remaining_time", "35": "plasma_sterilization",
        "48": "dry_level", "58": "care_program", "76": "wash_temperature", "87": "wash_spin_speed",
        "22": "wash_phase", "51": "dry_phase",
    }
    # nothing takes the names of the upstream washing machine: no commands go out
    assert [wm.config[n] for n in ("program", "temperature", "spin_speed", "remaining_time")] == [None] * 4
    assert wm.create_entities_select() == []


async def test_a_description_does_not_name_an_attribute_the_profile_does_not(hass):
    """Should the cloud describe the attributes one day: "Температура" of another code stays unknown."""
    data = copy.deepcopy(ITEM["backend_data"])
    for attr in data["attributes"]:
        if attr["name"] in ("62", "63"):  # not in the profile
            attr["description"] = "Температура"
    wm = washer(hass, data)
    assert wm.config["temperature"] is None
    assert wm.config.get_attr_by_name("wash_temperature").code == 76
    assert wm.create_entities_select() == []


async def test_values_and_entities(hass):
    wm = washer(hass)
    sensors = {type(e): e for e in wm.create_entities_sensor()}
    flags = {type(e): e for e in wm.create_entities_binary_sensor()}
    assert {cls.feature: sensors[cls].native_value for cls in sensor.WM_SENSORS} == {
        "wash_phase": "finished",
        "wash_program": "wash_cotton",
        "wash_temperature": "30",
        "wash_spin_speed": "800",
        "wash_remaining_minutes": 1,
        "dry_phase": "none",
        "dry_mode": "dry",
        "dry_program": "dry_auto",
        "dry_level": "dry_closet",
        "care_program": "care_none",
        "dry_remaining_time": 110,
    }
    assert sensors[sensor.HaierWMDryLevelSensor].options == ["dry_iron", "dry_wear", "dry_closet", "dry_extra"]
    assert {cls.feature: flags[cls].is_on for cls in binary_sensor.WM_FLAG_SENSORS} == {
        "washer_running": False, "dryer_running": False, "remote_control": False, "wash_dry_mode": False, "delayed_start": False,
        "auto_detergent": True, "auto_conditioner": True, "steam_treatment": False,
        "uv_sterilization": False, "plasma_sterilization": False,
    }
    # the cloud's alarms and ONLINE/OFFLINE, as for the washers of the current format
    assert flags[binary_sensor.HaierProblemSensor].is_on is False
    assert flags[binary_sensor.HaierConnectivitySensor].available
    assert wm.status == "on"  # 8, "onOffStatus" of the drums in the app
    unique_ids = [e.unique_id for e in (*sensors.values(), *flags.values())]
    assert len(set(unique_ids)) == len(unique_ids)
    assert f"{MAC}_CASWD129C1C_wash_program" in unique_ids


async def test_values_seen_on_the_machine(hass):
    """Read from the cloud while the machine ran: care "Шерсть", then drying "Авто" with plasma."""
    wm = washer(hass)
    care = sensor.HaierWMCareProgramSensor(wm)
    mode = sensor.HaierWMDryModeSensor(wm)
    level = sensor.HaierWMDryLevelSensor(wm)
    plasma = binary_sensor.HaierWMPlasmaSterilizationSensor(wm)
    for code, value in {"44": "2", "50": "41", "58": "8", "48": "0"}.items():
        wm._apply_value(code, value)
    assert (mode.native_value, care.native_value, level.native_value) == ("care", "care_wool", None)
    for code, value in {"44": "1", "50": "43", "58": "0", "48": "3", "35": "1"}.items():
        wm._apply_value(code, value)
    assert (mode.native_value, care.native_value, level.native_value, plasma.is_on) == (
        "dry", "care_none", "dry_closet", True)


async def test_pushed_values(hass):
    wm = washer(hass)
    program = sensor.HaierWMWashProgramSensor(wm)
    left = sensor.HaierWMWashRemainingTimeSensor(wm)
    wm._apply_value("0", 45)  # a program of the app ("Джинсы", "Шторы"...)
    wm._apply_value("43", 1)
    wm._apply_value("45", 15)
    assert (program.native_value, left.native_value) == ("wash_app", 75)
    wm._apply_value("0", 12345)  # a program the profile does not know
    assert program.native_value is None


async def test_dryer_filter_alarm(hass):
    """[E6] of the machine's error table: the filter sensor and "Неисправность" are on."""
    wm = washer(hass)
    flags = {type(e): e for e in wm.create_entities_binary_sensor()}
    filter_sensor = flags[binary_sensor.HaierWMDryerFilterSensor]
    problem = flags[binary_sensor.HaierProblemSensor]
    assert (filter_sensor.is_on, problem.is_on) == (False, False)
    wm._set_alarms(["dryerFilterBlockedAlarm"])
    assert (filter_sensor.is_on, problem.is_on) == (True, True)
    assert wm.alarm_texts == ["[E6] Засорен фильтр- осушитель."]
    wm._set_alarms(["dryerFanErr"])
    assert (filter_sensor.is_on, problem.is_on) == (False, True)


async def test_a_washing_seen_on_the_machine(hass):
    """"Быстрая", 25 min: the phases as the cloud pushed them, then the end."""
    wm = washer(hass)
    phase = sensor.HaierWMWashPhaseSensor(wm)
    running = binary_sensor.HaierWMWasherRunningSensor(wm)
    seen = []
    for values in ({"0": "4", "23": "1", "22": "4", "45": "25"}, {"22": "5"}, {"22": "11", "45": "4"},
                   {"23": "2", "22": "14", "45": "1"}):
        for code, value in values.items():
            wm._apply_value(code, value)
        seen.append((phase.native_value, running.is_on))
    assert seen == [("washing", True), ("rinsing", True), ("spinning", True), ("finished", False)]
    wm._apply_value("22", "15")
    assert phase.native_value == "delayed_start"


async def test_a_care_seen_on_the_machine(hass):
    """Care "Рубашки", 20 min: care, a phase not named yet (5) in the last minutes, finished."""
    wm = washer(hass)
    phase = sensor.HaierWMDryPhaseSensor(wm)
    running = binary_sensor.HaierWMDryerRunningSensor(wm)
    seen = []
    for values in ({"44": "2", "58": "14", "6": "1", "51": "9"}, {"51": "5"}, {"6": "2", "51": "6"}):
        for code, value in values.items():
            wm._apply_value(code, value)
        seen.append((phase.native_value, running.is_on))
    assert seen == [("care", True), (None, True), ("finished", False)]
