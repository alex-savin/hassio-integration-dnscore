"""Binary sensors: the cluster's problems, and each node's state."""

from __future__ import annotations

import copy

from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.test_util.aiohttp import AiohttpClientMocker

from custom_components.dnscore.const import DOMAIN
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .common import (
    NODE01,
    NODE02,
    NODE04,
    NODES,
    cluster_unique_id,
    load,
    mock_cluster,
    node_unique_id,
    stats,
    view,
)


def _state(hass: HomeAssistant, unique_id: str):
    entity_id = er.async_get(hass).async_get_entity_id(
        "binary_sensor", DOMAIN, unique_id
    )
    assert entity_id is not None, unique_id
    state = hass.states.get(entity_id)
    assert state is not None, entity_id
    return state


async def _setup(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    **kw,
) -> None:
    mock_cluster(aioclient_mock, **kw)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_member_state(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    assert _state(hass, node_unique_id(NODE01, "online")).state == STATE_ON
    assert _state(hass, node_unique_id(NODE04, "online")).state == STATE_ON
    assert _state(hass, node_unique_id(NODE01, "leader")).state == STATE_ON
    assert _state(hass, node_unique_id(NODE02, "leader")).state == STATE_OFF
    assert _state(hass, node_unique_id(NODE02, "serving")).state == STATE_ON


async def test_no_problems(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    for key in ("certificate", "blocklist", "shared_settings"):
        assert _state(hass, cluster_unique_id(key)).state == STATE_OFF, key
    upstreams = _state(hass, node_unique_id(NODE01, "upstreams"))
    assert upstreams.state == STATE_OFF
    assert upstreams.attributes["failing"] == 0
    assert _state(hass, node_unique_id(NODE01, "blocking")).state == STATE_ON


async def test_diverged_settings(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    diverged = view(NODE01)
    diverged["active_nodes"][1]["config_state"] = "diverged"
    await _setup(
        hass,
        mock_config_entry,
        aioclient_mock,
        overrides={(NODE01, "/api/cluster/status"): (200, diverged)},
    )
    assert _state(hass, cluster_unique_id("shared_settings")).state == STATE_ON


async def test_failing_renewal(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    acme = {**load("acme.json"), "state": "failed", "error": "DNS-01 timed out"}
    await _setup(
        hass,
        mock_config_entry,
        aioclient_mock,
        overrides={(n, "/api/acme"): (200, acme) for n in NODES},
    )
    assert _state(hass, cluster_unique_id("certificate")).state == STATE_ON


async def test_failed_blocklist_download(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    blocklist = {**load("blocklist_update.json"), "last_ok": False}
    await _setup(
        hass,
        mock_config_entry,
        aioclient_mock,
        overrides={(n, "/api/blocklist/update"): (200, blocklist) for n in NODES},
    )
    assert _state(hass, cluster_unique_id("blocklist")).state == STATE_ON


async def test_failing_upstream(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    failing = copy.deepcopy(stats(NODE02))
    failing["upstream_health"][0].update({"consec_failures": 4, "down_s": 30})
    await _setup(
        hass,
        mock_config_entry,
        aioclient_mock,
        overrides={(NODE02, "/api/stats"): (200, failing)},
    )
    upstreams = _state(hass, node_unique_id(NODE02, "upstreams"))
    assert upstreams.state == STATE_ON
    assert upstreams.attributes["failing"] == 1


async def test_member_not_listed_is_offline(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    await _setup(hass, mock_config_entry, aioclient_mock)
    aioclient_mock.clear_requests()
    mock_cluster(
        aioclient_mock,
        overrides={
            (n, "/api/cluster/status"): (200, view(n, drop=[NODE02])) for n in NODES
        },
    )
    await mock_config_entry.runtime_data.cluster.async_refresh()
    await hass.async_block_till_done()
    assert _state(hass, node_unique_id(NODE02, "online")).state == STATE_OFF


async def test_health(hass: HomeAssistant, init_integration: MockConfigEntry) -> None:
    """DNS Core 0.95.0: each node's own verdict, its conditions as attributes."""
    ok = _state(hass, node_unique_id(NODE01, "health"))
    assert ok.state == STATE_OFF
    assert ok.attributes["status"] == "ok"
    warn = _state(hass, node_unique_id(NODE02, "health"))
    assert warn.state == STATE_ON
    assert warn.attributes["conditions"][0]["id"] == "blocklist_empty"


async def test_health_refused_before_0_95(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A node that refuses the token (before 0.95.0): unavailable, asked once per version."""
    refused = (
        403,
        {"error": "GET /api/health is for the admin only; no token reaches it"},
    )
    await _setup(
        hass,
        mock_config_entry,
        aioclient_mock,
        overrides={(n, "/api/health"): refused for n in NODES},
    )
    assert _state(hass, node_unique_id(NODE01, "health")).state == "unavailable"
    node = mock_config_entry.runtime_data.nodes.nodes[NODE01]

    def asked() -> int:
        return sum(
            1
            for _, url, _, _ in aioclient_mock.mock_calls
            if str(url).endswith("/api/health") and "dns01" in str(url)
        )

    before = asked()
    await node.async_refresh()
    assert asked() == before  # the same version: not asked again
    updated = {**stats(NODE01), "firmware_version": "0.95.0"}
    aioclient_mock.clear_requests()
    mock_cluster(aioclient_mock, overrides={(NODE01, "/api/stats"): (200, updated)})
    await node.async_refresh()
    await hass.async_block_till_done()
    assert asked() == 1  # a new version: asked again, and answered
    assert _state(hass, node_unique_id(NODE01, "health")).state == STATE_OFF
