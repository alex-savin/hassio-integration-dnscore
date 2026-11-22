"""Shared test data: a three-node DNS Core cluster as its admin API answers.

Two boards (dns01, the leader, and dns02) and the Linux build running as the
Home Assistant app (dns04). The fixture files are a real node's answers
(the Linux binary, run locally with clustering off) with documentation
names and addresses; the per-node differences are applied here.
"""

from __future__ import annotations

from collections.abc import Iterable
import copy
import json
from pathlib import Path
from typing import Any
import zlib

from aiohttp import ClientConnectionError
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)
from yarl import URL

from custom_components.dnscore.const import CONF_KNOWN_URLS, CONF_TOKEN_NAME, DOMAIN
from homeassistant.const import (
    CONF_HOST,
    CONF_PORT,
    CONF_SSL,
    CONF_TOKEN,
    CONF_VERIFY_SSL,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceEntry

FIXTURES = Path(__file__).parent / "fixtures"

NODE01 = "dns-board-01"
NODE02 = "dns-board-02"
NODE04 = "home-assistant"
URL01 = "https://dns01.example.com"
URL02 = "https://dns02.example.com"
URL04 = "https://dns04.example.com"


class Node:
    """One member: its ID, DNS name, address, and what it is."""

    def __init__(self, node_id: str, short: str, ip: str, *, board: bool) -> None:
        self.node_id = node_id
        self.short = short
        self.host_name = f"{short}.example.com"
        self.url = f"https://{self.host_name}"
        self.ip = ip
        self.at_address = f"https://{ip}"
        self.board = board


NODES: dict[str, Node] = {
    NODE01: Node(NODE01, "dns01", "192.0.2.11", board=True),
    NODE02: Node(NODE02, "dns02", "192.0.2.12", board=True),
    NODE04: Node(NODE04, "dns04", "192.0.2.14", board=False),
}
LEADER = NODE01
TERM = 42
SERVER_NAME = "dns.example.com"
UNIQUE_ID = SERVER_NAME

TOKEN = "dnsa_" + "T" * 43
NEW_TOKEN = "dnsa_" + "N" * 43
TOKEN_NAME = "home-assistant-0123abcd"
OTA_PASSWORD = "correct horse battery"

ENTRY_DATA: dict[str, Any] = {
    CONF_HOST: "dns01.example.com",
    CONF_PORT: 443,
    CONF_SSL: True,
    CONF_VERIFY_SSL: True,
    CONF_TOKEN: TOKEN,
    CONF_TOKEN_NAME: TOKEN_NAME,
    CONF_KNOWN_URLS: [f"{URL01}/", f"{URL02}/", f"{URL04}/"],
}

# Each node's counters: queries and blocked.
COUNTERS = {NODE01: (120000, 18000), NODE02: (80000, 8000), NODE04: (40000, 2000)}


class Clock:
    """A monotonic clock the tests move by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def load(name: str) -> Any:
    """A fixture file's JSON."""
    return json.loads((FIXTURES / name).read_text())


def member(node: Node, *, is_self: bool) -> dict[str, Any]:
    """A member as `active_nodes` lists it."""
    base = copy.deepcopy(load("cluster_lone.json")["active_nodes"][0])
    queries, blocked = COUNTERS[node.node_id]
    base.update(
        {
            "id": node.node_id,
            "ip": node.ip,
            "member_id": f"{zlib.crc32(node.node_id.encode()):08x}" * 2,
            "host_name": node.host_name,
            "uptime_seconds": 86400,
            "config_generation": 7,
            "filter_version_hash": "d41edfc4",
            "serving": True,
            "stands": node.board,
            "config_state": "self" if is_self else "in-sync",
            "services": {"dns": "on", "doh": "on", "dot": "on", "doq": "on"},
            "counters": {
                "queries": queries,
                "blocked": blocked,
                "cached": queries // 2,
                "forwarded": queries // 3,
            },
        }
    )
    if not is_self:
        base["authenticated"] = True
    return base


def view(node_id: str, *, drop: Iterable[str] = ()) -> dict[str, Any]:
    """`GET /api/cluster/status` as `node_id` answers it (itself first)."""
    data = copy.deepcopy(load("cluster_lone.json"))
    gone = set(drop)
    order = [node_id, *(n for n in NODES if n != node_id)]
    data.update(
        {
            "leader": LEADER,
            "term": TERM,
            "quorum_of": 2,
            "config_generation": 7,
            "cluster_key_id": "1a2b3c4d",
        }
    )
    data["active_nodes"] = [
        member(NODES[n], is_self=n == node_id) for n in order if n not in gone
    ]
    return data


def stats(node_id: str) -> dict[str, Any]:
    """`GET /api/stats` as `node_id` answers it."""
    data = copy.deepcopy(load("stats.json"))
    queries, blocked = COUNTERS[node_id]
    data.update(
        {
            "node_id": node_id,
            "is_leader": node_id == LEADER,
            "cluster_leader": LEADER,
            "leader_ip": NODES[LEADER].ip,
            "dns_queries_total": queries,
            "dns_queries_blocked": blocked,
        }
    )
    if not NODES[node_id].board:
        # The Linux build reports the host's memory.
        for key in ("memory_free_bytes", "dma_free_bytes", "psram_free_bytes"):
            data[key] = 1763827712
    return data


def device(node_id: str) -> dict[str, Any]:
    """`GET /api/device` as `node_id` answers it."""
    node = NODES[node_id]
    data = copy.deepcopy(
        load("device_board.json" if node.board else "device_linux.json")
    )
    data["node"] = {"id": node_id, "is_leader": node_id == LEADER}
    if node.board:
        data["chip"]["mac"] = f"02:00:5e:00:00:{node.ip.rsplit('.', 1)[1]}"
    return data


def config(node_id: str, **changes: Any) -> dict[str, Any]:
    """`GET /api/config` as a token reading system and filtering gets it."""
    data = copy.deepcopy(load("config.json"))
    data["host_name"] = NODES[node_id].host_name
    data["tls"]["server_name"] = SERVER_NAME
    data["config_generation"] = 7
    data.update(changes)
    return data


def health(node_id: str) -> dict[str, Any]:
    """`GET /api/health` (0.95.0): dns02 has a warning, the others are fine."""
    if node_id == NODE02:
        return load("health.json")
    return {"status": "ok", "conditions": []}


def node_routes(node_id: str) -> dict[str, tuple[int, Any]]:
    """What one node answers to a GET, by path."""
    return {
        "/api/auth/status": (200, load("auth_status.json")),
        "/api/cluster/status": (200, view(node_id)),
        "/api/config": (200, config(node_id)),
        "/api/stats": (200, stats(node_id)),
        "/api/device": (200, device(node_id)),
        "/api/tls/status": (200, load("tls_status.json")),
        "/api/acme": (200, load("acme.json")),
        "/api/blocklist/update": (200, load("blocklist_update.json")),
        "/api/ota": (200, load("ota.json")),
        "/api/health": (200, health(node_id)),
    }


def node_post_routes(node_id: str) -> dict[str, tuple[int, Any]]:
    """What one node answers to a POST, by path."""
    leader = node_id == LEADER
    return {
        "/api/config": (200, config(node_id)),
        "/api/blocklist/update": (
            (202, {"started": True, "message": "started", "status": {}})
            if leader
            else (
                409,
                {"started": False, "message": f"only the leader ({LEADER}) downloads"},
            )
        ),
        "/api/cache/flush": (
            200,
            {"status": "ok", "dropped": 1500, "bytes_freed": 90000},
        ),
    }


def mock_cluster(
    aioclient_mock: AiohttpClientMocker,
    *,
    down: Iterable[str] = (),
    unresolved: Iterable[str] = (),
    overrides: dict[tuple[str, str], tuple[int, Any]] | None = None,
    post_overrides: dict[tuple[str, str], tuple[int, Any]] | None = None,
) -> None:
    """Register the three nodes, each at its URL and at its address.

    `down` lists node IDs that answer at neither; `unresolved` lists node
    IDs whose name does not resolve, so only the address answers.
    `overrides` replaces single GET answers, keyed by (node ID, path).
    """
    down, unresolved = set(down), set(unresolved)
    overrides = overrides or {}
    post_overrides = post_overrides or {}
    for node_id, node in NODES.items():
        for method, routes, changed in (
            ("get", node_routes(node_id), overrides),
            ("post", node_post_routes(node_id), post_overrides),
        ):
            register = getattr(aioclient_mock, method)
            for path, answer in routes.items():
                status, body = changed.get((node_id, path), answer)
                for base, answers in (
                    (node.url, node_id not in down and node_id not in unresolved),
                    (node.at_address, node_id not in down),
                ):
                    if answers:
                        register(f"{base}{path}", status=status, json=body)
                    else:
                        register(f"{base}{path}", exc=ClientConnectionError("down"))


def get_device(
    hass: HomeAssistant, entry: MockConfigEntry, identifier: str
) -> DeviceEntry | None:
    """The entry's device with this identifier."""
    return dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, identifier), entry.entry_id
    )


def prepend(
    aioclient_mock: AiohttpClientMocker, url: str, method: str = "get", **kwargs: Any
) -> None:
    """Answer before every answer registered so far (the first match wins)."""
    aioclient_mock._mocks.insert(
        0, AiohttpClientMockResponse(method, URL(url), **kwargs)
    )


async def refresh_all(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Refresh the cluster, then every node."""
    runtime = entry.runtime_data
    await runtime.cluster.async_refresh()
    await hass.async_block_till_done()
    for node in list(runtime.nodes.nodes.values()):
        await node.async_refresh()
    await hass.async_block_till_done()


def node_unique_id(node_id: str, key: str) -> str:
    """A node entity's unique ID."""
    return f"{node_id}_{key}"


def cluster_unique_id(key: str) -> str:
    """A cluster entity's unique ID."""
    return f"cluster-{UNIQUE_ID}_{key}"
