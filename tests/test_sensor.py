"""Sensors: the cluster's state and totals, and each node's counters."""

from __future__ import annotations

from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.test_util.aiohttp import AiohttpClientMocker

from custom_components.dnscore.const import DOMAIN
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .common import (
    NODE01,
    NODE02,
    NODE04,
    NODES,
    Clock,
    cluster_unique_id,
    config,
    load,
    mock_cluster,
    node_unique_id,
    refresh_all,
    stats,
)


def _state(hass: HomeAssistant, unique_id: str, platform: str = "sensor") -> str:
    entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, unique_id)
    assert entity_id is not None, unique_id
    state = hass.states.get(entity_id)
    assert state is not None, entity_id
    return state.state


async def test_cluster_sensors(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    assert _state(hass, cluster_unique_id("members")) == "3"
    assert _state(hass, cluster_unique_id("leader")) == "dns01.example.com"
    assert _state(hass, cluster_unique_id("blocklist_domains")) == "412345"
    assert (
        _state(hass, cluster_unique_id("blocklist_built"))
        == "2026-11-21T00:00:00+00:00"
    )
    assert (
        _state(hass, cluster_unique_id("certificate_expiry"))
        == "2027-01-30T00:00:00+00:00"
    )
    assert _state(hass, cluster_unique_id("certificate_renewal")) == "ok"
    assert _state(hass, cluster_unique_id("blocking_resumes")) == STATE_UNKNOWN


async def test_term_disabled_by_default(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, cluster_unique_id("term")
    )
    assert entity_id is not None
    entry = er.async_get(hass).async_get(entity_id)
    assert entry is not None
    assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION


async def test_blocked_ratio_over_the_cluster(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    # (18000 + 8000 + 2000) blocked of (120000 + 80000 + 40000) queries.
    assert _state(hass, cluster_unique_id("blocked_ratio")) == "11.67"


async def test_rates_need_two_readings(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    clock: Clock,
) -> None:
    mock_cluster(aioclient_mock)
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert _state(hass, node_unique_id(NODE01, "queries_rate")) == STATE_UNKNOWN
    assert _state(hass, cluster_unique_id("queries_rate")) == STATE_UNKNOWN

    clock.now += 30
    later = stats(NODE01)
    later["dns_queries_total"] += 3000
    later["dns_queries_blocked"] += 300
    aioclient_mock.clear_requests()
    mock_cluster(aioclient_mock, overrides={(NODE01, "/api/stats"): (200, later)})
    await refresh_all(hass, mock_config_entry)
    assert float(_state(hass, node_unique_id(NODE01, "queries_rate"))) == 100.0
    # dns02 and dns04 did not move: 0 queries/s each.
    assert float(_state(hass, cluster_unique_id("queries_rate"))) == 100.0
    assert float(_state(hass, cluster_unique_id("blocked_rate"))) == 10.0


async def test_rates_restart_after_a_node_restart(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    clock: Clock,
) -> None:
    mock_cluster(aioclient_mock)
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    clock.now += 30
    restarted = {**stats(NODE01), "uptime_seconds": 10, "dns_queries_total": 50}
    aioclient_mock.clear_requests()
    mock_cluster(aioclient_mock, overrides={(NODE01, "/api/stats"): (200, restarted)})
    await refresh_all(hass, mock_config_entry)
    assert _state(hass, node_unique_id(NODE01, "queries_rate")) == STATE_UNKNOWN


async def test_node_sensors(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    assert _state(hass, node_unique_id(NODE01, "queries")) == "120000"
    assert _state(hass, node_unique_id(NODE01, "blocked")) == "18000"
    assert _state(hass, node_unique_id(NODE01, "blocked_ratio")) == "15.0"
    assert _state(hass, node_unique_id(NODE01, "processing_time")) == "3.4"
    assert _state(hass, node_unique_id(NODE01, "forwarded_p90")) == "42.0"
    assert _state(hass, node_unique_id(NODE01, "cache_hit_rate")) == "60.0"
    assert _state(hass, node_unique_id(NODE01, "version")) == "0.94.5"
    assert _state(hass, node_unique_id(NODE02, "config_state")) == "in-sync"
    assert _state(hass, node_unique_id(NODE04, "config_state")) == "in-sync"


async def test_memory_on_boards_only(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    registry = er.async_get(hass)
    for key in ("internal_free", "dma_free", "dma_largest", "psram_free", "dma_low"):
        assert registry.async_get_entity_id(
            "sensor", DOMAIN, node_unique_id(NODE01, key)
        )
        assert (
            registry.async_get_entity_id("sensor", DOMAIN, node_unique_id(NODE04, key))
            is None
        )


async def test_paused_blocking_resumes_at(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    paused = {"blocking_enabled": False, "blocking_resume_at": 1795320000}
    mock_cluster(
        aioclient_mock,
        overrides={(n, "/api/config"): (200, config(n, **paused)) for n in NODES},
    )
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert (
        _state(hass, cluster_unique_id("blocking_resumes"))
        == "2026-11-22T04:00:00+00:00"
    )


async def test_no_renewal_without_acme(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    acme = {**load("acme.json"), "enabled": False}
    mock_cluster(
        aioclient_mock, overrides={(n, "/api/acme"): (200, acme) for n in NODES}
    )
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert _state(hass, cluster_unique_id("certificate_renewal")) == STATE_UNAVAILABLE
    assert _state(hass, cluster_unique_id("certificate_expiry")) != STATE_UNAVAILABLE
