"""Setting up a cluster: devices, failover, members joining and leaving."""

from __future__ import annotations

from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.test_util.aiohttp import AiohttpClientMocker

from custom_components.dnscore.const import CONF_KNOWN_URLS, DOMAIN, LOCAL_NODE
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .common import (
    NODE01,
    NODE02,
    NODE04,
    NODES,
    UNIQUE_ID,
    URL01,
    URL02,
    URL04,
    Clock,
    get_device,
    load,
    mock_cluster,
    node_unique_id,
    refresh_all,
    view,
)


async def test_setup_creates_devices(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    entry = init_integration
    assert entry.state is ConfigEntryState.LOADED
    cluster = get_device(hass, entry, f"cluster-{UNIQUE_ID}")
    assert cluster is not None
    assert cluster.model == "Cluster"
    assert cluster.entry_type is dr.DeviceEntryType.SERVICE

    board = get_device(hass, entry, NODE01)
    assert board is not None
    assert board.name == "dns01.example.com"
    assert board.model == "ESP32-P4 board"
    assert board.model_id == "ESP32-P4"
    assert board.hw_version == "v1.0"
    assert board.sw_version == "0.94.5"
    assert board.via_device_id == cluster.id
    assert board.configuration_url == f"{URL01}/"
    assert (dr.CONNECTION_NETWORK_MAC, "02:00:5e:00:00:11") in board.connections

    app = get_device(hass, entry, NODE04)
    assert app is not None
    assert app.model == "Linux node"
    assert app.hw_version is None
    assert app.configuration_url == f"{URL04}/"


async def test_unload(hass: HomeAssistant, init_integration: MockConfigEntry) -> None:
    assert await hass.config_entries.async_unload(init_integration.entry_id)
    assert init_integration.state is ConfigEntryState.NOT_LOADED


async def test_known_urls_remembered(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: AiohttpClientMocker,
) -> None:
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry, data={**mock_config_entry.data, CONF_KNOWN_URLS: []}
    )
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.data[CONF_KNOWN_URLS] == [
        f"{URL01}/",
        f"{URL02}/",
        f"{URL04}/",
    ]


async def test_token_rejected_starts_reauth(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    mock_cluster(
        aioclient_mock,
        overrides={
            (n, "/api/cluster/status"): (401, {"error": "unknown token"}) for n in NODES
        },
    )
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]


async def test_cluster_unreachable_retries(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    mock_cluster(aioclient_mock, down=NODES)
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_failover_to_another_member(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """dns01 (the configured node) is down: the cluster is read from another member."""
    mock_cluster(aioclient_mock, down=[NODE01])
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.LOADED
    runtime = mock_config_entry.runtime_data
    assert runtime.cluster.active_node_id == NODE02
    assert not runtime.nodes.nodes[NODE01].last_update_success
    assert runtime.nodes.nodes[NODE02].last_update_success


async def test_node_read_at_its_address(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """dns02's name does not resolve: it is read at its address."""
    mock_cluster(aioclient_mock, unresolved=[NODE02])
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    node = mock_config_entry.runtime_data.nodes.nodes[NODE02]
    assert node.last_update_success
    assert node.data.stats.node_id == NODE02
    assert any(
        str(url) == f"{NODES[NODE02].at_address}/api/stats"
        for _, url, _, _ in aioclient_mock.mock_calls
    )


async def test_wrong_node_on_a_name(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A name that reaches another member is not that node's answer."""
    mock_cluster(
        aioclient_mock,
        overrides={
            (NODE02, "/api/stats"): (200, {**load("stats.json"), "node_id": NODE01})
        },
    )
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert not mock_config_entry.runtime_data.nodes.nodes[NODE02].last_update_success


async def test_member_removed_after_a_while(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    clock: Clock,
) -> None:
    mock_cluster(aioclient_mock)
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert get_device(hass, mock_config_entry, NODE02) is not None

    aioclient_mock.clear_requests()
    mock_cluster(
        aioclient_mock,
        overrides={
            (n, "/api/cluster/status"): (200, view(n, drop=[NODE02])) for n in NODES
        },
    )
    await refresh_all(hass, mock_config_entry)
    assert get_device(hass, mock_config_entry, NODE02) is not None  # a restart, maybe

    clock.now += 31 * 60
    await refresh_all(hass, mock_config_entry)
    assert get_device(hass, mock_config_entry, NODE02) is None
    assert NODE02 not in mock_config_entry.runtime_data.nodes.nodes
    entity_registry = er.async_get(hass)
    assert (
        entity_registry.async_get_entity_id(
            "sensor", DOMAIN, node_unique_id(NODE02, "queries")
        )
        is None
    )


async def test_member_joins(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    mock_cluster(
        aioclient_mock,
        overrides={
            (n, "/api/cluster/status"): (200, view(n, drop=[NODE02])) for n in NODES
        },
    )
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert get_device(hass, mock_config_entry, NODE02) is None

    aioclient_mock.clear_requests()
    mock_cluster(aioclient_mock)
    await refresh_all(hass, mock_config_entry)
    assert get_device(hass, mock_config_entry, NODE02) is not None
    entity_registry = er.async_get(hass)
    assert entity_registry.async_get_entity_id(
        "sensor", DOMAIN, node_unique_id(NODE02, "queries")
    )


async def test_lone_node(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A node with clustering off is one device, read where it was configured."""
    mock_cluster(
        aioclient_mock,
        overrides={(NODE01, "/api/cluster/status"): (200, load("cluster_lone.json"))},
    )
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    runtime = mock_config_entry.runtime_data
    assert list(runtime.nodes.nodes) == [LOCAL_NODE]
    assert runtime.nodes.nodes[LOCAL_NODE].last_update_success
    assert get_device(hass, mock_config_entry, f"{UNIQUE_ID}-{LOCAL_NODE}") is not None


async def test_remove_device_only_when_gone(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    from custom_components.dnscore import async_remove_config_entry_device  # noqa: PLC0415

    board = get_device(hass, init_integration, NODE01)
    assert board is not None
    assert not await async_remove_config_entry_device(hass, init_integration, board)
    registry = dr.async_get(hass)
    stale = registry.async_get_or_create(
        config_entry_id=init_integration.entry_id, identifiers={(DOMAIN, "gone-node")}
    )
    assert await async_remove_config_entry_device(hass, init_integration, stale)
