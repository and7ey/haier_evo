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

import pytest

from custom_components.haier_evo import binary_sensor, sensor
from custom_components.haier_evo.device_wm import HaierWM

from .conftest import load_fixture

ITEM = load_fixture("caswd_dump.json")["devices"][0]
MAC = ITEM["device_id"]


def washer(hass, backend_data: dict | None = None) -> HaierWM:
    hub = SimpleNamespace(hass=hass, cloud_available=True, laundry_control=False)
    return HaierWM(haier=hub, device_mac=MAC, device_serial="x", device_title=ITEM["device_name"],
                   backend_data=copy.deepcopy(backend_data or ITEM["backend_data"]))


def push(wm: HaierWM, properties: dict | None = None, **status) -> None:
    """A status message of the cloud: its values, then the entities are told."""
    wm.on_message({"event": "status", "macAddress": MAC,
                   "payload": {"statuses": [{"properties": properties or {}, **status}]}})


def rest(**values: str) -> dict:
    """A REST status of the machine with these values (by code) and ONLINE."""
    data = copy.deepcopy(ITEM["backend_data"])
    data["status"] = "ONLINE"
    for attr in data["attributes"]:
        if attr["name"] in values:
            attr["currentValue"] = values[attr["name"]]
    return data


@pytest.fixture
def clock(monkeypatch):
    """time.time() of device_wm, moved by the test."""
    import custom_components.haier_evo.device_wm as device_wm

    now = [1_000_000.0]
    monkeypatch.setattr(device_wm.time, "time", lambda: now[0])
    return now


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
        "washer_running": False, "dryer_running": False, "washer_remote_control": False,
        "dryer_remote_control": False, "child_lock": False, "wash_dry_mode": False, "delayed_start": False,
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
    for values in ({"0": "4", "23": "1", "22": "4", "45": "25"}, {"22": "5"}, {"22": "9", "45": "10"},
                   {"22": "11", "45": "4"}, {"23": "2", "22": "14", "45": "1"}):
        for code, value in values.items():
            wm._apply_value(code, value)
        seen.append((phase.native_value, running.is_on))
    assert seen == [("washing", True), ("rinsing", True), ("final_rinse", True), ("spinning", True), ("finished", False)]
    wm._apply_value("22", "15")
    assert phase.native_value == "delayed_start"


async def test_a_care_seen_on_the_machine(hass):
    """Care "Рубашки", 20 min: care, finishing (5) in the last minutes, finished."""
    wm = washer(hass)
    phase = sensor.HaierWMDryPhaseSensor(wm)
    running = binary_sensor.HaierWMDryerRunningSensor(wm)
    seen = []
    for values in ({"44": "2", "58": "14", "6": "1", "51": "9"}, {"51": "5"}, {"6": "2", "51": "6"}):
        for code, value in values.items():
            wm._apply_value(code, value)
        seen.append((phase.native_value, running.is_on))
    assert seen == [("care", True), ("finishing", True), ("finished", False)]


async def test_a_lagging_rest_status_does_not_undo_a_push(hass, clock):
    """Seen on the machine: the power-off pushed, the periodic read 22 s later still "on" and ONLINE."""
    wm = washer(hass)
    assert wm.status == "on"
    stale = copy.deepcopy(ITEM["backend_data"])
    stale["status"] = "ONLINE"
    for attr in stale["attributes"]:
        if attr["name"] == "8":
            attr["currentValue"] = "1"

    wm.on_message({"event": "status", "macAddress": MAC, "payload": {"statuses": [{"properties": {"8": "0"}}]}})
    wm.on_message({"event": "deviceStatusEvent", "macAddress": MAC, "payload": {"status": "TURNED_OFF"}})
    assert wm.status == "off"
    clock[0] += 22
    wm.update_from_backend(copy.deepcopy(stale), requested_at=clock[0])
    assert wm.status == "off"
    # what the cache saves is what the entities show
    saved = {a["name"]: a.get("currentValue") for a in wm.status_data["attributes"]}
    assert (saved["8"], wm.status_data["status"]) == ("0", "TURNED_OFF")
    # long after the push, REST is the only source again (a lost push)
    clock[0] += wm.PUSH_WINS_FOR
    wm.update_from_backend(copy.deepcopy(stale), requested_at=clock[0])
    assert (wm.status, wm.status_data["status"]) == ("on", "ONLINE")


