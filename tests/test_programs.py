"""Capability preparation is local and never sends appliance commands."""
from copy import deepcopy

import pytest

from custom_components.haier_evo.programs import active_program_details, build_catalogue, program_settings, value_valid


def select(key, default, choices):
    return {"name": key, "valueType": "3", "defaultValue": default,
            "valueRange": {"list": {"type": "LIST", "data": [{"data": value, "name": f"Cloud {value}"} for value in choices]}}}


def number(key, default, low, high, step, unit=""):
    return {"name": key, "valueType": "3", "defaultValue": default,
            "valueRange": {"range": {"type": "STEP", "data": {
                "dataType": "Double", "minValue": low, "maxValue": high, "step": step, "unit": unit}}}}


def modifier(priority, mode, values, actions):
    return {"priority": priority, "trigger": {"operator": "AND", "conditions": {
        "condition": [{"mode": mode, "values": values}]}, "alarms": []}, "actions": actions}


def forced(key, value):
    return {"name": key, "rewriteFields": "WD", "writable": False, "defaultValue": value}


@pytest.fixture
def capabilities():
    fields = [select("temp", "30", ["30", "40", "60"]),
              number("rinses", "2", "1", "5", "1"),
              select("spin", "600", ["0", "600", "1000"]),
              select("steam", "false", ["true", "false"]),
              select("fresh", "false", ["true", "false"]),
              select("stain", "0", ["0", "5"]),
              number("soil", "20", "5", "35", "5", "min"),
              number("delay", "0.0", "0.0", "24.0", "0.5", "h"),
              {"name": "schedule", "valueType": "2"},
              {"name": "internal", "valueType": "1", "defaultValue": "5"}]
    item = {"templateId": "synthetic-template", "preview": {"name": "Хлопок", "approximateTime": "1 час"},
            "detail": {"name": "Хлопок", "description": "Обычные вещи"},
            "programConfig": {"link": {"name": "synthetic-command"}}}
    enrich = {key: {"title": title} for key, title in (
        ("temp", "Температура"), ("rinses", "Полоскания"), ("spin", "Отжим"),
        ("steam", "Обработка паром"), ("fresh", "Свежесть"), ("stain", "Пятна"),
        ("soil", "Продолжительность"), ("delay", "Планируемое завершение"), ("internal", "Фиксированное"))}
    enrich["temp"]["list"] = {"valueToIcon": {"30": {"name": "30 °C"}, "50": {"name": "50 °C"}}}
    enrich["steam"]["list"] = {"valueToIcon": {"true": {"name": "Включено"}, "false": {"name": "Выключено"}}}
    data = {
        "smartDeviceControl": {
            "attributes": [{"name": "schedule", "writable": True, "list": {"data": [{"data": "true"}, {"data": "false"}]}}],
            "businessAttributes": [{"name": "synthetic-command", "commandParameters": {"name": "group", "attrNameList": fields}}],
            "modifiers": [
                modifier(13, "steam", ["true"], [forced("temp", "60"), forced("stain", "0")]),
                modifier(12, "fresh", ["true"], [forced("delay", "0.0"), forced("schedule", "false")]),
                modifier(11, "stain", ["5"], [forced("steam", "false"), forced("temp", "50"),
                                               forced("soil", "35"), forced("internal", "9")]),
                modifier(5, "phase", ["active"], [{"name": "temp", "rewriteFields": "W", "writable": False}]),
                {"priority": 2, "trigger": {"operator": "OR", "alarms": ["door", "water"]},
                 "actions": [{"name": "rinses", "rewriteFields": "W", "writable": False}]},
            ]},
        "allProgram": {"blocks": [{"title": "Ткани", "subtitle": "По типу ткани", "programs": [item]}]},
        "enrichAttributes": {"attrs": enrich},
        "controlBlock": {"delayStart": {"link": {"name": "delay"}, "statusLink": {"name": "schedule"}}},
        "clothingSoilingEnrich": {
            "link": {"name": "soil"}, "title": "Уровень загрязнения", "description": "Время и интенсивность",
            "programs": [{"templateId": "synthetic-template", "rates": [
                {"value": "10", "title": "Лёгкий", "description": "Без стойких пятен"},
                {"value": "20", "title": "Средний", "description": "Обычные загрязнения"},
                {"value": "35", "title": "Сильный", "description": "Сложные пятна"}]}]},
    }
    base = {"commandName": "group", "commands": [{"commandName": field["name"],
            "value": field.get("defaultValue", "false")} for field in fields]}
    return data, base


