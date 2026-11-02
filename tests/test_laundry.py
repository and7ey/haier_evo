import json
import asyncio
import logging
from pathlib import Path
import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_component import EntityComponent
from unittest.mock import Mock, patch

from custom_components.haier_evo import api


class FakeCloud:
    def __init__(self, data):
        self.data = data
        self._laundry_poll_lock = asyncio.Lock()
        self._laundry_retry_until = 0.0
        self._laundry_next_request = 0.0
        self.hass = Mock()
        self.hass.config.language = "ru"
        self.socket_status = api.SocketStatus.INITIALIZED
        self.devices = []
        self.socket_app = Mock()
        self.socket_app.sock.connected = True

    def pull_device_data(self, device_mac):
        return self.data


def create(kind, data):
    cloud = FakeCloud(data)
    device = api.HaierDevice.create(cloud, kind, "synthetic-device", device_title="Machine")
    return cloud, device


def test_dryer_initial_data_and_program_event():
    data = {
        "smartDeviceControl": {"info": {"model": "SYNTHETIC"}, "status": "ONLINE",
            "attributes": [{"name": "22", "currentValue": "4", "readable": True}]},
        "device": {"monitoringScreen": {"phase": {"phase": {"name": "22"},
            "cyclePhaseValueToTitle": {"4": "Heating"}}}},
        "program": {"status": "IN_PROGRESS", "title": "Delicate"},
    }
    cloud, device = create("DRY", data)
    assert isinstance(device, api.HaierLaundry)
    assert device.available and device.phase == "Heating"
    sensors = {s._device_attr_name: s for s in device.create_entities_sensor()}
    assert set(sensors) == {"phase", "program_status", "program_name"}
    assert sensors["phase"].translation_key == "laundry_phase"
    assert sensors["phase"].native_value == "Heating"
    assert sensors["program_status"].native_value == "running"
    callback = Mock()
    device.add_write_ha_state_callback(callback)
    device.on_message({"event": "ProgramDryerBaseEvent", "data": {
        "status": "PROGRAM_FINISHED", "program": {"status": "FINISHED", "title": "Delicate"}}})
    assert device.program_status == "finished"
    assert sensors["program_status"].native_value == "finished"
    assert device.program_name == "Delicate"
    cloud.hass.loop.call_soon_threadsafe.assert_any_call(callback)


def test_washer_phase_timer_and_offline_transition():
    data = {
        "smartDeviceControl": {"info": {"model": "SYNTHETIC"}, "status": "OFFLINE",
            "attributes": [{"name": "18", "currentValue": "0", "readable": True},
                {"name": "32", "currentValue": "1", "readable": True},
                {"name": "33", "currentValue": "28", "readable": True}]},
        "allProgram": {"monitoringScreen": {"phase": {"name": "18"},
            "timer": {"remainingTimeHH": {"name": "32"}, "remainingTimeMM": {"name": "33"}}}},
    }
    cloud, device = create("WM", data)
    assert not device.available and device.phase == "Код 0" and device.remaining_time == 88
    sensors = {s._device_attr_name: s for s in device.create_entities_sensor()}
    assert set(sensors) == {"phase", "remaining_time", "program_status", "program_name"}
    assert sensors["phase"].entity_category == "diagnostic"
    assert sensors["remaining_time"].translation_key == "laundry_program_time"
    assert sensors["remaining_time"].device_class == "duration"
    assert sensors["phase"].translation_key == "laundry_phase_code"
    device.on_message({"event": "status", "payload": {"statuses": [{"properties": {
        "18": "2", "32": "0", "33": "20"}}]}})
    assert device.available and device.phase == "Код 2" and device.remaining_time == 20
    device.on_message({"event": "deviceStatusEvent", "payload": {"status": "OFFLINE"}})
    assert not device.available
    device._set_attribute_value("33", "bad")
    assert device.remaining_time is None


def test_legacy_washer_still_uses_existing_class():
    cloud = FakeCloud({"attributes": []})
    with patch.object(api, "HaierWM") as legacy:
        api.HaierDevice.create(cloud, "WM", "synthetic-device")
    legacy.assert_called_once()


