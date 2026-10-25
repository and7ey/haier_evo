"""Replay real discovery/setup functions with synthetic cloud and HA boundaries.

Run: python3 -m unittest discover -s tests -v
No credentials, network, device commands, or Home Assistant installation needed.
"""
from __future__ import annotations

import ast
import copy
import json
import logging
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components/haier_evo"
LEGACY_COMPONENT_ID = "72a6d224-cb66-4e6d-b427-2e4609252684"


class InvalidDevicesList(Exception):
    """Synthetic identity for the integration's discovery exception."""


class RetryError(Exception):
    """Synthetic identity for exhausted tenacity HTTP retries."""


class ConfigEntryNotReady(Exception):
    """Synthetic identity for HA's retry request."""


def load_function(filename: str, name: str, namespace: dict[str, object],
                  class_name: str | None = None) -> object:
    """Compile the unmodified production function; replace only its imports."""
    path = COMPONENT / filename
    module = ast.parse(path.read_text(), filename=str(path))
    nodes = module.body
    if class_name:
        nodes = next(node.body for node in nodes
                     if isinstance(node, ast.ClassDef) and node.name == class_name)
    function = next(node for node in nodes
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name == name)
    selected = ast.Module(body=[ast.ImportFrom(module="__future__", names=[
        ast.alias(name="annotations")], level=0), function], type_ignores=[])
    exec(compile(ast.fix_missing_locations(selected), str(path), "exec"), namespace)
    return namespace[name]


def cloud_page(component_id: str = LEGACY_COMPONENT_ID,
               devices: int = 2) -> dict[str, object]:
    return {"presentation": {"layout": {"scrollContainer": [{
        "trackingData": {"component": {"componentName": "deviceList",
                                         "componentId": component_id}},
        "state": json.dumps({"items": [{
            "title": f"Synthetic AC {index}",
            "action": {"link": f"haier://device?type=AC&deviceId=device-{index}&serialNum=serial-{index}"},
        } for index in range(devices)]}),
    }]}}}


class DiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.factory = Mock(side_effect=lambda **kwargs: SimpleNamespace(**kwargs))
        namespace = {"json": json, "urlparse": urlparse, "parse_qs": parse_qs,
                     "InvalidDevicesList": InvalidDevicesList,
                     "HaierDevice": SimpleNamespace(create=self.factory),
                     "_LOGGER": logging.getLogger(__name__)}
        self.pull_data = load_function("api.py", "pull_data", namespace, "Haier")

    def instance(self, response: dict[str, object]) -> SimpleNamespace:
        return SimpleNamespace(devices=[], _pull_data=None,
                               pull_data_from_api=Mock(return_value=response),
                               connect_in_thread=Mock())

    def test_existing_device_list_loads_devices_and_connects_once(self) -> None:
        instance = self.instance(cloud_page())
        self.pull_data(instance)
        self.assertEqual([device.device_mac for device in instance.devices],
                         ["device-0", "device-1"])
        instance.connect_in_thread.assert_called_once_with()

    def test_empty_or_missing_device_list_cannot_report_success(self) -> None:
        for response in ({}, {"presentation": {}}, cloud_page(devices=0)):
            with self.subTest(response=response):
                instance = self.instance(response)
                with self.assertRaises(InvalidDevicesList):
                    self.pull_data(instance)
                self.assertEqual(instance.devices, [])
                instance.connect_in_thread.assert_not_called()

    def test_device_list_without_items_cannot_report_success(self) -> None:
        response = cloud_page()
        response["presentation"]["layout"]["scrollContainer"][0]["state"] = json.dumps({
            "title": "Synthetic home", "subtitle": "Devices are loading",
        })
        instance = self.instance(response)
        with self.assertRaises(InvalidDevicesList):
            self.pull_data(instance)
        self.assertEqual(instance.devices, [])
        self.factory.assert_not_called()
        instance.connect_in_thread.assert_not_called()

    def test_device_list_uuid_is_presentation_metadata_not_device_identity(self) -> None:
        instance = self.instance(cloud_page(component_id="new-server-layout-id"))
        self.pull_data(instance)
        self.assertEqual(len(instance.devices), 2)
        instance.connect_in_thread.assert_called_once_with()

    def test_discovery_does_not_rewrite_the_cloud_response(self) -> None:
        response = cloud_page()
        original = copy.deepcopy(response)
        self.pull_data(self.instance(response))
        self.assertEqual(response, original)

    def test_failed_second_device_does_not_publish_partial_discovery(self) -> None:
        first_device = SimpleNamespace(device_mac="device-0")
        self.factory.side_effect = [first_device, TimeoutError("status timed out")]
        instance = self.instance(cloud_page())
        with self.assertRaises(TimeoutError):
            self.pull_data(instance)
        self.assertEqual(instance.devices, [])
        instance.connect_in_thread.assert_not_called()


class SetupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.client = Mock()
        self.client.load_tokens.return_value = None
        self.client.pull_data.return_value = None
        self.hass = SimpleNamespace(data={}, config_entries=SimpleNamespace(
            async_forward_entry_setups=AsyncMock()))

        async def execute(job: object) -> object:
            return job()

        self.hass.async_add_executor_job = execute
        self.entry = SimpleNamespace(entry_id="synthetic-entry", data={})
        self.api = SimpleNamespace(Haier=Mock(return_value=self.client),
                                   InvalidDevicesList=InvalidDevicesList)
        namespace = {"DOMAIN": "haier_evo", "api": self.api,
                     "PLATFORMS": ["climate", "switch"],
                     "ConfigEntryNotReady": ConfigEntryNotReady,
                     "RequestException": TimeoutError, "RetryError": RetryError,
                     "async_get_integration": AsyncMock(return_value=SimpleNamespace(version="test")),
                     "_LOGGER": logging.getLogger(__name__)}
        self.setup_entry = load_function("__init__.py", "async_setup_entry", namespace)

    async def test_exhausted_http_retries_request_setup_retry_and_cleanup(self) -> None:
        self.client.pull_data.side_effect = RetryError("Two HTTP status failures")
        with self.assertRaises(ConfigEntryNotReady):
            await self.setup_entry(self.hass, self.entry)
        self.client.stop.assert_called_once_with()
        self.assertNotIn(self.entry.entry_id, self.hass.data.get("haier_evo", {}))
        self.hass.config_entries.async_forward_entry_setups.assert_not_awaited()

    async def test_success_forwards_platforms_once(self) -> None:
        self.assertTrue(await self.setup_entry(self.hass, self.entry))
        self.hass.config_entries.async_forward_entry_setups.assert_awaited_once()
        self.client.stop.assert_not_called()

    def bind_discovery(self, client: Mock, response: dict[str, object]) -> None:
        namespace = {"json": json, "urlparse": urlparse, "parse_qs": parse_qs,
                     "InvalidDevicesList": InvalidDevicesList,
                     "HaierDevice": SimpleNamespace(create=Mock(
                         side_effect=lambda **kwargs: SimpleNamespace(**kwargs))),
                     "_LOGGER": logging.getLogger(__name__)}
        pull_data = load_function("api.py", "pull_data", namespace, "Haier")
        client.devices = []
        client.pull_data_from_api.return_value = response
        client.pull_data.side_effect = lambda: pull_data(client)

    async def test_empty_real_discovery_never_forwards_platforms(self) -> None:
        self.bind_discovery(self.client, cloud_page(devices=0))
        with self.assertRaises(ConfigEntryNotReady):
            await self.setup_entry(self.hass, self.entry)
        self.hass.config_entries.async_forward_entry_setups.assert_not_awaited()
        self.client.stop.assert_called_once_with()
        self.assertNotIn(self.entry.entry_id, self.hass.data.get("haier_evo", {}))

    async def test_invalid_json_state_requests_retry_and_cleans_client(self) -> None:
        response = cloud_page()
        response["presentation"]["layout"]["scrollContainer"][0]["state"] = "{invalid"
        self.bind_discovery(self.client, response)
        with self.assertRaises(ConfigEntryNotReady):
            await self.setup_entry(self.hass, self.entry)
        self.hass.config_entries.async_forward_entry_setups.assert_not_awaited()
        self.client.stop.assert_called_once_with()
        self.assertNotIn(self.entry.entry_id, self.hass.data.get("haier_evo", {}))

    async def test_request_failure_requests_retry_and_cleans_client(self) -> None:
        # The namespace maps requests.RequestException to this synthetic error.
        self.client.pull_data.side_effect = TimeoutError("status connection timed out")
        with self.assertRaises(ConfigEntryNotReady):
            await self.setup_entry(self.hass, self.entry)
        self.client.stop.assert_called_once_with()
        self.assertNotIn(self.entry.entry_id, self.hass.data.get("haier_evo", {}))
        self.hass.config_entries.async_forward_entry_setups.assert_not_awaited()

    async def test_retry_after_empty_discovery_publishes_once_with_new_client(self) -> None:
        first_client = self.client
        second_client = Mock()
        self.bind_discovery(first_client, cloud_page(devices=0))
        self.bind_discovery(second_client, cloud_page())
        self.api.Haier.side_effect = [first_client, second_client]
        with self.assertRaises(ConfigEntryNotReady):
            await self.setup_entry(self.hass, self.entry)
        self.assertNotIn(self.entry.entry_id, self.hass.data["haier_evo"])
        self.assertTrue(await self.setup_entry(self.hass, self.entry))
        self.assertIs(self.hass.data["haier_evo"][self.entry.entry_id], second_client)
        self.assertEqual([device.device_mac for device in second_client.devices],
                         ["device-0", "device-1"])
        self.assertEqual(first_client.devices, [])
        self.hass.config_entries.async_forward_entry_setups.assert_awaited_once()
        first_client.stop.assert_called_once_with()
        first_client.connect_in_thread.assert_not_called()
        second_client.stop.assert_not_called()
        second_client.connect_in_thread.assert_called_once_with()

    async def test_empty_discovery_requests_retry_and_cleans_pending_client(self) -> None:
        self.client.pull_data.side_effect = InvalidDevicesList("No devices discovered")
        with self.assertRaises(ConfigEntryNotReady):
            await self.setup_entry(self.hass, self.entry)
        self.client.stop.assert_called_once_with()
        self.assertNotIn(self.entry.entry_id, self.hass.data.get("haier_evo", {}))
        self.hass.config_entries.async_forward_entry_setups.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
