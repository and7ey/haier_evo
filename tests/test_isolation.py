"""A bug in the code of one device stays with that device.

The hub, the platforms and the setup of the entry are shared by every
device of an account: an exception while a washer applies its state or
creates its entities must not leave the air conditioners next to it unread
or without entities.
"""
from __future__ import annotations

import logging
from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er

from custom_components.haier_evo.api import HaierAC
from custom_components.haier_evo.device import HaierDevice
from custom_components.haier_evo.laundry_events import LaundryEvents

from . import test_devices as base
from .conftest import MAC_1, MAC_2
from .test_resilience import unload
from .test_setup_edge_cases import climates, logged


async def test_resync_reads_the_others_when_one_device_fails_to_apply_its_state(hass, setup_entry, caplog):
    hub = setup_entry.runtime_data
    broken, other = hub.devices
    assert (broken.device_id, other.device_id) == (MAC_1, MAC_2)
    real = HaierDevice.update_from_backend

    def update(self, data, **kwargs):
        if self is broken:
            raise RuntimeError("a bug in the code of one device")
        real(self, data, **kwargs)

    other.synced = False
    with patch.object(HaierDevice, "update_from_backend", update):
        # the cloud answered for both: nothing is left to read again
        assert await hub._async_resync() == set()
        assert other.synced
        assert logged(caplog, logging.ERROR, "Failed to apply the state of") == 1
        other.synced = False
        assert await hub._async_resync() == set()
        assert other.synced
    # the traceback once per device, not at every pass (every 15 minutes)
    assert logged(caplog, logging.ERROR, "Failed to apply the state of") == 1
    # and again once the device has been read in between
    assert await hub._async_resync() == set()
    with patch.object(HaierDevice, "update_from_backend", update):
        await hub._async_resync()
    assert logged(caplog, logging.ERROR, "Failed to apply the state of") == 2


async def test_platform_keeps_the_others_when_one_device_fails_to_create_entities(
    hass, config_entry, mock_cloud, ws_factory, caplog
):
    mock_cloud()
    real = HaierAC.create_entities_binary_sensor

    def create(self):
        if self.device_id == MAC_2:
            raise RuntimeError("a bug in the code of one device")
        return real(self)

    with patch.object(HaierAC, "create_entities_binary_sensor", create):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.LOADED
    registry = er.async_get(hass)
    binary_sensors = [
        entity.unique_id for entity in er.async_entries_for_config_entry(registry, config_entry.entry_id)
        if entity.domain == "binary_sensor"
    ]
    # the unit next to it has its binary sensors, the broken one keeps its other platforms
    assert binary_sensors
    assert all(unique_id.startswith(MAC_1) for unique_id in binary_sensors)
    assert climates(hass) == 2
    assert logged(caplog, logging.ERROR, "Failed to create the binary_sensor entities of") == 1
    await unload(hass, config_entry)


async def test_entry_loads_when_the_events_of_one_machine_fail_to_start(hass, aioclient_mock, ws_factory, caplog):
    real = LaundryEvents.start

    def start(self, entry):
        if self.device.device_id == base.HW90["device_id"]:
            raise RuntimeError("a bug in the events of one machine")
        real(self, entry)

    with patch.object(LaundryEvents, "start", start):
        entry = await base.start(hass, aioclient_mock, ws_factory, base.LAUNDRY)
    try:
        assert logged(caplog, logging.ERROR, "Failed to start the program events of") == 1
        for device in entry.runtime_data.devices:
            prefix = f"{device.device_id}_{device.device_model}"
            assert base.eid(hass, "sensor", f"{prefix}_state")
            assert base.eid(hass, "event", f"{prefix}_laundry")
    finally:
        await base.stop(hass, entry)