def settings(capabilities, overrides=None, **kwargs):
    data, base = capabilities
    return program_settings(data, "Ткани - Хлопок", base, overrides, **kwargs)


def test_catalogue_keeps_cloud_groups_descriptions_and_repeated_dryer_preview(capabilities):
    data, base = capabilities
    result = build_catalogue(data, {"Ткани - Хлопок": base})
    assert result == [{"title": "Ткани", "description": "По типу ткани", "programs": [
        {"option": "Ткани - Хлопок", "name": "Хлопок", "description": "Обычные вещи", "duration": "1 час"}]}]
    detail = {"templateId": "joined-template", "detail": {"name": "Смешанная", "description": "Сухие вещи",
              "chips": [{"approximate": "40 минут"}]}}
    preview = {"templateId": "joined-template", "preview": {"name": "Превью"}}
    dryer = {"device": {"programDetails": {"details": [detail]}, "allProgram": {"groups": [
        {"title": "Ткани", "subtitle": "Первая группа", "previews": [preview]},
        {"title": "Особая сушка", "subtitle": "Вторая группа", "previews": [deepcopy(preview)]}]}}}
    groups = build_catalogue(dryer, {"Смешанная": base})
    assert [group["title"] for group in groups] == ["Ткани", "Особая сушка"]
    assert [group["programs"][0]["option"] for group in groups] == ["Смешанная", "Смешанная"]
    assert groups[0]["programs"][0]["duration"] == "40 минут"
    assert "templateId" not in str(groups)
    assert build_catalogue(dryer, {}) == []


def test_schema_exposes_only_declared_editable_fields_and_enriched_soil(capabilities):
    result = settings(capabilities)
    fields = {field["key"]: field for field in result["fields"]}
    assert result["program"] == "Ткани - Хлопок"
    assert set(fields) == {"temp", "rinses", "spin", "steam", "fresh", "stain", "soil", "delay"}
    assert fields["temp"]["options"][0]["label"] == "30 °C"
    assert fields["temp"]["options"][1]["label"] == "Cloud 40"
    assert fields["steam"]["options"][0]["label"] == "Включено"
    assert fields["soil"]["title"] == "Уровень загрязнения"
    assert fields["soil"]["options"][0] == {"value": "10", "label": "Лёгкий", "description": "Без стойких пятен"}
    assert fields["delay"]["kind"] == "number" and fields["delay"]["unit"] == "h"
    assert fields["delay"]["step"] == 0.5


def test_preferences_and_scheduled_status_are_prepared_without_mutation(capabilities):
    before = deepcopy(capabilities)
    result = settings(capabilities, {"temp": "40", "delay": "1.5", "soil": "35"})
    assert result["values"]["schedule"] == "true"
    assert result["values"]["delay"] == "1.5"
    assert result["commands"][-1] == {"commandName": "internal", "value": "5"}
    assert capabilities == before
    assert settings(capabilities)["values"]["schedule"] == "false"


def test_linked_fixed_schedule_flag_is_derived_from_editable_finish_time(capabilities):
    data, _ = capabilities
    fields = data["smartDeviceControl"]["businessAttributes"][0]["commandParameters"]["attrNameList"]
    fields[-2].update(valueType="1", defaultValue="false")
    result = settings(capabilities, {"delay": "0.5", "fresh": "false"})
    assert result["values"]["schedule"] == "true"
    assert "schedule" not in {field["key"] for field in result["fields"]}
    assert settings(capabilities, {"delay": "0"})["values"]["schedule"] == "false"
    assert settings(capabilities, {"delay": "4.0", "fresh": "true"})["values"]["schedule"] == "false"
    with pytest.raises(ValueError):
        settings(capabilities, {"schedule": "true"})


def test_linked_schedule_flag_must_be_declared_writable(capabilities):
    data, _ = capabilities
    data["smartDeviceControl"]["attributes"][0]["writable"] = False
    field = next(field for field in settings(capabilities)["fields"] if field["key"] == "delay")
    assert field["disabled"] and field["description_key"] == "schedule_readonly"
    with pytest.raises(ValueError):
        settings(capabilities, {"delay": "0.5"})


@pytest.mark.parametrize("overrides", [
    {"unknown": "1"}, {"internal": "5"}, {"schedule": "true"},
    {"temp": "50"}, {"rinses": "6"}, {"rinses": "1.5"}, {"delay": "0.3"},
    {"delay": True}, {"delay": "NaN"}, {"delay": float("inf")}, {"soil": "25"},
    {"rinses": []}, {"steam": None}, {"steam": "yes"},
])
def test_unknown_fixed_nonfinite_and_invalid_preferences_are_rejected(capabilities, overrides):
    with pytest.raises(ValueError):
        settings(capabilities, overrides)


