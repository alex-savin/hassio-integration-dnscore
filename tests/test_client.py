"""Tests for pydnscore, the client library (no Home Assistant needed)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
import json

from aiohttp import ClientConnectionError, ClientSession
import pytest
from pytest_homeassistant.test_util.aiohttp import AiohttpClientMocker

from custom_components.dnscore.pydnscore import (
    AcmeStatus,
    BlocklistStatus,
    ClusterStatus,
    Device,
    DnscoreAuthenticationError,
    DnscoreBadRequestError,
    DnscoreClient,
    DnscoreConflictError,
    DnscoreConnectionError,
    DnscoreForbiddenError,
    DnscoreHostNotAllowedError,
    DnscoreInvalidResponseError,
    DnscoreNotFoundError,
    DnscoreRateLimitedError,
    DnscoreResponseError,
    DnscoreUnavailableError,
    Health,
    OtaState,
    ServerConfig,
    Stats,
    TlsStatus,
    build_url,
    epoch_to_datetime,
)

from .common import NODE01, NODE04, OTA_PASSWORD, TOKEN, device, load, stats, view

BASE = "https://dns01.example.com"


@pytest.fixture
async def session(aioclient_mock: AiohttpClientMocker) -> AsyncGenerator[ClientSession]:
    """A session bound to the mocker."""
    client_session = aioclient_mock.create_session(asyncio.get_running_loop())
    yield client_session
    await client_session.close()


@pytest.fixture
def client(session: ClientSession) -> DnscoreClient:
    """A client with a token."""
    return DnscoreClient(session, f"{BASE}/", token=TOKEN)


@pytest.mark.parametrize(
    ("host", "port", "ssl", "expected"),
    [
        ("dns01.example.com", 443, True, "https://dns01.example.com/"),
        ("dns01.example.com", None, True, "https://dns01.example.com/"),
        ("dns01.example.com", 8443, True, "https://dns01.example.com:8443/"),
        ("192.0.2.11", 80, False, "http://192.0.2.11/"),
        ("2001:db8::1", 443, True, "https://[2001:db8::1]/"),
    ],
)
def test_build_url(host: str, port: int | None, ssl: bool, expected: str) -> None:
    assert build_url(host, port, ssl=ssl) == expected


async def test_token_header(
    client: DnscoreClient, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(f"{BASE}/api/stats", json=stats(NODE01))
    result = await client.get_stats()
    assert result.node_id == NODE01
    _, url, _, headers = aioclient_mock.mock_calls[0]
    assert str(url) == f"{BASE}/api/stats"
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert "X-OTA-Password" not in headers


async def test_ota_password_replaces_token(
    client: DnscoreClient, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(f"{BASE}/api/config", json=load("config.json"))
    await client.get_config(ota_password=OTA_PASSWORD)
    _, _, _, headers = aioclient_mock.mock_calls[0]
    assert headers["X-OTA-Password"] == OTA_PASSWORD
    assert "Authorization" not in headers


async def test_auth_status_sends_no_credentials(
    client: DnscoreClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """A token on the open route gets 403, so none is sent."""
    aioclient_mock.get(f"{BASE}/api/auth/status", json=load("auth_status.json"))
    status = await client.get_auth_status()
    assert status.claimed
    assert not status.setup_needed
    _, _, _, headers = aioclient_mock.mock_calls[0]
    assert "Authorization" not in headers


async def test_at_address(
    session: ClientSession, aioclient_mock: AiohttpClientMocker
) -> None:
    """At an address: the request goes to the IP, the Host header names the node."""
    client = DnscoreClient(session, f"{BASE}/", token=TOKEN, address="192.0.2.11")
    aioclient_mock.get("https://192.0.2.11/api/stats", json=stats(NODE01))
    await client.get_stats()
    _, url, _, headers = aioclient_mock.mock_calls[0]
    assert str(url) == "https://192.0.2.11/api/stats"
    assert headers["Host"] == "dns01.example.com"
    assert client.address == "192.0.2.11"


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (400, DnscoreBadRequestError),
        (401, DnscoreAuthenticationError),
        (403, DnscoreForbiddenError),
        (404, DnscoreNotFoundError),
        (409, DnscoreConflictError),
        (421, DnscoreHostNotAllowedError),
        (422, DnscoreBadRequestError),
        (500, DnscoreResponseError),
        (503, DnscoreUnavailableError),
        (507, DnscoreUnavailableError),
    ],
)
async def test_error_statuses(
    client: DnscoreClient,
    aioclient_mock: AiohttpClientMocker,
    status: int,
    error: type[DnscoreResponseError],
) -> None:
    aioclient_mock.get(f"{BASE}/api/stats", status=status, json={"error": "no"})
    with pytest.raises(error) as caught:
        await client.get_stats()
    assert caught.value.status == status
    assert caught.value.message == "no"
    assert caught.value.path == "/api/stats"


async def test_message_member_is_the_sentence(
    client: DnscoreClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """The blocklist's refusals carry `message`, not `error`."""
    aioclient_mock.post(
        f"{BASE}/api/blocklist/update",
        status=409,
        json={"started": False, "message": "only the leader downloads"},
    )
    with pytest.raises(DnscoreConflictError) as caught:
        await client.refresh_blocklist()
    assert caught.value.message == "only the leader downloads"