async def test_a_drying_seen_on_the_machine(hass):
    """"Быстрая" after a washing: drying, final drying 15 min before the end, finishing, finished."""
    wm = washer(hass)
    phase = sensor.HaierWMDryPhaseSensor(wm)
    seen = []
    for values in ({"44": "1", "6": "1", "51": "8", "27": "58"}, {"51": "4", "27": "15"},
                   {"51": "5", "27": "1"}, {"6": "2", "51": "6"}):
        for code, value in values.items():
            wm._apply_value(code, value)
        seen.append(phase.native_value)
    assert seen == ["drying", "final_drying", "finishing", "finished"]


async def test_an_unknown_option_is_unknown(hass):
    """An option the cloud has not reported is neither on nor off."""
    data = copy.deepcopy(ITEM["backend_data"])
    for attr in data["attributes"]:
        if attr["name"] == "13":  # steam_treatment
            attr["currentValue"] = None
    assert binary_sensor.HaierWMSteamSensor(washer(hass, data)).is_on is None


async def test_a_bad_time_does_not_block_the_next_message(hass):
    """"NaN" for the minutes: no time, and a message that corrects it is applied whole."""
    wm = washer(hass)
    left = sensor.HaierWMWashRemainingTimeSensor(wm)
    push(wm, {"45": "NaN"})
    assert left.native_value is None
    push(wm, {"22": "0", "45": "1"})
    assert (left.native_value, wm.value_of("wash_phase")) == (1, "none")


async def test_the_hours_and_minutes_are_one_reading(hass, clock):
    """1:01, a push of the minutes "00" at the hour, a lagging REST of 0:59: not 0:00."""
    wm = washer(hass)
    left = sensor.HaierWMWashRemainingTimeSensor(wm)
    wm.update_from_backend(rest(**{"43": "1", "45": "1"}))
    assert left.native_value == 61
    push(wm, {"45": "0"})
    assert left.native_value == 60
    clock[0] += 60
    wm.update_from_backend(rest(**{"43": "0", "45": "59"}))
    assert left.native_value == 60
    saved = {a["name"]: a.get("currentValue") for a in wm.status_data["attributes"]}
    assert (saved["43"], saved["45"]) == ("1", "0")


async def test_a_rest_status_that_agrees_with_the_pushed_minutes_brings_the_hour(hass, clock):
    """1:00, the push of the hour 0 lost, the minutes pushed on; REST of 0:44 with the pushed 44: 44 min."""
    wm = washer(hass)
    left = sensor.HaierWMWashRemainingTimeSensor(wm)
    wm.update_from_backend(rest(**{"43": "1", "45": "0"}))
    for minutes in range(59, 43, -1):
        clock[0] += 60
        push(wm, {"45": str(minutes)})
    assert left.native_value == 104
    wm.update_from_backend(rest(**{"43": "0", "45": "44"}))
    assert left.native_value == 44
    saved = {a["name"]: a.get("currentValue") for a in wm.status_data["attributes"]}
    assert (saved["43"], saved["45"]) == ("0", "44")


async def test_a_lagging_rest_status_does_not_clear_a_pushed_alarm(hass, clock):
    wm = washer(hass)
    flags = {type(e): e for e in wm.create_entities_binary_sensor()}
    filter_sensor = flags[binary_sensor.HaierWMDryerFilterSensor]
    push(wm, alarms=["dryerFilterBlockedAlarm"])
    clock[0] += 20
    wm.update_from_backend(rest())  # alarms: []
    assert (filter_sensor.is_on, flags[binary_sensor.HaierProblemSensor].is_on) == (True, True)
    assert wm.status_data["alarms"] == ["dryerFilterBlockedAlarm"]
    # the machine cleared it: an empty list pushed counts, a lagging REST does not bring it back
    push(wm, alarms=[])
    clock[0] += 20
    stale = rest()
    stale["alarms"] = ["dryerFilterBlockedAlarm"]
    wm.update_from_backend(stale)
    assert filter_sensor.is_on is False


async def test_alarms_null_are_not_a_pushed_list(hass, clock):
    """E6, a push with "alarms": null, a REST status with none: the alarm does not come back."""
    wm = washer(hass)
    filter_sensor = binary_sensor.HaierWMDryerFilterSensor(wm)
    wm.update_from_backend(rest())
    push(wm, alarms=["dryerFilterBlockedAlarm"])
    clock[0] += 20
    push(wm, alarms=None)
    clock[0] += 20
    wm.update_from_backend(rest())
    assert filter_sensor.is_on is False


