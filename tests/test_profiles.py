"""Device profiles: every YAML loads, our models resolve to their own file."""
from __future__ import annotations

from pathlib import Path

import pytest
from homeassistant.util.yaml import load_yaml

from custom_components.haier_evo import config as CFG

DEVICES = Path(CFG.__file__).parent / "devices"


@pytest.mark.parametrize("path", sorted(DEVICES.glob("*.yaml")), ids=lambda p: p.stem)
def test_profile_is_valid(path: Path):
    data = load_yaml(str(path))
    assert isinstance(data, dict)
    for attr in data["attributes"]:
        assert "name" in attr and "id" in attr


@pytest.mark.parametrize("raw_model", ["AS25PHP3HRA", "AS70S2SF2FA-B"])
def test_models_without_a_profile_take_the_cloud_codes(raw_model: str):
    """No copy of a profile of another series (#28, #47): default.yaml sets no
    code, everything comes from the cloud's description."""
    model = raw_model.replace("-", "").replace("/", "")[:11]
    config = CFG.HaierACConfig(model, "")
    assert config._find_config_file().endswith("default.yaml")
    assert config.get_config_attributes() == []


def test_unknown_model_uses_default():
    config = CFG.HaierACConfig("XX00UNKNOWN", "")
    assert config._find_config_file().endswith("default.yaml")


def test_item_lookup_returns_none_for_unknown():
    attr = CFG.Attribute({
        "name": "2",
        "description": "Режимы",
        "list": {"data": [{"data": "1", "name": "Охлаждение"}]},
    })
    assert attr.get_item_name("1") == "cool"
    assert attr.get_item_name("99") is None
    assert attr.get_item_code("cool") == "1"
    assert attr.get_item_code("turbo") is None


def test_temperature_items_do_not_pollute_shared_mappings():
    CFG.Item.create("fridge_mode", {"data": "3", "name": "3℃"})
    assert "3℃" not in CFG.Item.mappings
