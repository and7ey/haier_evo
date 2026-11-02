from unittest.mock import AsyncMock, Mock, patch

import pytest

from custom_components.haier_evo import async_setup


@pytest.mark.asyncio
async def test_frontend_asset_is_registered_once_during_integration_setup():
    hass = Mock()
    hass.http.async_register_static_paths = AsyncMock()
    with patch('custom_components.haier_evo.add_extra_js_url') as register:
        assert await async_setup(hass, {}) is True
    paths = hass.http.async_register_static_paths.await_args.args[0]
    assert len(paths) == 1
    assert paths[0].url_path == '/haier_evo_frontend/haier-laundry-card.js'
    with open(paths[0].path, encoding='utf-8') as asset:
        assert "customElements.define('haier-laundry-detail'" in asset.read()
    register.assert_called_once_with(hass, paths[0].url_path)