def test_unknown_program_values_remain_unknown():
    data = {"smartDeviceControl": {"info": {"model": "SYNTHETIC"}, "status": "ONLINE", "attributes": []},
            "device": {"monitoringScreen": {}}}
    cloud, device = create("DRY", data)
    assert device.program_status is None and device.program_name is None
    assert {s._device_attr_name for s in device.create_entities_sensor()} == {"program_status", "program_name"}


def test_incomplete_messages_do_not_fabricate_state():
    data = {"smartDeviceControl": {"info": {"model": "SYNTHETIC"}, "attributes": []},
            "device": {"monitoringScreen": {}}, "program": {"status": "FINISHED"}}
    cloud, device = create("DRY", data)
    assert not device.available
    device.on_message({"event": "status", "payload": {"statuses": [{}]}})
    device.on_message({"event": "deviceStatusEvent", "payload": {}})
    device.on_message({"event": "ProgramDryerBaseEvent", "data": {"program": "unexpected"}})
    assert not device.available and device.program_status == "finished"


@pytest.mark.asyncio
async def test_home_assistant_updates_phase_after_status_event(hass):
    data = {
        "smartDeviceControl": {"info": {"model": "SYNTHETIC"}, "status": "ONLINE",
            "attributes": [{"name": "22", "currentValue": "4", "readable": True}]},
        "device": {"monitoringScreen": {"phase": {"phase": {"name": "22"},
            "cyclePhaseValueToTitle": {"4": "Heating", "5": "Cooling"}}}},
    }
    cloud = FakeCloud(data)
    cloud.hass = hass
    device = api.HaierDevice.create(cloud, "DRY", "synthetic-device", device_title="Machine")
    entities = device.create_entities_sensor()
    component = EntityComponent(logging.getLogger(__name__), "sensor", hass)
    await component.async_add_entities(entities)
    phase = next(e for e in entities if e._device_attr_name == "phase")
    assert hass.states.get(phase.entity_id).state == "Heating"
    device.on_message({"event": "status", "payload": {"statuses": [{"properties": {"22": "5"}}]}})
    await hass.async_block_till_done()
    assert hass.states.get(phase.entity_id).state == "Cooling"


def test_existing_root_washer_attributes_keep_legacy_support():
    cloud = FakeCloud({"attributes": [{"name": "legacy"}], "smartDeviceControl": {}})
    with patch.object(api, "HaierWM") as legacy:
        api.HaierDevice.create(cloud, "WM", "synthetic-device")
    legacy.assert_called_once()


def test_reconnect_waits_for_fresh_status():
    data = {"smartDeviceControl": {"info": {"model": "SYNTHETIC"}, "status": "ONLINE", "attributes": []},
            "device": {"monitoringScreen": {}}}
    cloud, device = create("DRY", data)
    cloud.devices = [device]
    assert device.available
    api.Haier._on_close(cloud, None, None, None)
    assert not device.available and device.values == {}
    api.Haier._on_open(cloud, None)
    assert not device.available
    device.on_message({"event": "status", "payload": {"statuses": [{"properties": {}}]}})
    assert not device.available
    device.on_message({"event": "status", "payload": {"statuses": [{"properties": {"22": "4"}}]}})
    assert device.available