async def test_a_restart_keeps_a_fresh_push(hass, clock):
    """Off, a restart from the saved description, a lagging REST before the first WebSocket message."""
    from custom_components.haier_evo.api import cacheable_description

    wm = washer(hass)
    push(wm, {"8": "0"})
    wm.on_message({"event": "deviceStatusEvent", "macAddress": MAC, "payload": {"status": "TURNED_OFF"}})
    saved = cacheable_description(wm.status_data)
    clock[0] += 30
    again = washer(hass, saved)
    assert again.status == "off"
    again.update_from_backend(rest(**{"8": "1"}))
    assert (again.status, again.status_data["status"]) == ("off", "TURNED_OFF")
    # later than PUSH_WINS_FOR the REST state is taken
    clock[0] += again.PUSH_WINS_FOR
    again.update_from_backend(rest(**{"8": "1"}))
    assert (again.status, again.status_data["status"]) == ("on", "ONLINE")


async def test_a_rebuild_from_the_cloud_keeps_a_fresh_push(hass, clock):
    """Another unit added: the entry reads the cloud, the lagging REST must not undo off and E6."""
    from custom_components.haier_evo.api import Haier, cacheable_description

    wm = washer(hass)
    push(wm, {"8": "0"}, alarms=["dryerFilterBlockedAlarm"])
    wm.on_message({"event": "deviceStatusEvent", "macAddress": MAC, "payload": {"status": "TURNED_OFF"}})
    cached = {"devices": [{"mac": MAC.upper(), "status": cacheable_description(wm.status_data)}]}
    clock[0] += 60
    rebuilt = washer(hass, rest(**{"8": "1"}))  # what the cloud said at the rebuild
    assert rebuilt.status == "on"
    hub = SimpleNamespace(devices=[rebuilt])
    Haier.carry_fresh(hub, cached)
    assert (rebuilt.status, rebuilt.status_data["status"], rebuilt.alarms) == (
        "off", "TURNED_OFF", ["dryerFilterBlockedAlarm"])
    # and the cache it saves next keeps them, with the times
    assert rebuilt.status_data["pushedAt"].keys() >= {"8", "status", "alarms"}
    # pushed longer ago than PUSH_WINS_FOR: the cloud's state stays
    clock[0] += rebuilt.PUSH_WINS_FOR
    later = washer(hass, rest(**{"8": "1"}))
    Haier.carry_fresh(SimpleNamespace(devices=[later]), cached)
    assert (later.status, later.alarms) == ("on", [])


async def test_without_the_profile_flag_rest_is_taken(hass, clock):
    """Other machines of the old format: a read-back of a command is what the machine has."""
    wm = washer(hass)
    wm.config._config["rest_lags_behind_push"] = False
    push(wm, {"76": "30"})
    wm.update_from_backend(rest(**{"76": "40"}))
    assert wm.value_of("wash_temperature") == "40"
    assert "pushedAt" not in wm.status_data


async def test_programs_of_the_app_for_drying(hass):
    """"Сушка на полке", "Спортивная", "Подогрев одежды", "Сушка по времени": 50=42 in Evo."""
    wm = washer(hass)
    program = sensor.HaierWMDryProgramSensor(wm)
    push(wm, {"50": "42"})
    assert program.native_value == "dry_app"


async def test_a_partial_profile_of_another_machine_keeps_the_descriptions(tmp_path):
    """A user's profile that changes the code of the spin only: the rest is still named by description."""
    from custom_components.haier_evo import config as CFG  # noqa: N812

    user = tmp_path / "haier_evo"
    (tmp_path / "haier_evo.HW60OLD.yaml").write_text(
        'attributes:\n  - name: spin_speed\n    id: "7"\n', encoding="utf-8")
    cfg = CFG.HaierWMConfig("HW60OLD", str(user))
    for code, description in (("1", "Программа"), ("2", "Температура"), ("7", "Скорость отжима"),
                              ("9", "Оставшееся время")):
        cfg.attrs.append(CFG.Attribute({"name": code, "description": description, "currentValue": "0"}))
    cfg.merge_attributes()
    assert [cfg[n] for n in ("program", "temperature", "spin_speed", "remaining_time")] == ["1", "2", "7", "9"]