async def test_rate_limited(
    client: DnscoreClient, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(
        f"{BASE}/api/config",
        status=429,
        json={"error": "slow down"},
        headers={"Retry-After": "30"},
    )
    with pytest.raises(DnscoreRateLimitedError) as caught:
        await client.get_config(ota_password="wrong")
    assert caught.value.retry_after == 30


async def test_connection_error(
    client: DnscoreClient, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(f"{BASE}/api/stats", exc=ClientConnectionError("refused"))
    with pytest.raises(DnscoreConnectionError):
        await client.get_stats()


async def test_timeout(
    client: DnscoreClient, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(f"{BASE}/api/stats", exc=TimeoutError())
    with pytest.raises(DnscoreConnectionError, match="no answer in time"):
        await client.get_stats()


async def test_not_json(
    client: DnscoreClient, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(f"{BASE}/api/stats", text="<html>hello</html>")
    with pytest.raises(DnscoreInvalidResponseError):
        await client.get_stats()


async def test_create_token(
    client: DnscoreClient, aioclient_mock: AiohttpClientMocker
) -> None:
    answer = {**load("agents_create.json"), "token": "dnsa_" + "x" * 43}
    aioclient_mock.post(f"{BASE}/api/agents", json=answer)
    token = await client.create_token(
        ota_password=OTA_PASSWORD,
        name="home-assistant-0123abcd",
        scopes=("status:read", "filtering:write"),
        networks=["192.0.2.50/32"],
    )
    assert token == answer["token"]
    _, _, data, headers = aioclient_mock.mock_calls[0]
    assert json.loads(data) == {
        "action": "create",
        "name": "home-assistant-0123abcd",
        "scopes": ["status:read", "filtering:write"],
        "networks": ["192.0.2.50/32"],
    }
    assert headers["X-OTA-Password"] == OTA_PASSWORD
    assert "Authorization" not in headers


async def test_create_token_without_token(
    client: DnscoreClient, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.post(f"{BASE}/api/agents", json=load("agents_create.json"))
    with pytest.raises(DnscoreInvalidResponseError):
        await client.create_token(ota_password=OTA_PASSWORD, name="x", scopes=())


async def test_list_and_revoke_tokens(
    client: DnscoreClient, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(f"{BASE}/api/agents", json=load("agents.json"))
    aioclient_mock.post(f"{BASE}/api/agents", json={"status": "ok"})
    tokens = await client.list_tokens(ota_password=OTA_PASSWORD)
    assert [t.name for t in tokens] == ["home-assistant-0123abcd"]
    assert "status:read" in tokens[0].scopes
    await client.revoke_token(ota_password=OTA_PASSWORD, name="home-assistant-0123abcd")
    _, _, data, _ = aioclient_mock.mock_calls[1]
    assert json.loads(data) == {"action": "revoke", "name": "home-assistant-0123abcd"}


@pytest.mark.parametrize(
    ("enabled", "resume_at", "body"),
    [
        (True, 0, {"blocking_enabled": True}),
        (False, 0, {"blocking_enabled": False, "blocking_resume_at": 0}),
        (
            False,
            1795320000,
            {"blocking_enabled": False, "blocking_resume_at": 1795320000},
        ),
    ],
)
async def test_set_blocking(
    client: DnscoreClient,
    aioclient_mock: AiohttpClientMocker,
    enabled: bool,
    resume_at: int,
    body: dict[str, object],
) -> None:
    aioclient_mock.post(f"{BASE}/api/config", json=load("config.json"))
    await client.set_blocking(enabled, resume_at=resume_at)
    _, _, data, headers = aioclient_mock.mock_calls[0]
    assert json.loads(data) == body
    assert headers["Authorization"] == f"Bearer {TOKEN}"


# ---- the models --------------------------------------------------------------------------


def test_stats_model() -> None:
    result = Stats.from_dict(stats(NODE01))
    assert result.queries_total == 120000
    assert result.queries_blocked == 18000
    assert result.is_leader
    assert result.cache.hit_rate == 60.0
    assert result.latency["forwarded"].p90_us == 42000
    assert result.upstreams
    assert all(not u.failing for u in result.upstreams)
    assert result.clock == datetime(2026, 11, 22, 3, 12, 36, tzinfo=UTC)
    assert result.blocking_paused_until is None


def test_device_models() -> None:
    board = Device.from_dict(device(NODE01))
    assert board.is_board
    assert board.chip.model == "ESP32-P4"
    assert board.memory.dma_low is not None
    assert board.memory.dma_low.during == "blocklist fetch + UniFi poll"
    linux = Device.from_dict(device(NODE04))
    assert not linux.is_board
    assert linux.firmware.idf == "linux"


def test_cluster_model() -> None:
    cluster = ClusterStatus.from_dict(view(NODE04))
    assert cluster.enabled
    assert cluster.node_id == NODE04
    assert cluster.self_member is not None
    assert cluster.self_member.is_self
    assert next(m.node_id for m in cluster.members) == NODE04
    assert cluster.is_leader(NODE01)
    assert not cluster.is_leader("")
    dns01 = cluster.member(NODE01)
    assert dns01 is not None
    assert dns01.host_name == "dns01.example.com"
    assert dns01.label == "dns01.example.com"
    assert dns01.counters is not None
    assert dns01.counters.queries == 120000
    assert cluster.member("nobody") is None


def test_lone_node_model() -> None:
    """A node with clustering off lists itself with no node ID."""
    cluster = ClusterStatus.from_dict(load("cluster_lone.json"))
    assert not cluster.enabled
    assert cluster.self_member is not None
    assert cluster.self_member.member_id is None


def test_config_model() -> None:
    config = ServerConfig.from_dict(
        {**load("config.json"), "tls": {"server_name": "DNS.Example.com."}}
    )
    assert config.server_name == "dns.example.com"
    assert config.port_https == 443
    assert config.blocking_enabled is True


def test_shared_models() -> None:
    tls = TlsStatus.from_dict(load("tls_status.json"))
    assert tls.not_after == datetime(2027, 1, 30, tzinfo=UTC)
    assert "dns04.example.com" in tls.hostnames
    acme = AcmeStatus.from_dict(load("acme.json"))
    assert acme.state == "ok"
    assert not acme.failing
    assert acme.last_success == epoch_to_datetime(1793836800)
    blocklist = BlocklistStatus.from_dict(load("blocklist_update.json"))
    assert blocklist.loaded
    assert blocklist.domains == 412345
    assert blocklist.built == epoch_to_datetime(1795219200)
    assert blocklist.last_ok is True


def test_ota_and_health_models() -> None:
    ota = OtaState.from_dict(load("ota.json"))
    assert ota.confirmed
    assert not ota.update.active
    health = Health.from_dict(load("health.json"))
    assert health.problem
    assert health.conditions[0].id == "blocklist_empty"


@pytest.mark.parametrize("value", [None, 0, 12345, -1])
def test_unset_clock_is_none(value: int | None) -> None:
    assert epoch_to_datetime(value) is None
