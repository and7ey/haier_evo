"""Keep the entity IDs of the earlier laundry implementation."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .device import HaierDevice
from .device_laundry import HaierLaundry
from .logger import _LOGGER


@callback
def async_migrate_laundry_entities(
    hass: HomeAssistant, entry: ConfigEntry, devices: list[HaierDevice]
) -> None:
    """Change only unique IDs; existing target IDs need a user's choice."""
    registry = er.async_get(hass)
    conflicts = []
    for device in devices:
        if not isinstance(device, HaierLaundry):
            continue
        prefix = f"{device.device_id}_{device.device_model}_"
        for domain, old_suffix, new_suffix in (
            ("sensor", "program_status", "state"),
            ("sensor", "program_name", "program"),
            ("select", "selected_program", "laundry_program"),
        ):
            # The native platform removes control entities while control is off.
            if domain == "select" and not device.control_enabled:
                continue
            old_id = registry.async_get_entity_id(domain, DOMAIN, prefix + old_suffix)
            old = registry.async_get(old_id) if old_id is not None else None
            if old is None or old.config_entry_id != entry.entry_id:
                continue
            target_id = registry.async_get_entity_id(domain, DOMAIN, prefix + new_suffix)
            if target_id is not None:
                # A default-looking target may already be used in an automation.
                conflicts.append(f"{old.entity_id} -> {target_id}")
                continue
            registry.async_update_entity(old.entity_id, new_unique_id=prefix + new_suffix)
    if conflicts:
        _LOGGER.warning(
            "Laundry entity migration left both old and native entities unchanged because "
            "the target already exists; choose which entity ID to keep: %s",
            ", ".join(conflicts),
        )
