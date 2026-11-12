"""Old laundry IDs survive native setup; registry conflicts need a user's choice."""
from unittest.mock import Mock

import pytest
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.haier_evo.const import DOMAIN
from custom_components.haier_evo.device import HaierDevice
from custom_components.haier_evo.device_laundry import HaierLaundry
from custom_components.haier_evo.migration import async_migrate_laundry_entities

from .test_devices import HW90, LAUNDRY, mock_account, stop

MAPPINGS = (
    ("sensor", "program_status", "state"),
    ("sensor", "program_name", "program"),
    ("select", "selected_program", "laundry_program"),
)


@pytest.fixture
def device():
    return Mock(spec=HaierLaundry, device_id=HW90["device_id"], device_model="HW90B14367U", control_enabled=True)


def register(hass, entry, device, domain, suffix):
    registered_device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, device.device_id)}
    )
    return er.async_get(hass).async_get_or_create(
        domain, DOMAIN, f"{device.device_id}_{device.device_model}_{suffix}",
        config_entry=entry, device_id=registered_device.id,
        suggested_object_id=f"my_machine_{suffix}",
    )


@pytest.mark.parametrize("disabled", [False, True])
async def test_native_setup_keeps_old_ids_and_settings(
    hass, aioclient_mock, ws_factory, config_entry, device, disabled
):
    mock_account(aioclient_mock, LAUNDRY)
    hass.config_entries.async_update_entry(config_entry, options={"laundry_control": True})
    registry = er.async_get(hass)
    area = ar.async_get(hass).async_create("Laundry")
    old = {}
    for domain, old_suffix, new_suffix in MAPPINGS:
        entity = register(hass, config_entry, device, domain, old_suffix)
        entity = registry.async_update_entity(
            entity.entity_id, name=f"My {old_suffix}", icon="mdi:star", area_id=area.id,
            disabled_by=er.RegistryEntryDisabler.USER if disabled else None,
            hidden_by=er.RegistryEntryHider.USER,
        )
        entity = registry.async_update_entity_options(
            entity.entity_id, "conversation", {"should_expose": False}
        )
        old[domain, new_suffix] = entity

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    await ws_factory.wait_for(1)
    assert ws_factory.current.sent == []
    for domain, old_suffix, new_suffix in MAPPINGS:
        before = old[domain, new_suffix]
        prefix = f"{device.device_id}_{device.device_model}_"
        assert registry.async_get_entity_id(domain, DOMAIN, prefix + old_suffix) is None
        assert registry.async_get_entity_id(domain, DOMAIN, prefix + new_suffix) == before.entity_id
        after = registry.async_get(before.entity_id)
        for attr in ("id", "name", "icon", "area_id", "disabled_by", "hidden_by", "options", "device_id"):
            assert getattr(after, attr) == getattr(before, attr)
        state = hass.states.get(before.entity_id)
        if disabled:
            assert state is None
        else:
            assert state is not None and state.state != "unavailable"

    snapshot = dict(registry.entities)
    async_migrate_laundry_entities(hass, config_entry, config_entry.runtime_data.devices)
    assert dict(registry.entities) == snapshot
    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()
    for (domain, suffix), before in old.items():
        assert registry.async_get_entity_id(
            domain, DOMAIN, f"{device.device_id}_{device.device_model}_{suffix}"
        ) == before.entity_id
        assert registry.async_get(before.entity_id).id == before.id
    await stop(hass, config_entry)


async def test_old_select_survives_control_off_until_it_is_enabled(
    hass, aioclient_mock, ws_factory, config_entry, device
):
    mock_account(aioclient_mock, LAUNDRY)
    registry = er.async_get(hass)
    old = register(hass, config_entry, device, "select", "selected_program")
    old = registry.async_update_entity(old.entity_id, name="My program", icon="mdi:star")
    prefix = f"{device.device_id}_{device.device_model}_"
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    await ws_factory.wait_for(1)
    assert registry.async_get(old.entity_id) == old
    assert registry.async_get_entity_id("select", DOMAIN, prefix + "laundry_program") is None

    await stop(hass, config_entry)
    hass.config_entries.async_update_entry(config_entry, options={"laundry_control": True})
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    await ws_factory.wait_for(2)
    assert registry.async_get_entity_id("select", DOMAIN, prefix + "laundry_program") == old.entity_id
    migrated = registry.async_get(old.entity_id)
    assert (migrated.id, migrated.name, migrated.icon) == (old.id, old.name, old.icon)
    assert hass.states.get(old.entity_id).state != "unavailable"
    assert ws_factory.current.sent == []
    await stop(hass, config_entry)


@pytest.mark.parametrize("target_kind", ["default", "customized", "other_entry"])
async def test_conflicts_leave_both_entries_unchanged(
    hass, config_entry, device, target_kind, caplog
):
    target_entry = config_entry
    if target_kind == "other_entry":
        target_entry = MockConfigEntry(domain=DOMAIN, data={"email": "other@example.com", "region": "ru"})
        target_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    pairs = []
    for domain, old_suffix, new_suffix in MAPPINGS:
        old = register(hass, config_entry, device, domain, old_suffix)
        target = register(hass, target_entry, device, domain, new_suffix)
        if target_kind == "customized":
            target = registry.async_update_entity(
                target.entity_id, name="My native sensor", icon="mdi:heart",
                disabled_by=er.RegistryEntryDisabler.USER,
            )
        pairs.append((old, target))

    snapshot = dict(registry.entities)
    for _ in range(2):
        caplog.clear()
        async_migrate_laundry_entities(hass, config_entry, [device])
        assert dict(registry.entities) == snapshot
        warnings = [r for r in caplog.records if "Laundry entity migration" in r.message]
        assert len(warnings) == 1
        for old, target in pairs:
            assert old.entity_id in warnings[0].message
            assert target.entity_id in warnings[0].message


async def test_only_own_laundry_records_are_migrated(hass, config_entry, device):
    other = MockConfigEntry(domain=DOMAIN, data={"email": "other@example.com", "region": "ru"})
    other.add_to_hass(hass)
    registry = er.async_get(hass)
    for domain, suffix, _ in MAPPINGS:
        register(hass, other, device, domain, suffix)
    register(hass, config_entry, device, "sensor", "fault")
    register(hass, config_entry, device, "sensor", "phase")
    register(hass, config_entry, device, "sensor", "remaining_time")
    register(hass, config_entry, device, "binary_sensor", "remote_control")
    snapshot = dict(registry.entities)
    async_migrate_laundry_entities(hass, config_entry, [device])
    assert dict(registry.entities) == snapshot

    non_laundry = Mock(spec=HaierDevice, device_id=device.device_id, device_model=device.device_model)
    async_migrate_laundry_entities(hass, other, [non_laundry])
    assert dict(registry.entities) == snapshot
