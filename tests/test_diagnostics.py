"""Diagnostics carry the cluster's answers with every name, address and token redacted."""

from __future__ import annotations

import json

from pytest_homeassistant.common import MockConfigEntry

from custom_components.dnscore.diagnostics import async_get_config_entry_diagnostics
from homeassistant.core import HomeAssistant

from .common import NODES, SERVER_NAME, TOKEN, TOKEN_NAME


async def test_diagnostics_redacted(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    result = await async_get_config_entry_diagnostics(hass, init_integration)
    text = json.dumps(result)
    assert result["cluster"]["available"] is True
    assert set(result["nodes"]) == {"node_0", "node_1", "node_2"}
    assert result["nodes"]["node_0"]["stats"]["dns_queries_total"] > 0
    for secret in (TOKEN, TOKEN_NAME, SERVER_NAME, "example.com", "02:00:5e"):
        assert secret not in text, secret
    for node in NODES.values():
        assert node.ip not in text
        assert node.host_name not in text
        assert node.node_id not in text
