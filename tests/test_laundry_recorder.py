"""UI metadata stays live without bloating the native selector's history."""
import json

import pytest
from homeassistant.components.recorder.db_schema import StateAttributes, States, StatesMeta
from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done
from sqlalchemy import select

from . import test_devices as base

UI_ATTRIBUTES = {"program_catalog", "program_settings", "active_program_details"}


@pytest.fixture
def mock_recorder_before_hass(recorder_db_url):
    """Prepare the recorder database before the shared custom-integration fixture."""


async def test_native_selector_records_state_and_normal_attributes_without_ui_metadata(
    recorder_mock, hass, aioclient_mock, ws_factory,
):
    entry = await base.start(hass, aioclient_mock, ws_factory, base.LAUNDRY, options={"laundry_control": True})
    try:
        device = base.device_by_mac(entry, base.HW90["device_id"])
        entity_id = base.eid(hass, "select", f"{device.device_id}_{device.device_model}_laundry_program")
        for option in ("Смешанная", "Хлопок+"):
            await hass.services.async_call(
                "select", "select_option", {"entity_id": entity_id, "option": option}, blocking=True,
            )
        live = hass.states.get(entity_id)
        assert UI_ATTRIBUTES <= live.attributes.keys()
        assert live.attributes["options"]
        await async_wait_recording_done(hass)

        def read_history():
            with recorder_mock.get_session() as session:
                return list(session.execute(
                    select(States.state, StateAttributes.shared_attrs)
                    .join(StatesMeta, States.metadata_id == StatesMeta.metadata_id)
                    .outerjoin(StateAttributes, States.attributes_id == StateAttributes.attributes_id)
                    .where(StatesMeta.entity_id == entity_id)
                    .order_by(States.state_id)
                ))

        rows = await recorder_mock.async_add_executor_job(read_history)
        assert {"Смешанная", "Хлопок+"} <= {state for state, _attributes in rows}
        for _state, raw_attributes in rows:
            attributes = json.loads(raw_attributes or "{}")
            assert not UI_ATTRIBUTES.intersection(attributes)
            assert "options" not in attributes  # Native SelectEntity excludes this too.
            assert attributes["friendly_name"] == live.attributes["friendly_name"]
    finally:
        await base.stop(hass, entry)