def control_fixture():
    return {
        "smartDeviceControl": {"info": {"model": "SYNTHETIC"}, "status": "ONLINE",
            "attributes": [
                {"name": "24", "currentValue": "1", "readable": True},
                {"name": "18", "currentValue": "0", "readable": True},
                {"name": "19", "currentValue": "2", "readable": True, "writable": True},
                {"name": "144", "writable": True},
            ],
            "groupCommands": [{"name": "8", "attrNameList": ["20", "21"]}],
            "businessAttributes": [
                {"name": "complete", "commandParameters": {"name": "8", "attrNameList": [
                    {"name": "20", "defaultValue": "1"}, {"name": "21", "defaultValue": "2"}]}},
                {"name": "incomplete", "commandParameters": {"name": "8", "attrNameList": [
                    {"name": "20", "defaultValue": "1"}, {"name": "21"}]}},
            ]},
        "remoteControlMode": {"link": {"name": "24"}},
        "controlBlock": {
            "pause": {"link": {"name": "19"}, "pauseValue": "2"},
            "resumeProgram": {"link": {"name": "19"}, "resumeValue": "1"},
            "cancel": {"link": {"name": "144"}, "cancelValue": "1"},
        },
        "allProgram": {"monitoringScreen": {"phase": {"name": "18", "cyclePhaseValueToTitle": {"0": "Standby"}}},
                       "blocks": [{"title": "Fabric", "programs": [
            {"preview": {"name": "Cotton"}, "programConfig": {"link": {"name": "complete"}, "selectedValues": []}},
            {"preview": {"name": "Missing"}, "programConfig": {"link": {"name": "incomplete"}, "selectedValues": []}},
        ]}]},
    }


def test_selection_is_local_and_start_requires_explicit_action():
    cloud, device = create("WM", control_fixture())
    assert list(device.programs) == ["Fabric - Cotton"]
    select = device.create_entities_select()[0]
    assert select.current_option == "Не выбрана"
    with pytest.raises(HomeAssistantError):
        device.send_laundry_action("start")
    select.set_option("Fabric - Cotton")
    cloud.socket_app.send.assert_not_called()
    assert select.current_option == "Fabric - Cotton"
    device.send_laundry_action("start")
    payload = json.loads(cloud.socket_app.send.call_args.args[0])
    assert payload["action"] == "operation" and payload["commandName"] == "8"
    assert payload["commands"] == [{"commandName": "20", "value": "1"}, {"commandName": "21", "value": "2"}]



def test_start_cannot_be_repeated_without_reselecting_even_after_send_failure():
    cloud, device = create("WM", control_fixture())
    device.set_selected_program("Fabric - Cotton")
    device.send_laundry_action("start")
    assert device.selected_program is None
    with pytest.raises(HomeAssistantError):
        device.send_laundry_action("start")
    assert cloud.socket_app.send.call_count == 1
    device.set_selected_program("Fabric - Cotton")
    with pytest.raises(HomeAssistantError):
        device.send_laundry_action("start")
    assert cloud.socket_app.send.call_count == 1
    device.on_message({"event": "status", "payload": {"statuses": [{"properties": {"18": "1"}}]}})
    device.on_message({"event": "status", "payload": {"statuses": [{"properties": {"18": "0"}}]}})
    cloud.socket_app.send.side_effect = OSError("synthetic failure")
    with pytest.raises(HomeAssistantError):
        device.send_laundry_action("start")
    assert device.selected_program is None
    with pytest.raises(HomeAssistantError):
        device.send_laundry_action("start")
    assert cloud.socket_app.send.call_count == 2

def test_actions_require_remote_access_and_a_connected_socket():
    cloud, device = create("WM", control_fixture())
    assert set(device.actions) == {"pause", "resume", "cancel"}
    for action, command, value in [("pause", "19", "2"), ("resume", "19", "1"), ("cancel", "144", "1")]:
        device._program_status = "PAUSED" if action == "resume" else "IN_PROGRESS"
        device.send_laundry_action(action)
        payload = json.loads(cloud.socket_app.send.call_args.args[0])
        assert payload["action"] == "command"
        assert payload["command"] == {"commandName": command, "value": value}
    cloud.socket_app.send.reset_mock()
    device._set_attribute_value("24", "0")
    with pytest.raises(HomeAssistantError):
        device.send_laundry_action("resume")
    device._set_attribute_value("24", "1")
    cloud.socket_app.sock.connected = False
    with pytest.raises(HomeAssistantError):
        device.send_laundry_action("pause")
    cloud.socket_app.send.assert_not_called()


def test_send_failure_is_reported_without_retry():
    cloud, device = create("WM", control_fixture())
    cloud.socket_app.send.side_effect = OSError("synthetic failure")
    device._program_status = "IN_PROGRESS"
    with pytest.raises(HomeAssistantError):
        device.send_laundry_action("pause")
    assert cloud.socket_app.send.call_count == 1


