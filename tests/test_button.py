"""Buttons: refresh the blocklist (on the leader), flush a node's cache."""

from __future__ import annotations

import pytest
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.test_util.aiohttp import AiohttpClientMocker

from custom_components.dnscore.const import DOMAIN
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er

from .common import (
    LEADER,
    NODE02,
    NODE04,
    NODES,
    URL04,
    cluster_unique_id,
    mock_cluster,
    node_unique_id,
    view,
)


def _button(hass: HomeAssistant, unique_id: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id("button", DOMAIN, unique_id)
    assert entity_id is not None, unique_id
    return entity_id


def _posts(aioclient_mock: AiohttpClientMocker, path: str) -> list[str]:
    return [
        str(url)
        for method, url, _, _ in aioclient_mock.mock_calls
        if method == "POST" and str(url).endswith(path)
    ]


async def _press(hass: HomeAssistant, entity_id: str) -> None:
    await hass.services.async_call(
        "button", "press", {ATTR_ENTITY_ID: entity_id}, blocking=True
    )


async def test_refresh_blocklist_asks_the_leader(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Set up through the app node: the request still goes to the leader."""
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry, data={**mock_config_entry.data, "host": "dns04.example.com"}
    )
    mock_cluster(aioclient_mock)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    await _press(hass, _button(hass, cluster_unique_id("refresh_blocklist")))
    assert _posts(aioclient_mock, "/api/blocklist/update") == [
        f"{NODES[LEADER].url}/api/blocklist/update"
    ]


async def test_refresh_refused_in_dns_cores_words(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """No leader known: the answering node refuses, and says why."""
    leaderless = {n: {**view(n), "leader": ""} for n in NODES}
    mock_cluster(
        aioclient_mock,
        overrides={(n, "/api/cluster/status"): (200, v) for n, v in leaderless.items()},
        post_overrides={
            (n, "/api/blocklist/update"): (409, {"message": "no leader yet"})
            for n in NODES
        },
    )
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    with pytest.raises(ServiceValidationError, match="no leader yet"):
        await _press(hass, _button(hass, cluster_unique_id("refresh_blocklist")))


async def test_flush_cache_on_one_node(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    await _press(hass, _button(hass, node_unique_id(NODE04, "flush_cache")))
    assert _posts(aioclient_mock, "/api/cache/flush") == [f"{URL04}/api/cache/flush"]


async def test_flush_cache_at_the_address(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    mock_cluster(aioclient_mock, unresolved=[NODE02])
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    await _press(hass, _button(hass, node_unique_id(NODE02, "flush_cache")))
    assert _posts(aioclient_mock, "/api/cache/flush")[-1] == (
        f"{NODES[NODE02].at_address}/api/cache/flush"
    )
