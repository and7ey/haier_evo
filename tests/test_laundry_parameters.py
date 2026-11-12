"""Program staging uses native entities and never changes an active cycle."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.haier_evo.device_laundry import HaierLaundry, laundry_programs
from custom_components.haier_evo.programs import build_catalogue

from . import test_devices as base

PROGRAMS = [
    (index, program["title"])
    for index, data in enumerate(base.LAUNDRY["devices"])
    for program in laundry_programs(data["backend_data"])
]


def make_machine(hass, index=1):
    data = base.LAUNDRY["devices"][index]
    cloud = SimpleNamespace(hass=hass, cloud_available=True, laundry_control=True, async_send=AsyncMock())
    device = HaierLaundry(
        haier=cloud, device_mac=data["device_id"], backend_data=deepcopy(data["backend_data"]),
    )
    device._raw[device.codes["onOffStatus"]] = "1"
    device._raw[device.codes["remoteCtrValid"]] = "1"
    device._raw[device.codes["cyclePhase"]] = "0"
    device.program_status = "FINISHED"
    return device, cloud


def change_value(device):
    field = next(
        field for field in device.program_settings["fields"]
        if not field["disabled"] and field["kind"] == "select"
        and any(option["value"] != field["value"] for option in field["options"])
    )
    return field["key"], next(option["value"] for option in field["options"] if option["value"] != field["value"])


@pytest.fixture
def machine(hass):
    device, cloud = make_machine(hass)
    yield device, cloud
    device.clear_start_pending()


@pytest.mark.parametrize(("index", "title"), PROGRAMS)
async def test_every_cloud_program_has_valid_parameters_without_changing_native_start(hass, index, title):
    device, cloud = make_machine(hass, index)
    device.set_selected_program(title)
    assert len(PROGRAMS) == 77
    settings = device.program_settings
    assert settings["program"] == title and "error" not in settings
    assert len(device.program_catalog) == (3 if index == 2 else 5)
    program = next(program for program in device.programs if program["title"] == title)
    assert device._start_commands(program) == program["commands"]
    cloud.async_send.assert_not_awaited()


async def test_staged_values_reach_native_start_once_without_optimistic_current_values(machine):
    device, cloud = machine
    device.set_selected_program("Смешанная")
    key, value = change_value(device)
    current = deepcopy(device.active_program_details)
    device.configure_program("Смешанная", {key: value})
    cloud.async_send.assert_not_awaited()
    assert device.active_program_details == current
    assert device.program_status == "FINISHED"
    await device.start()
    assert cloud.async_send.await_count == 1
    message = cloud.async_send.call_args.args[0]
    assert message["action"] == "operation"
    assert next(command["value"] for command in message["commands"] if command["commandName"] == key) == value
    assert device.active_program_details == current


@pytest.mark.parametrize("status", ["IN_PROGRESS", "PAUSED"])
async def test_running_and_paused_programs_cannot_be_reconfigured(machine, status):
    device, cloud = machine
    device.set_selected_program("Смешанная")
    device.program_status = status
    before = deepcopy(device.program_settings)
    with pytest.raises(ServiceValidationError) as error:
        device.configure_program("Смешанная", {"50": "30"})
    assert error.value.translation_key == "laundry_parameters_active"
    assert device.program_settings == before
    cloud.async_send.assert_not_awaited()


@pytest.mark.parametrize("parameters", [{"invented": "1"}, {"14": "true"}, {"50": "999"}, {}, [], {"50": float("nan")}])
async def test_invalid_parameters_leave_preferences_unchanged(machine, parameters):
    device, cloud = machine
    device.set_selected_program("Смешанная")
    before = deepcopy(device.program_settings)
    with pytest.raises(ServiceValidationError):
        device.configure_program("Смешанная", parameters)
    assert device.program_settings == before
    cloud.async_send.assert_not_awaited()


async def test_pending_or_inflight_command_blocks_staging_and_control_option_is_respected(machine):
    device, cloud = machine
    device.set_selected_program("Смешанная")
    key, value = change_value(device)
    device._start_pending = True
    with pytest.raises(ServiceValidationError) as error:
        device.configure_program("Смешанная", {key: value})
    assert error.value.translation_key == "laundry_busy"
    device._start_pending = False
    await device._control_lock.acquire()
    try:
        with pytest.raises(ServiceValidationError):
            device.configure_program("Смешанная", {key: value})
    finally:
        device._control_lock.release()
    cloud.laundry_control = False
    with pytest.raises(ServiceValidationError) as error:
        device.configure_program("Смешанная", {key: value})
    assert error.value.translation_key == "laundry_control_off"
    cloud.async_send.assert_not_awaited()


async def test_program_change_resets_preferences_and_old_program_cannot_be_configured(machine):
    device, cloud = machine
    device.set_selected_program("Смешанная")
    key, value = change_value(device)
    device.configure_program("Смешанная", {key: value})
    device.set_selected_program("Смешанная")
    assert device._program_parameters == {key: value}
    next_title = next(program["title"] for program in device.programs if program["title"] != "Смешанная")
    device.set_selected_program(next_title)
    assert not device._program_parameters
    with pytest.raises(ServiceValidationError) as error:
        device.configure_program("Смешанная", {key: value})
    assert error.value.translation_key == "laundry_parameters_program_changed"
    cloud.async_send.assert_not_awaited()


async def test_malformed_schema_reports_a_translation_key_and_rejects_staging(machine):
    device, cloud = machine
    device.set_selected_program("Смешанная")
    device.status_data["smartDeviceControl"]["businessAttributes"] = []
    error = device.program_settings["error"]
    assert error["translation_key"] == "laundry_parameters_missing_schema"
    assert error["translation_placeholders"] == {"device": device.device_name}
    with pytest.raises(ServiceValidationError) as raised:
        device.configure_program("Смешанная", {"50": "30"})
    assert raised.value.translation_key == error["translation_key"]
    cloud.async_send.assert_not_awaited()


async def test_current_program_details_only_use_reported_configuration(machine):
    device, cloud = machine
    device._apply_program({"title": "Смешанная", "status": "IN_PROGRESS", "config": {
        "selectedValues": [{"attrName": "50", "attrValue": "60"}],
    }})
    temperature = next(field for field in device.active_program_details["fields"] if field["key"] == "50")
    assert temperature["value"] == "60 °C"
    device._apply_program({"title": "Другая программа", "status": "IN_PROGRESS"})
    assert not device._active_program_values
    cloud.async_send.assert_not_awaited()


async def test_native_selector_service_supports_entity_and_device_targets(hass, aioclient_mock, ws_factory):
    entry = await base.start(hass, aioclient_mock, ws_factory, base.LAUNDRY, options={"laundry_control": True})
    try:
        device = base.device_by_mac(entry, base.HW90["device_id"])
        device._raw[device.codes["onOffStatus"]] = "1"
        device._raw[device.codes["remoteCtrValid"]] = "1"
        device._raw[device.codes["cyclePhase"]] = "0"
        device.program_status = "FINISHED"
        uid = f"{device.device_id}_{device.device_model}_laundry_program"
        entity_id = base.eid(hass, "select", uid)
        registry = er.async_get(hass)
        registered = registry.async_get(entity_id)
        assert registered.unique_id == uid and registered.entity_category is None
        await hass.services.async_call("select", "select_option", {"entity_id": entity_id, "option": "Смешанная"}, blocking=True)
        key, value = change_value(device)
        sent = deepcopy(ws_factory.current.sent)
        await hass.services.async_call("haier_evo", "configure_program", {
            "entity_id": entity_id, "program": "Смешанная", "parameters": {key: value},
        }, blocking=True)
        await hass.async_block_till_done()
        state = hass.states.get(entity_id)
        assert state.attributes["program_catalog"]
        assert state.attributes["program_settings"]["values"][key] == value
        assert "active_program_details" in state.attributes
        await hass.services.async_call("haier_evo", "configure_program", {
            "device_id": registered.device_id, "program": "Смешанная", "parameters": {key: value},
        }, blocking=True)
        assert dr.async_get(hass).async_get(registered.device_id)
        assert ws_factory.current.sent == sent
    finally:
        await base.stop(hass, entry)


async def test_other_native_select_is_rejected_with_translated_error(hass, aioclient_mock, ws_factory):
    entry = await base.start(hass, aioclient_mock, ws_factory, base.EVO)
    try:
        registry = er.async_get(hass)
        other = next(
            item.entity_id for item in er.async_entries_for_config_entry(registry, entry.entry_id)
            if item.domain == "select"
        )
        with pytest.raises(ServiceValidationError) as error:
            await hass.services.async_call("haier_evo", "configure_program", {
                "entity_id": other, "program": "Смешанная", "parameters": {"50": "30"},
            }, blocking=True)
        assert error.value.translation_key == "laundry_parameters_target"
    finally:
        await base.stop(hass, entry)


async def test_schema_change_between_staging_and_start_rejects_without_sending(machine):
    device, cloud = machine
    device.set_selected_program("Смешанная")
    key, value = change_value(device)
    device.configure_program("Смешанная", {key: value})
    schema = next(
        schema["commandParameters"] for schema in device.status_data["smartDeviceControl"]["businessAttributes"]
        if schema["name"] == next(p["link"] for p in device.programs if p["title"] == "Смешанная")
    )
    field = next(field for field in schema["attrNameList"] if str(field["name"]) == key)
    field["writable"] = False
    with pytest.raises(ServiceValidationError) as error:
        await device.start()
    assert error.value.translation_key == "laundry_parameters_unavailable_value"
    cloud.async_send.assert_not_awaited()
    assert device._start_pending is False
    assert device._cancel_start_pending is None


@pytest.mark.parametrize("index", range(3))
async def test_catalogue_is_reused_during_ticks_and_rebuilt_for_a_new_description(hass, index):
    with patch("custom_components.haier_evo.device_laundry.build_catalogue", wraps=build_catalogue) as build:
        device, _cloud = make_machine(hass, index)
        catalogue = deepcopy(device.program_catalog)
        description = deepcopy(device.status_data)
        remaining_code = device.codes.get("remainingDryTime") or device.codes["remainingTimeMM"]
        for minutes in (5, 4, 3):
            snapshot = deepcopy(description)
            attributes = snapshot["smartDeviceControl"]["attributes"]
            next(attr for attr in attributes if str(attr["name"]) == remaining_code)["currentValue"] = str(minutes)
            device.update_from_backend(snapshot)
            assert device.program_catalog == catalogue
        assert build.call_count == 1
        groups = description["device"]["allProgram"]["groups"] if index == 2 else description["allProgram"]["blocks"]
        groups[0]["title"] = "Updated / Обновлено"
        device._get_status(description)
        assert device.program_catalog[0]["title"] == "Updated / Обновлено"
        assert device.program_catalog[0]["programs"] == catalogue[0]["programs"]
        assert build.call_count == 2