def test_steam_and_fresh_air_normalize_dependents_and_preferences_restore(capabilities):
    result = settings(capabilities, {"steam": "true", "temp": "30", "stain": "5", "fresh": "true", "delay": "4.0"})
    assert result["values"]["temp"] == "60"
    assert result["values"]["stain"] == "0"
    assert result["values"]["delay"] == "0.0" and result["values"]["schedule"] == "false"
    locked = {field["key"] for field in result["fields"] if field["disabled"]}
    assert {"temp", "stain", "delay"} <= locked
    restored = settings(capabilities, {"steam": "false", "temp": "30", "fresh": "false", "delay": "4.0"})
    assert restored["values"]["temp"] == "30" and restored["values"]["schedule"] == "true"


def test_stain_forced_values_override_original_ranges_and_fixed_defaults(capabilities):
    result = settings(capabilities, {"stain": "5", "soil": "10"})
    assert result["values"]["temp"] == "50" and result["values"]["soil"] == "35"
    assert result["values"]["internal"] == "9"
    temperature = next(field for field in result["fields"] if field["key"] == "temp")
    assert temperature["disabled"] and temperature["value"] == "50"
    assert temperature["options"][-1]["label"] == "50 °C"


def test_forced_soil_uses_rate_interval_description_instead_of_minutes(capabilities):
    data, _ = capabilities
    rate = data["clothingSoilingEnrich"]["programs"][0]["rates"][0]
    rate.update(fromValue="5", toValue="10")
    data["smartDeviceControl"]["modifiers"][2]["actions"][2]["defaultValue"] = "5"
    result = settings(capabilities, {"stain": "5"})
    soil = next(field for field in result["fields"] if field["key"] == "soil")
    assert soil["disabled"] and soil["value"] == "5"
    assert soil["options"][-1] == {"value": "5", "label": "Лёгкий", "description": "Без стойких пятен"}


def test_forced_soil_outside_rate_intervals_has_no_invented_soiling_level(capabilities):
    data, _ = capabilities
    data["smartDeviceControl"]["modifiers"][2]["actions"][2]["defaultValue"] = "5"
    result = settings(capabilities, {"stain": "5"})
    soil = next(field for field in result["fields"] if field["key"] == "soil")
    assert soil["disabled"] and soil["value"] == "5"
    assert soil["options"][-1]["label_key"] == "automatic"
    assert soil["options"][-1]["description_key"] == "forced_parameter"


def test_live_conditions_alarms_and_priority_control_writability(capabilities):
    data, _ = capabilities
    result = settings(capabilities, live_values={"phase": "active"}, alarms=("water",))
    assert {field["key"] for field in result["fields"] if field["disabled"]} == {"temp", "rinses"}
    data["smartDeviceControl"]["modifiers"].append(modifier(
        8, "phase", ["active"], [{"name": "temp", "rewriteFields": "W", "writable": True}]))
    result = settings(capabilities, live_values={"phase": "active"}, alarms=("other",))
    assert not any(field["disabled"] for field in result["fields"])


def test_and_conditions_need_every_declared_condition(capabilities):
    data, _ = capabilities
    rule = modifier(1, "phase", ["active"], [{"name": "rinses", "rewriteFields": "W", "writable": False}])
    rule["trigger"]["conditions"]["condition"].append({"mode": "power", "values": ["true"]})
    data["smartDeviceControl"]["modifiers"] = [rule]
    assert not next(field for field in settings(capabilities, live_values={"phase": "active"})["fields"] if field["key"] == "rinses")["disabled"]
    assert next(field for field in settings(capabilities, live_values={"phase": "active", "power": True})["fields"] if field["key"] == "rinses")["disabled"]


def test_boolean_modifier_conditions_accept_observed_zero_and_one(capabilities):
    data, _ = capabilities
    data["smartDeviceControl"]["modifiers"] = [modifier(
        1, "power", ["false"], [{"name": "temp", "rewriteFields": "W", "writable": False}])]
    assert next(field for field in settings(capabilities, live_values={"power": "0"})["fields"] if field["key"] == "temp")["disabled"]
    assert not next(field for field in settings(capabilities, live_values={"power": "1"})["fields"] if field["key"] == "temp")["disabled"]