def test_translations_and_icons_cover_new_entities():
    root = Path(__file__).resolve().parents[1] / "custom_components/haier_evo"
    icons = json.loads((root / "icons.json").read_text())
    for lang in ("en", "ru"):
        strings = json.loads((root / "translations" / f"{lang}.json").read_text())
        for domain, entries in icons["entity"].items():
            for key in entries:
                assert strings["entity"][domain][key]["name"]


def test_idle_is_used_only_when_device_reports_standby():
    data = {"smartDeviceControl": {"info": {"model": "SYNTHETIC"}, "status": "ONLINE",
            "attributes": [{"name": "22", "currentValue": "0", "readable": True}]},
            "device": {"monitoringScreen": {"phase": {"phase": {"name": "22"},
                "cyclePhaseValueToTitle": {"0": "Режим ожидания"}}}}}
    cloud, device = create("DRY", data)
    cloud.hass.config.language = "ru"
    assert device.program_status == "idle"
    assert device.program_name == "Нет активной программы"
    device.phase_labels["0"] = "Unknown meaning"
    assert device.program_status is None and device.program_name is None


def test_invalid_programs_and_actions_are_not_exposed():
    data = control_fixture()
    fields = data["smartDeviceControl"]["businessAttributes"][0]["commandParameters"]["attrNameList"]
    fields[0]["name"] = "not-in-group"
    next(attr for attr in data["smartDeviceControl"]["attributes"] if attr["name"] == "19")["list"] = {"data": [{"data": "1"}]}
    cloud, device = create("WM", data)
    assert not device.programs
    assert "pause" not in device.actions and "resume" in device.actions
    with pytest.raises(HomeAssistantError):
        device.send_laundry_action("pause")
    cloud.socket_app.send.assert_not_called()


def test_missing_or_duplicate_titles_do_not_choose_wrong_program():
    data = control_fixture()
    entry = data["allProgram"]["blocks"][0]["programs"][0]
    data["allProgram"]["blocks"][0]["programs"].append(entry.copy())
    cloud, device = create("WM", data)
    assert not device.programs
    assert not device.create_entities_select()



def test_program_parameters_outside_server_limits_are_rejected():
    data = control_fixture()
    field = data["smartDeviceControl"]["businessAttributes"][0]["commandParameters"]["attrNameList"][0]
    field["valueRange"] = {"list": {"data": [{"data": "2"}]}}
    assert not create("WM", data)[1].programs
    field["valueRange"] = {"range": {"data": {"minValue": "2", "maxValue": "5"}}}
    assert not create("WM", data)[1].programs


def test_duplicate_business_definitions_are_rejected():
    data = control_fixture()
    data["smartDeviceControl"]["businessAttributes"].append(
        data["smartDeviceControl"]["businessAttributes"][0].copy())
    assert not create("WM", data)[1].programs


def test_duplicate_or_malformed_groups_are_rejected():
    data = control_fixture()
    data["smartDeviceControl"]["groupCommands"].append(
        data["smartDeviceControl"]["groupCommands"][0].copy())
    assert not create("WM", data)[1].programs
    data["smartDeviceControl"]["groupCommands"] = "invalid"
    assert not create("WM", data)[1].programs


def test_washer_uses_server_phase_labels_when_available():
    data = control_fixture()
    data["smartDeviceControl"]["attributes"].append({"name": "18", "currentValue": "14", "readable": True})
    data["allProgram"]["monitoringScreen"] = {"phase": {"name": "18"},
        "finishing": {"finishingAttrValue": "14", "title": "Finishing"}}
    cloud, device = create("WM", data)
    assert device.phase == "Finishing"


