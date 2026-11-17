"""Casarte CASWD129C1CU, a washer-dryer of the old cloud format (HaierWM, CASWD129C1C.yaml).

The description here is made up from the codes of the profile, with the
descriptions the old format gives several attributes ("Температура",
"Программа"...); to be replaced with the machine's diagnostics.
"""
from __future__ import annotations

from types import SimpleNamespace

from custom_components.haier_evo import binary_sensor, sensor
from custom_components.haier_evo.device_wm import HaierWM

MAC = "a0:b7:65:00:00:01"


def attribute(code: str, description: str, value: str) -> dict:
    return {"name": code, "description": description, "currentValue": value, "type": "LIST"}


DESCRIPTION = {
    "info": {"model": "CASWD129C1CU", "serialNumber": "x"},
    "status": "ONLINE",
    "settings": {"name": {"name": "Стиральная машина"}},
    "attributes": [
        attribute("0", "Программа", "10"),
        attribute("1", "Стирка и сушка", "1"),
        attribute("4", "Удалённое управление", "0"),
        attribute("8", "Соединение", "1"),
        attribute("19", "Статус", "true"),
        attribute("27", "Оставшееся время", "40"),
        attribute("43", "Оставшееся время", "1"),
        attribute("44", "Режим", "1"),
        attribute("45", "Оставшееся время", "25"),
        attribute("48", "Температура", "2"),
        attribute("50", "Программа", "1"),
        attribute("76", "Температура", "40"),
        attribute("87", "Скорость отжима", "1000"),
        attribute("99", "Температура", "0"),  # not in the profile
    ],
}


def washer(hass) -> HaierWM:
    hub = SimpleNamespace(hass=hass, cloud_available=True, laundry_control=False)
    return HaierWM(haier=hub, device_mac=MAC, device_serial="x", device_title="Стиральная машина",
                   backend_data=DESCRIPTION)


async def test_the_profile_is_found_for_the_cut_model(hass):
    wm = washer(hass)
    assert wm.device_model == "CASWD129C1C"
    assert wm.config._find_config_file().endswith("CASWD129C1C.yaml")


async def test_attributes_are_named_by_code_not_by_description(hass):
    wm = washer(hass)
    names = {str(a.code): a.name for a in wm.config.attrs}
    assert {code: names[code] for code in ("0", "19", "27", "48", "50", "76", "87", "99")} == {
        "0": "wash_program", "19": "status", "27": "dry_remaining_time", "48": "dry_level",
        "50": "dry_program", "76": "wash_temperature", "87": "wash_spin_speed", "99": "unknown",
    }
    # nothing takes the names of the upstream washing machine: no commands go out
    assert [wm.config[n] for n in ("program", "temperature", "spin_speed", "remaining_time")] == [None] * 4
    assert wm.create_entities_select() == []


async def test_values_and_entities(hass):
    wm = washer(hass)
    sensors = {type(e): e for e in wm.create_entities_sensor()}
    flags = {type(e): e for e in wm.create_entities_binary_sensor()}
    assert {cls.feature: sensors[cls].native_value for cls in sensor.WM_SENSORS} == {
        "wash_program": "wash_cotton",
        "wash_temperature": "40",
        "wash_spin_speed": "1000",
        "wash_remaining_minutes": 85,  # 1 h 25 min
        "dry_mode": "dry",
        "dry_program": "dry_cotton",
        "dry_level": "dry_closet",
        "care_program": None,  # not in the description
        "dry_remaining_time": 40,
    }
    assert sensors[sensor.HaierWMWashProgramSensor].options[:3] == ["wash_auto", "wash_shirts", "wash_bulky"]
    assert {cls.feature: flags[cls].is_on for cls in binary_sensor.WM_FLAG_SENSORS} == {
        "connection": True, "remote_control": False, "wash_dry_mode": True, "delayed_start": False,
        "auto_detergent": False, "auto_conditioner": False, "steam_treatment": False,
        "uv_sterilization": False, "plasma_sterilization": False,
    }
    assert wm.status == "on"
    unique_ids = [e.unique_id for e in (*sensors.values(), *flags.values())]
    assert len(set(unique_ids)) == len(unique_ids)
    assert f"{MAC}_CASWD129C1C_wash_program" in unique_ids


async def test_pushed_values(hass):
    wm = washer(hass)
    program = sensor.HaierWMWashProgramSensor(wm)
    left = sensor.HaierWMWashRemainingTimeSensor(wm)
    wm._apply_value("0", 17)
    wm._apply_value("43", 0)
    wm._apply_value("45", 7)
    assert (program.native_value, left.native_value) == ("wash_wool", 7)
    wm._apply_value("0", 12345)  # a program the profile does not know
    assert program.native_value is None
