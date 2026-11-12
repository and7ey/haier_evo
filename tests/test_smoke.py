from homeassistant.config_entries import ConfigEntryState


async def test_setup(hass, setup_entry):
    assert setup_entry.state is ConfigEntryState.LOADED
    states = hass.states.async_entity_ids("climate")
    assert len(states) == 2, states