def test_explicit_commands_work_when_cycle_state_is_unknown():
    data = control_fixture()
    data["allProgram"]["monitoringScreen"]["phase"]["cyclePhaseValueToTitle"] = {}
    cloud, device = create("WM", data)
    device.set_selected_program("Fabric - Cotton")
    assert set(device.available_actions) == {"start", "pause", "resume", "cancel"}
    device.send_laundry_action("pause")
    assert json.loads(cloud.socket_app.send.call_args.args[0])["command"]["value"] == "2"
    device.send_laundry_action("start")
    device.set_selected_program("Fabric - Cotton")
    with pytest.raises(HomeAssistantError):
        device.send_laundry_action("start")
    with pytest.raises(HomeAssistantError):
        device.send_laundry_action("pause")
    assert cloud.socket_app.send.call_count == 2


def test_finished_event_without_nested_program_and_phase_changes_clear_stale_status():
    data = control_fixture()
    cloud, device = create("WM", data)
    device.on_message({"event": "ProgramDryerBaseEvent", "data": {"status": "PROGRAM_FINISHED"}})
    assert device.program_status == "finished"
    device.on_message({"event": "status", "payload": {"statuses": [{"properties": {"18": "2"}}]}})
    assert device.program_status is None


def test_offline_online_requires_fresh_data_before_commands():
    cloud, device = create("WM", control_fixture())
    device.set_selected_program("Fabric - Cotton")
    device.on_message({"event": "deviceStatusEvent", "payload": {"status": "OFFLINE"}})
    device.on_message({"event": "deviceStatusEvent", "payload": {"status": "ONLINE"}})
    assert not device.available and not device.values and device.selected_program is None
    with pytest.raises(HomeAssistantError):
        device.send_laundry_action("start")
    cloud.socket_app.send.assert_not_called()
    device.on_message({"event": "status", "payload": {"statuses": [{"properties": {"18": "0", "24": "1"}}]}})
    assert device.available and device.program_status == "idle"


def test_start_stays_pending_after_event_without_confirmed_status():
    cloud, device = create("WM", control_fixture())
    device.set_selected_program("Fabric - Cotton")
    device.send_laundry_action("start")
    device.on_message({"event": "ProgramDryerBaseEvent", "data": {"program": {"title": "Cotton"}}})
    device.set_selected_program("Fabric - Cotton")
    with pytest.raises(HomeAssistantError):
        device.send_laundry_action("start")
    assert cloud.socket_app.send.call_count == 1


def dynamic_delay_fixture():
    data = control_fixture()
    data["controlBlock"]["delayStart"] = {
        "link": {"name": "delay-hours"}, "statusLink": {"name": "schedule-enabled"}}
    data["smartDeviceControl"]["attributes"].append({
        "name": "schedule-enabled", "writable": True,
        "defaultValue": "true", "currentValue": "0",
        "list": {"data": [{"data": "false"}, {"data": "true"}]}})
    data["smartDeviceControl"]["groupCommands"][0]["attrNameList"] += ["delay-hours", "schedule-enabled"]
    data["smartDeviceControl"]["businessAttributes"][0]["commandParameters"]["attrNameList"] += [
        {"name": "delay-hours", "defaultValue": "0.0"},
        {"name": "schedule-enabled", "valueType": "2"}]
    return data


def test_dynamic_delay_flag_uses_explicit_immediate_start_not_attribute_default():
    cloud, device = create("WM", dynamic_delay_fixture())
    commands = device.programs["Fabric - Cotton"]["commands"]
    assert commands[-2:] == [
        {"commandName": "delay-hours", "value": "0.0"},
        {"commandName": "schedule-enabled", "value": "false"}]
    cloud.socket_app.send.assert_not_called()


@pytest.mark.parametrize("invalid", ["unlinked", "read_only", "nonzero_delay", "missing_delay",
                                      "wrong_type", "invalid_choice", "duplicate_attribute"])
