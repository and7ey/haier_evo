"""The table of supported models in README.md agrees with the code (tests/check_device_list.py of 1.7.2).

Every model profile (devices/*.yaml) and every model of the cloud dumps
(fixtures) has a row, and the level of each row is what the repository can
back it with.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
LEVELS = {"устройство": "device", "данные облака": "data", "профиль": "profile", "сообщение": "report"}
KINDS = {"AC": "Кондиционер", "REF": "Холодильник", "WH": "Водонагреватель", "WM": "Стиральная машина",
         "DRY": "Сушильная машина"}
DUMPS = ("evo_dump.json", "ref_dump.json", "laundry_dump.json", "wh_dump.json")
README = (ROOT / "README.md").read_text(encoding="utf-8")


def key(model: str) -> str:
    """What the integration looks a profile up by: no maker's note in brackets, no - / or spaces."""
    model = re.sub(r"\s+\([^)]*\)$", "", model.strip())
    return re.sub(r"[-/\s]", "", model).upper()


def matches(model_key: str, name: str) -> bool:
    return model_key == name or model_key[:11] == name


def _rows() -> list[dict]:
    block = re.search(r"<!-- devices:start -->\n(.*?)<!-- devices:end -->", README, re.S)
    assert block, "README has the table of models between its markers"
    rows = []
    for line in block.group(1).splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 3 or cells[0] in ("Тип", "") or set(cells[0]) <= set("-:"):
            continue
        level = next((v for k, v in LEVELS.items() if k in cells[2]), None)
        rows.append({"kind": cells[0], "model": cells[1], "key": key(cells[1]), "level": level, "line": line.strip()})
    return rows


def _profiles() -> dict[str, str]:
    profiles = {}
    for path in sorted((ROOT / "custom_components" / "haier_evo" / "devices").glob("*.yaml")):
        if path.stem == "default":
            continue
        names = set(re.findall(r"^\s*-\s*name:\s*(\w+)", path.read_text(encoding="utf-8"), re.M))
        profiles[path.stem] = "WH" if "heating_mode" in names else "WM" if "program" in names else "AC"
    return profiles


def _data() -> dict[str, str | None]:
    """{model key: the type the app's device list gives it} of the cloud dumps."""
    data = {}
    for name in DUMPS:
        dump = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
        page_types = {}
        layout = ((dump.get("backend_data") or {}).get("presentation") or {}).get("layout", {})
        for container in layout.get("scrollContainer") or []:
            for match in re.finditer(r"type=(\w+)&deviceId=([0-9a-fA-F%3A:]+)", json.dumps(container)):
                page_types[match.group(2).replace("%3A", ":").lower()] = match.group(1)
        for dev in dump["devices"]:
            bd = dev["backend_data"]
            inner = bd.get("smartDeviceControl") if isinstance(bd.get("smartDeviceControl"), dict) else bd
            data[key((inner.get("info") or {}).get("model"))] = page_types.get(dev["device_id"].lower())
    return data


ROWS, PROFILES, DATA = _rows(), _profiles(), _data()


def row_for(name: str) -> dict | None:
    return next((r for r in ROWS if matches(r["key"], name)), None)


def backed(row: dict) -> tuple[set, set]:
    return {p for p in PROFILES if matches(row["key"], p)}, {m for m in DATA if m == row["key"]}


def test_rows_are_well_formed():
    assert len(ROWS) > 10
    assert [r["line"] for r in ROWS if r["kind"] not in KINDS.values() or r["level"] is None] == []
    keys = [r["key"] for r in ROWS]
    assert sorted({k for k in keys if keys.count(k) > 1}) == []


def test_every_profile_and_dump_has_a_row():
    assert sorted(p for p in PROFILES if row_for(p) is None) == []
    assert sorted(m for m in DATA if row_for(m) is None) == []
    # a model of the test data is at least "данные облака"
    assert sorted(m for m in DATA if (row_for(m) or {}).get("level") not in ("device", "data")) == []


@pytest.mark.parametrize("level", ["device", "data", "profile", "report"])
def test_level_is_what_the_repository_backs(level):
    rows = [r for r in ROWS if r["level"] == level]
    if level in ("device", "data"):
        assert [r["model"] for r in rows if not backed(r)[1]] == []
    elif level == "profile":
        # a profile and no test data (else the row is "данные облака")
        assert [r["model"] for r in rows if not backed(r)[0] or backed(r)[1]] == []
    else:
        assert [r["model"] for r in rows if any(backed(r))] == []


def test_type_of_a_row():
    assert sorted(f"{p}: {row_for(p)['kind']} != {KINDS[k]}" for p, k in PROFILES.items()
                  if row_for(p) and row_for(p)["kind"] != KINDS[k]) == []
    assert sorted(f"{m}: {row_for(m)['kind']} != {KINDS.get(t)}" for m, t in DATA.items()
                  if t and row_for(m) and row_for(m)["kind"] != KINDS.get(t)) == []


def test_form_for_a_new_device():
    assert (ROOT / ".github" / "ISSUE_TEMPLATE" / "new_device.yml").exists()
    assert "issues/new?template=new_device.yml" in README
