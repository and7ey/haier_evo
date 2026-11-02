"""Pure catalogue and command preparation from Evo capability descriptors."""
from decimal import Decimal, InvalidOperation
import math


def _object(value):
    return value if isinstance(value, dict) else {}


def _items(value):
    return value if isinstance(value, list) else []


def _text(value):
    return value.replace("\u2014", "-").strip() if isinstance(value, str) else ""


def _wire(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if not isinstance(value, (str, int, float)):
        raise ValueError("Некорректное значение настройки")
    return str(value)


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError("Ожидалось число")
    try:
        number = Decimal(str(value))
    except InvalidOperation as err:
        raise ValueError("Ожидалось число") from err
    if not number.is_finite():
        raise ValueError("Ожидалось конечное число")
    return number


def value_valid(field, value):
    """Validate a declared value, including exact range steps."""
    if not isinstance(field, dict) or not isinstance(value, (str, int, float, bool)):
        return False
    limits = field.get("valueRange") or {}
    if not isinstance(limits, dict):
        return False
    listing, ranged = limits.get("list") or {}, limits.get("range") or {}
    if not isinstance(listing, dict) or not isinstance(ranged, dict):
        return False
    choices, bounds = listing.get("data") or [], ranged.get("data") or {}
    if not isinstance(choices, list) or not isinstance(bounds, dict):
        return False
    try:
        if _wire(value).casefold() in ("nan", "inf", "+inf", "-inf", "infinity", "+infinity", "-infinity"):
            return False
        if isinstance(value, float):
            _number(value)
        if choices and _wire(value) not in {_wire(item["data"]) for item in choices
                                           if isinstance(item, dict) and "data" in item}:
            return False
        if bounds:
            number = _number(value)
            low = _number(bounds.get("minValue", value))
            high = _number(bounds.get("maxValue", value))
            if not low <= number <= high:
                return False
            if bounds.get("dataType") == "Integer" and number != number.to_integral_value():
                return False
            if "step" in bounds:
                step = _number(bounds["step"])
                if step <= 0 or (number - low) % step:
                    return False
    except (ValueError, InvalidOperation):
        return False
    return True


def _washer_entries(data):
    for group in _items(_object(data.get("allProgram")).get("blocks")):
        if not isinstance(group, dict):
            continue
        entries = []
        for item in _items(group.get("programs")):
            if not isinstance(item, dict):
                continue
            name = _object(item.get("preview")).get("name")
            if isinstance(group.get("title"), str) and isinstance(name, str):
                entries.append((f"{group['title']} - {name}", item))
        yield group, entries


def _dryer_details(data):
    return [item for item in _items(_object(_object(data.get("device")).get("programDetails")).get("details"))
            if isinstance(item, dict)]


def build_catalogue(data, programs):
    """Keep cloud groups and repeated previews, exposing only validated options."""
    if not isinstance(data, dict) or not isinstance(programs, dict):
        return []
    groups = list(_washer_entries(data))
    details = _dryer_details(data)
    by_template = {}
    for item in details:
        template = item.get("templateId")
        if isinstance(template, (str, int)):
            key = str(template)
            by_template[key] = item if key not in by_template else None
    dryer_groups = _items(_object(_object(data.get("device")).get("allProgram")).get("groups"))
    for group in dryer_groups:
        if not isinstance(group, dict):
            continue
        entries = []
        for preview in _items(group.get("previews")):
            item = by_template.get(str(_object(preview).get("templateId")))
            if isinstance(item, dict):
                item = {**item, "preview": _object(preview).get("preview")}
                entries.append((_object(item.get("detail")).get("name"), item))
        groups.append((group, entries))
    if details and not dryer_groups:
        groups.append(({"title": "Программы"}, [(_object(item.get("detail")).get("name"), item) for item in details]))
    result = []
    for group, entries in groups:
        options = []
        for option, item in entries:
            if not isinstance(option, str) or option not in programs:
                continue
            detail, preview = _object(item.get("detail")), _object(item.get("preview"))
            duration = _text(preview.get("approximateTime"))
            if not duration:
                duration = next((_text(chip.get("approximate")) for chip in _items(detail.get("chips"))
                                 if isinstance(chip, dict) and _text(chip.get("approximate"))), "")
            options.append({"option": option, "name": _text(detail.get("name")) or _text(preview.get("name")) or option,
                            "description": _text(detail.get("description")), "duration": duration})
        if options:
            result.append({"title": _text(group.get("title")), "description": _text(group.get("subtitle")),
                           "programs": options})
    return result


def _definition(data, option, base_program):
    matches = [item for _, entries in _washer_entries(data) for name, item in entries if name == option]
    matches.extend(item for item in _dryer_details(data) if _object(item.get("detail")).get("name") == option)
    if len(matches) != 1:
        raise ValueError("Программа не найдена или неоднозначна")
    item = matches[0]
    link = _object(_object(item.get("programConfig")).get("link")).get("name")
    definitions = [attr.get("commandParameters") for attr in _items(_object(data.get("smartDeviceControl")).get("businessAttributes"))
                   if isinstance(attr, dict) and attr.get("name") == link]
    if len(definitions) != 1 or not isinstance(definitions[0], dict):
        raise ValueError("Отсутствует схема программы")
    definition = definitions[0]
    fields = _items(definition.get("attrNameList"))
    if (not fields or any(not isinstance(field, dict) or field.get("name") is None for field in fields)
            or len({str(field["name"]) for field in fields}) != len(fields)
            or str(definition.get("name")) != str(base_program.get("commandName"))):
        raise ValueError("Некорректная схема программы")
    return item, fields


def _field_schema(field, enrich):
    key = str(field["name"])
    schema = {"key": key, "title": _text(enrich.get("title")), "description": _text(enrich.get("hint")),
              "kind": "select", "value": "", "options": [], "min": None, "max": None,
              "step": None, "unit": "", "disabled": False}
    limits = _object(field.get("valueRange"))
    listing, ranged = _object(limits.get("list")), _object(limits.get("range"))
    if listing:
        choices = listing.get("data")
        if listing.get("type") != "LIST" or not isinstance(choices, list) or not choices:
            raise ValueError("Неизвестный список значений настройки")
        labels = _object(_object(enrich.get("list")).get("valueToIcon"))
        for item in choices:
            if not isinstance(item, dict) or "data" not in item:
                raise ValueError("Некорректный список значений настройки")
            value = _wire(item["data"])
            label = _text(_object(labels.get(value)).get("name")) or _text(item.get("name"))
            schema["options"].append({"value": value, "label": label or value,
                                      "description": _text(item.get("description"))})
        if len({item["value"] for item in schema["options"]}) != len(schema["options"]) or ranged:
            raise ValueError("Неоднозначный диапазон настройки")
    elif ranged:
        bounds = _object(ranged.get("data"))
        if ranged.get("type") != "STEP" or not {"minValue", "maxValue", "step"} <= bounds.keys():
            raise ValueError("Неизвестный диапазон настройки")
        low, high, step = (_number(bounds[key]) for key in ("minValue", "maxValue", "step"))
        if low > high or step <= 0 or bounds.get("dataType") not in ("Integer", "Double"):
            raise ValueError("Некорректный диапазон настройки")
        if not all(math.isfinite(float(value)) for value in (low, high, step)):
            raise ValueError("Диапазон настройки слишком велик")
        schema.update(kind="number", min=float(low), max=float(high), step=float(step), unit=_text(bounds.get("unit")))
    else:
        raise ValueError("Отсутствует диапазон настройки")
    return schema


def _modifier_rules(data, field_names):
    rules = []
    raw = _object(data.get("smartDeviceControl")).get("modifiers", [])
    if not isinstance(raw, list):
        raise ValueError("Некорректные зависимости настроек")
    for rule in raw:
        if not isinstance(rule, dict) or not isinstance(rule.get("actions"), list):
            raise ValueError("Некорректные зависимости настроек")
        actions = [action for action in rule["actions"] if isinstance(action, dict) and str(action.get("name")) in field_names]
        if not actions:
            continue
        trigger = _object(rule.get("trigger"))
        conditions = _object(trigger.get("conditions")).get("condition", [])
        alarms = trigger.get("alarms", [])
        priority = rule.get("priority")
        if (isinstance(priority, bool) or not isinstance(priority, int) or trigger.get("operator") not in ("AND", "OR")
                or not isinstance(conditions, list) or not isinstance(alarms, list)
                or not all(isinstance(alarm, str) for alarm in alarms) or not (conditions or alarms)):
            raise ValueError("Неизвестное условие зависимости настроек")
        for condition in conditions:
            if (not isinstance(condition, dict) or not isinstance(condition.get("mode"), (str, int))
                    or not isinstance(condition.get("values"), list) or not condition["values"]):
                raise ValueError("Неизвестное условие зависимости настроек")
            for value in condition["values"]:
                _wire(value)
        for action in actions:
            if (action.get("rewriteFields") not in ("W", "WD") or not isinstance(action.get("writable"), bool)
                    or (action["rewriteFields"] == "WD" and ("defaultValue" not in action or action["writable"]))):
                raise ValueError("Неизвестное действие зависимости настроек")
            if action["rewriteFields"] == "WD":
                _wire(action["defaultValue"])
        rules.append((priority, trigger["operator"], conditions, alarms, actions))
    return sorted(rules, key=lambda rule: rule[0], reverse=True)


def _apply_modifiers(rules, source, live_values, alarms):
    previous = source
    seen = set()
    for _ in range(len(rules) + 2):
        values, writable, forced = dict(source), {}, {}
        context = {str(key): _wire(value) for key, value in live_values.items() if isinstance(value, (str, int, float, bool))}
        context.update(previous)
        for priority, operator, conditions, required_alarms, actions in rules:
            checks = []
            for condition in conditions:
                allowed = {_wire(value) for value in condition["values"]}
                value = context.get(str(condition["mode"]))
                if allowed <= {"true", "false"}:
                    value = {"0": "false", "1": "true"}.get(value, value)
                checks.append(value in allowed)
            checks.extend(alarm in alarms for alarm in required_alarms)
            if not (all(checks) if operator == "AND" else any(checks)):
                continue
            for action in actions:
                key = str(action["name"])
                if key in writable:
                    if writable[key][0] == priority and (writable[key][1] != action["writable"]
                            or (action["rewriteFields"] == "WD" and forced.get(key) != _wire(action["defaultValue"]))):
                        raise ValueError("Противоречивые зависимости настроек")
                    continue
                writable[key] = (priority, action["writable"])
                if action["rewriteFields"] == "WD":
                    values[key] = context[key] = _wire(action["defaultValue"])
                    forced[key] = values[key]
        if values == previous:
            return values, {key for key, (_, allowed) in writable.items() if not allowed}, forced
        state = tuple(sorted(values.items()))
        if state in seen:
            break
        seen.add(state)
        previous = values
    raise ValueError("Зависимости настроек не удалось согласовать")


def program_settings(data, option, base_program, overrides=None, live_values=None, alarms=None):
    """Validate preferences, then apply declared dependencies without sending commands.

    Callers keep original preferences separately: disabling a dependency restores
    those preferences on the next call. Callers reject newly submitted locked keys.
    """
    if not isinstance(data, dict) or not isinstance(option, str) or not isinstance(base_program, dict):
        raise ValueError("Некорректная программа")
    if overrides is not None and not isinstance(overrides, dict):
        raise ValueError("Некорректные настройки программы")
    if live_values is not None and not isinstance(live_values, dict):
        raise ValueError("Некорректные данные устройства")
    if alarms is not None and (not isinstance(alarms, (list, tuple)) or not all(isinstance(value, str) for value in alarms)):
        raise ValueError("Некорректные данные ошибок устройства")
    item, definitions = _definition(data, option, base_program)
    fields_by_key = {str(field["name"]): field for field in definitions}
    commands = base_program.get("commands")
    if (not isinstance(commands, list) or not all(isinstance(command, dict) and "commandName" in command and "value" in command for command in commands)
            or len({str(command["commandName"]) for command in commands}) != len(commands)
            or {str(command["commandName"]) for command in commands} != fields_by_key.keys()):
        raise ValueError("Неполная команда программы")
    source = {str(command["commandName"]): _wire(command["value"]) for command in commands}
    if not all(value_valid(fields_by_key[key], value) for key, value in source.items()):
        raise ValueError("Начальные настройки программы вне допустимых значений")
    device = _object(data.get("device"))
    enrich = _object(_object(data.get("enrichAttributes") or device.get("enrichAttributes")).get("attrs"))
    soil = _object(data.get("clothingSoilingEnrich"))
    soil_key = _object(soil.get("link")).get("name")
    schemas, soil_rates = {}, []
    for key, field in fields_by_key.items():
        metadata = _object(enrich.get(key))
        if str(field.get("valueType")) != "3" or not _text(metadata.get("title")):
            continue
        schema = _field_schema(field, metadata)
        if str(soil_key) == key:
            matching = [entry for entry in _items(soil.get("programs"))
                        if isinstance(entry, dict) and entry.get("templateId") == item.get("templateId")]
            if len(matching) != 1:
                continue
            rates = _items(matching[0].get("rates"))
            soil_rates = rates
            options = [{"value": _wire(rate["value"]), "label": _text(rate.get("title")),
                        "description": _text(rate.get("description"))}
                       for rate in rates if isinstance(rate, dict) and "value" in rate
                       and _text(rate.get("title")) and value_valid(field, rate["value"])]
            if not options or len({entry["value"] for entry in options}) != len(options):
                raise ValueError("Неизвестные уровни загрязнения")
            schema.update(title=_text(soil.get("title")) or schema["title"],
                          description=_text(soil.get("description")), kind="select", options=options,
                          min=None, max=None, step=None, unit="")
        schemas[key] = schema
    for key, value in (overrides or {}).items():
        if key not in schemas or not value_valid(fields_by_key[key], value):
            raise ValueError("Настройка недоступна или значение выходит за допустимые пределы")
        wire = _wire(value)
        if schemas[key]["kind"] == "select" and wire not in {entry["value"] for entry in schemas[key]["options"]}:
            raise ValueError("Недопустимый вариант настройки")
        source[key] = wire
    rules = _modifier_rules(data, fields_by_key)
    values, disabled, forced = _apply_modifiers(rules, source, live_values or {}, alarms or ())
    for key, value in forced.items():
        field = fields_by_key[key]
        if _object(_object(field.get("valueRange")).get("range")):
            _number(value)
        if value.casefold() in ("nan", "inf", "+inf", "-inf", "infinity", "+infinity", "-infinity"):
            raise ValueError("Некорректное принудительное значение настройки")
    delay = _object(_object(data.get("controlBlock")).get("delayStart"))
    delay_key = str(_object(delay.get("link")).get("name", ""))
    status_key = str(_object(delay.get("statusLink")).get("name", ""))
    if (delay_key in values and status_key in values
            and str(fields_by_key[status_key].get("valueType")) in ("1", "2")
            and (delay_key in schemas or str(fields_by_key[status_key].get("valueType")) == "2")):
        status = "false" if _number(values[delay_key]) == 0 else "true"
        attributes = [attr for attr in _items(_object(data.get("smartDeviceControl")).get("attributes"))
                      if isinstance(attr, dict) and str(attr.get("name")) == status_key]
        choices = _items(_object(attributes[0].get("list")).get("data")) if len(attributes) == 1 else []
        allowed = {choice["data"] for choice in choices if isinstance(choice, dict) and isinstance(choice.get("data"), str)}
        can_schedule = len(attributes) == 1 and attributes[0].get("writable") is True and {"false", "true"} <= allowed
        if delay_key in schemas and not can_schedule:
            schemas[delay_key].update(disabled=True, description="Сервер не разрешает менять время завершения программы")
        if status not in allowed or (status != source[status_key] and not can_schedule):
            raise ValueError("Запланировать программу с этими параметрами нельзя")
        if status_key in forced and forced[status_key] != status:
            raise ValueError("Противоречивое время запуска программы")
        values[status_key] = status
    result_fields = []
    for key, schema in schemas.items():
        value = values[key]
        schema.update(value=value, disabled=schema["disabled"] or key in disabled)
        if schema["kind"] == "select" and value not in {entry["value"] for entry in schema["options"]}:
            if key not in forced:
                raise ValueError("Начальное значение настройки отсутствует в списке")
            labels = _object(_object(_object(enrich.get(key)).get("list")).get("valueToIcon"))
            label = _text(_object(labels.get(value)).get("name"))
            description = ""
            if key == str(soil_key):
                rate = next((rate for rate in soil_rates if isinstance(rate, dict)
                             and _number(rate.get("fromValue", rate.get("value"))) <= _number(value)
                             <= _number(rate.get("toValue", rate.get("value")))), {})
                label, description = _text(rate.get("title")), _text(rate.get("description"))
                if not label:
                    label, description = "Автоматически", "Значение задано связанной настройкой программы"
            schema["options"].append({"value": value, "label": label or value, "description": description})
        result_fields.append(schema)
    return {"program": option, "fields": result_fields, "values": values,
            "commands": [{"commandName": str(command["commandName"]), "value": values[str(command["commandName"])]}
                         for command in commands]}


def active_program_details(data, program_name, programs, live_values, program_values=None):
    """Describe observed values only; programme defaults are never observations."""
    result = {"program": _text(program_name), "fields": []}
    if not isinstance(data, dict) or not isinstance(programs, dict) or not isinstance(live_values, dict) or not result["program"]:
        return result
    candidates = [(option, item) for _, entries in _washer_entries(data) for option, item in entries
                  if option in programs and (option == program_name
                      or _object(item.get("detail")).get("name") == program_name
                      or _object(item.get("preview")).get("name") == program_name)]
    candidates.extend((_object(item.get("detail")).get("name"), item) for item in _dryer_details(data)
                      if _object(item.get("detail")).get("name") == program_name
                      and program_name in programs)
    if not candidates or any(item.get("programConfig") != candidates[0][1].get("programConfig") for _, item in candidates):
        return result
    option, item = candidates[0]
    if not isinstance(programs.get(option), dict):
        return result
    try:
        _, definitions = _definition(data, option, programs[option])
    except ValueError:
        return result
    device = _object(data.get("device"))
    enrich = _object(_object(data.get("enrichAttributes") or device.get("enrichAttributes")).get("attrs"))
    soil = _object(data.get("clothingSoilingEnrich"))
    soil_key = str(_object(soil.get("link")).get("name"))
    rates = [entry for entry in _items(soil.get("programs"))
             if isinstance(entry, dict) and entry.get("templateId") == item.get("templateId")]
    soil_rates = _items(rates[0].get("rates")) if len(rates) == 1 else []
    reported = program_values if isinstance(program_values, dict) else {}
    observed = {str(key): value for key, value in {**live_values, **reported}.items()}
    delay = _object(_object(data.get("controlBlock")).get("delayStart"))
    delay_key = str(_object(delay.get("link")).get("name", ""))
    status_key = str(_object(delay.get("statusLink")).get("name", ""))
    if observed.get(delay_key) is None and str(observed.get(status_key)).lower() in ("0", "false"):
        observed[delay_key] = "0"
    for field in definitions:
        key = str(field["name"])
        if str(field.get("valueType")) != "3" and key not in reported:
            continue
        metadata = _object(enrich.get(key))
        title, description = _text(metadata.get("title")), _text(metadata.get("hint"))
        if key not in observed or not title or not isinstance(observed[key], (str, int, float, bool)):
            continue
        value = _wire(observed[key])
        if value.casefold() in ("nan", "inf", "+inf", "-inf", "infinity", "+infinity", "-infinity"):
            continue
        labels = _object(_object(metadata.get("list")).get("valueToIcon"))
        if {"true", "false"} <= labels.keys():
            value = {"0": "false", "1": "true"}.get(value, value)
        label = _text(_object(labels.get(value)).get("name"))
        if not label and value in ("true", "false"):
            label = "Включено" if value == "true" else "Выключено"
        if key == soil_key and soil_rates:
            matches = []
            for rate in soil_rates:
                if not isinstance(rate, dict) or not _text(rate.get("title")):
                    continue
                try:
                    if _number(rate.get("fromValue", rate.get("value"))) <= _number(value) <= _number(rate.get("toValue", rate.get("value"))):
                        matches.append(rate)
                except ValueError:
                    continue
            if len(matches) != 1:
                continue
            title = _text(soil.get("title")) or title
            label, description = _text(matches[0].get("title")), _text(matches[0].get("description"))
        if not label:
            for mode in _items(_object(metadata.get("range")).get("modes")):
                if not isinstance(mode, dict):
                    continue
                try:
                    if _number(mode.get("minValue")) <= _number(value) <= _number(mode.get("maxValue")):
                        label = _text(_object(mode.get("name")).get("selector"))
                        if label:
                            break
                except ValueError:
                    continue
        label = label or value
        unit = _text(_object(_object(_object(field.get("valueRange")).get("range")).get("data")).get("unit"))
        try:
            _number(label)
        except ValueError:
            pass
        else:
            if title.casefold() in ("температура", "температура воды", "temperature", "water temperature"):
                unit = "°C"
            elif title.casefold() in ("отжим", "скорость отжима", "spin", "spin speed"):
                unit = "об/мин"
            if unit:
                label = f"{label} { {'h': 'ч', 'min': 'мин', 's': 'с', 'rpm': 'об/мин'}.get(unit, unit)}"
        result["fields"].append({"key": key, "title": title, "value": label, "description": description})
    return result