def test_dynamic_delay_does_not_fill_other_unknown_or_scheduled_parameters(invalid):
    data = dynamic_delay_fixture()
    fields = data["smartDeviceControl"]["businessAttributes"][0]["commandParameters"]["attrNameList"]
    attribute = data["smartDeviceControl"]["attributes"][-1]
    if invalid == "unlinked":
        data["controlBlock"]["delayStart"]["statusLink"]["name"] = "other"
    elif invalid == "read_only":
        attribute["writable"] = False
    elif invalid == "nonzero_delay":
        fields[-2]["defaultValue"] = "3.0"
    elif invalid == "missing_delay":
        fields[-2].pop("defaultValue")
    elif invalid == "wrong_type":
        fields[-1]["valueType"] = "3"
    elif invalid == "invalid_choice":
        attribute["list"]["data"] = [{"data": "true"}]
    else:
        data["smartDeviceControl"]["attributes"].append(attribute.copy())
    cloud, device = create("WM", data)
    assert not device.programs
    cloud.socket_app.send.assert_not_called()


@pytest.mark.parametrize("key", ["link", "statusLink"])
def test_malformed_delay_descriptor_does_not_break_other_programs(key):
    data = dynamic_delay_fixture()
    data["controlBlock"]["delayStart"][key] = "unexpected"
    assert not create("WM", data)[1].programs
    fields = data["smartDeviceControl"]["businessAttributes"][0]["commandParameters"]["attrNameList"]
    fields[-1]["valueType"] = "1"
    fields[-1]["defaultValue"] = "false"
    assert "Fabric - Cotton" in create("WM", data)[1].programs


def test_configuring_dependency_rejects_locked_edits_and_restores_local_preference():
    data = control_fixture()
    fields = data["smartDeviceControl"]["businessAttributes"][0]["commandParameters"]["attrNameList"]
    fields[0].update({"valueType": "3", "defaultValue": "60", "valueRange": {
        "list": {"type": "LIST", "data": [{"data": "30"}, {"data": "60"}]}}})
    fields[1].update({"valueType": "3", "defaultValue": "false", "valueRange": {
        "list": {"type": "LIST", "data": [{"data": "false"}, {"data": "true"}]}}})
    data["enrichAttributes"] = {"attrs": {"20": {"title": "Temperature"}, "21": {"title": "Steam"}}}
    data["smartDeviceControl"]["modifiers"] = [{"priority": 1, "trigger": {
        "operator": "AND", "conditions": {"condition": [{"mode": "21", "values": ["true"]}]}},
        "actions": [{"name": "20", "rewriteFields": "WD", "writable": False, "defaultValue": "60"}]}]
    cloud, device = create("WM", data)
    device.set_selected_program("Fabric - Cotton")
    device.configure_program("Fabric - Cotton", {"20": "30"})
    device.configure_program("Fabric - Cotton", {"21": "true"})
    temp = next(field for field in device.program_settings["fields"] if field["key"] == "20")
    assert temp["value"] == "60" and temp["disabled"]
    with pytest.raises(HomeAssistantError):
        device.configure_program("Fabric - Cotton", {"20": "30"})
    device.configure_program("Fabric - Cotton", {"21": "false"})
    assert device.program_settings["values"]["20"] == "30"
    cloud.socket_app.send.assert_not_called()


def test_connection_quality_event_does_not_invent_availability_or_warn(caplog):
    cloud, device = create("WM", control_fixture())
    device.mark_stale()
    device.on_message({"event": "deviceConnectionStatusEvent", "payload": {"status": "GOOD"}})
    assert device.connection_status == "GOOD" and not device.available
    assert "Got unknown message" not in caplog.text
    cloud.socket_app.send.assert_not_called()


def test_program_events_keep_current_parameters_separate_from_next_selection():
    cloud, device = create("WM", control_fixture())
    device.on_message({"event": "ProgramWasherBaseEvent", "data": {"program": {
        "status": "IN_PROGRESS", "title": "Cotton", "config": {"selectedValues": [
            {"attrName": "20", "attrValue": "2"}]}}}})
    assert device._active_program_values == {"20": "2"}
    device.set_selected_program("Fabric - Cotton")
    assert device._active_program_values == {"20": "2"}
    device.on_message({"event": "ProgramWasherBaseEvent", "data": {"program": {
        "status": "IN_PROGRESS", "title": "Other"}}})
    assert not device._active_program_values
    cloud.socket_app.send.assert_not_called()