@pytest.mark.parametrize("invalid", ["range_type", "missing_step", "modifier_operator", "modifier_action", "duplicate_fields", "missing_command", "duplicate_command", "unavailable_schedule"])
def test_malformed_relevant_capabilities_fail_closed_with_value_error(capabilities, invalid):
    data, base = capabilities
    fields = data["smartDeviceControl"]["businessAttributes"][0]["commandParameters"]["attrNameList"]
    if invalid == "range_type":
        fields[1]["valueRange"]["range"]["type"] = "UNSUPPORTED"
    elif invalid == "missing_step":
        fields[1]["valueRange"]["range"]["data"].pop("step")
    elif invalid == "modifier_operator":
        data["smartDeviceControl"]["modifiers"][0]["trigger"]["operator"] = "XOR"
    elif invalid == "modifier_action":
        data["smartDeviceControl"]["modifiers"][0]["actions"][0]["rewriteFields"] = "UNKNOWN"
    elif invalid == "duplicate_fields":
        fields.append(deepcopy(fields[0]))
    elif invalid == "missing_command":
        base["commands"].pop()
    elif invalid == "duplicate_command":
        base["commands"].append(deepcopy(base["commands"][0]))
    else:
        data["smartDeviceControl"]["attributes"][0]["list"]["data"] = [{"data": "false"}]
    with pytest.raises(ValueError):
        settings(capabilities, {"delay": "1.0"})


def test_range_validator_checks_exact_decimal_steps_and_integer_values():
    field = number("x", "0.0", "0.0", "24.0", "0.5")
    assert value_valid(field, "1.5") and value_valid(field, "0.0")
    assert not value_valid(field, "1.4") and not value_valid(field, True)
    field["valueRange"]["range"]["data"]["dataType"] = "Integer"
    assert not value_valid(field, "1.5")


def test_active_details_show_current_values_and_omit_missing_defaults(capabilities):
    data, base = capabilities
    result = active_program_details(data, "Хлопок", {"Ткани - Хлопок": base}, {
        "temp": "40", "rinses": "4", "spin": "1000", "steam": "0", "soil": "35", "delay": "1.5"})
    fields = {field["key"]: field for field in result["fields"]}
    assert fields["temp"]["value"] == "40 °C"
    assert fields["rinses"]["value"] == "4"
    assert fields["spin"]["value"] == "1000 rpm"
    assert fields["steam"]["value"] == "Выключено"
    assert fields["soil"]["value"] == "Сильный" and fields["soil"]["description"] == "Сложные пятна"
    assert fields["delay"]["value"] == "1.5 h"
    assert "fresh" not in fields and "stain" not in fields
    assert not active_program_details(data, "Хлопок", {"Ткани - Хлопок": base}, {})["fields"]


def test_active_details_accept_identical_group_duplicates_and_reject_ambiguous_config(capabilities):
    data, base = capabilities
    duplicate = deepcopy(data["allProgram"]["blocks"][0])
    duplicate["title"] = "Другая группа"
    data["allProgram"]["blocks"].append(duplicate)
    programs = {"Ткани - Хлопок": base, "Другая группа - Хлопок": base}
    assert len(active_program_details(data, "Хлопок", programs, {"temp": "30"})["fields"]) == 1
    duplicate["programs"][0]["programConfig"]["selectedValues"] = [{"attrName": "steam", "attrValue": "true"}]
    assert not active_program_details(data, "Хлопок", programs, {"temp": "30"})["fields"]
    assert not active_program_details(data, "Другая программа", programs, {"temp": "30"})["fields"]


def test_active_details_only_show_supported_settings_and_reported_current_values(capabilities):
    data, base = capabilities
    detail = active_program_details(data, "Хлопок", {"Хлопок": base},
                                    {"temp": "30", "internal": "5"}, {"temp": "60"})
    assert next(f for f in detail["fields"] if f["key"] == "temp")["value"] == "60 °C"
    assert not any(f["key"] == "internal" for f in detail["fields"])


def test_explicit_cloud_readonly_parameter_cannot_be_staged(capabilities):
    data, _ = capabilities
    next(field for field in data["smartDeviceControl"]["businessAttributes"][0]["commandParameters"]["attrNameList"]
         if field["name"] == "temp")["writable"] = False
    temperature = next(field for field in settings(capabilities)["fields"] if field["key"] == "temp")
    assert temperature["disabled"]
    with pytest.raises(ValueError):
        settings(capabilities, {"temp": "40"})
