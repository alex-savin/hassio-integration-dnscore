"""The config flow: one node's address, the OTA password once, a scoped token."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

from aiohttp import ClientConnectionError
import pytest
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.test_util.aiohttp import AiohttpClientMocker

from custom_components.dnscore.config_flow import parse_networks
from custom_components.dnscore.const import (
    CONF_KNOWN_URLS,
    CONF_OTA_PASSWORD,
    CONF_RESTRICT_NETWORK,
    CONF_TOKEN_NAME,
    DOMAIN,
    SECTION_CONNECTION,
    TOKEN_SCOPES,
)
from homeassistant.config_entries import SOURCE_HASSIO, SOURCE_USER
from homeassistant.const import (
    CONF_HOST,
    CONF_PORT,
    CONF_SSL,
    CONF_TOKEN,
    CONF_VERIFY_SSL,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.hassio import HassioServiceInfo

from .common import (
    NEW_TOKEN,
    NODE01,
    NODE04,
    OTA_PASSWORD,
    TOKEN_NAME,
    UNIQUE_ID,
    URL01,
    URL02,
    URL04,
    config,
    load,
    view,
)

pytestmark = pytest.mark.usefixtures(
    "mock_setup_entry", "mock_instance_id", "no_token_delay"
)

USER_INPUT = {
    CONF_HOST: "dns01.example.com",
    CONF_PORT: 443,
    SECTION_CONNECTION: {CONF_SSL: True, CONF_VERIFY_SSL: True},
}


def mock_node(
    aioclient_mock: AiohttpClientMocker,
    *,
    answers: dict[tuple[str, str], dict[str, Any]] | None = None,
    agents: list[dict[str, Any]] | None = None,
) -> None:
    """dns01 as the flow meets it: open status, then the OTA password, then the token."""
    answers = answers or {}

    def reg(method: str, path: str, **default: Any) -> None:
        kwargs = answers.get((method, path), default)
        getattr(aioclient_mock, method)(f"{URL01}{path}", **kwargs)

    reg("get", "/api/auth/status", json=load("auth_status.json"))
    reg("get", "/api/config", json=config(NODE01))
    reg("get", "/api/cluster/status", json=view(NODE01))
    reg("get", "/api/agents", json={"agents": agents or []})
    reg("post", "/api/agents", json={**load("agents_create.json"), "token": NEW_TOKEN})


async def _start(hass: HomeAssistant) -> dict[str, Any]:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)


async def test_user_flow(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    mock_node(aioclient_mock)
    result = await _start(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "credentials"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_OTA_PASSWORD: OTA_PASSWORD, CONF_RESTRICT_NETWORK: ""}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == UNIQUE_ID
    assert result["result"].unique_id == UNIQUE_ID
    assert result["data"] == {
        CONF_HOST: "dns01.example.com",
        CONF_PORT: 443,
        CONF_SSL: True,
        CONF_VERIFY_SSL: True,
        CONF_TOKEN: NEW_TOKEN,
        CONF_TOKEN_NAME: TOKEN_NAME,
        CONF_KNOWN_URLS: [f"{URL01}/", f"{URL02}/", f"{URL04}/"],
    }
    [create] = [
        json.loads(data)
        for method, url, data, _ in aioclient_mock.mock_calls
        if method == "POST" and str(url).endswith("/api/agents")
    ]
    assert create == {
        "action": "create",
        "name": TOKEN_NAME,
        "scopes": list(TOKEN_SCOPES),
        "networks": [],
    }
    # The OTA password never went into the entry.
    assert OTA_PASSWORD not in json.dumps(result["data"])


async def test_token_limited_to_an_address(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    mock_node(aioclient_mock)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_OTA_PASSWORD: OTA_PASSWORD, CONF_RESTRICT_NETWORK: "192.0.2.50"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    [create] = [
        json.loads(data)
        for method, _, data, _ in aioclient_mock.mock_calls
        if method == "POST"
    ]
    assert create["networks"] == ["192.0.2.50/32"]


async def test_old_token_revoked_first(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    mock_node(aioclient_mock, agents=[{"name": TOKEN_NAME, "scopes": ["status:read"]}])
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_OTA_PASSWORD: OTA_PASSWORD}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    actions = [
        json.loads(data)["action"]
        for method, _, data, _ in aioclient_mock.mock_calls
        if method == "POST"
    ]
    assert actions == ["revoke", "create"]


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        ({"exc": ClientConnectionError("refused")}, "cannot_connect"),
        ({"status": 404, "text": "<html>no</html>"}, "not_dnscore"),
        ({"text": "<html>a web page</html>"}, "not_dnscore"),
        ({"status": 421, "json": {"error": "unknown host"}}, "host_not_allowed"),
        ({"json": {**load("auth_status.json"), "claimed": False}}, "setup_needed"),
    ],
)
async def test_probe_errors(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    answer: dict[str, Any],
    error: str,
) -> None:
    mock_node(aioclient_mock, answers={("get", "/api/auth/status"): answer})
    result = await _start(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        ({"status": 401, "json": {"error": "wrong password"}}, "invalid_auth"),
        ({"status": 429, "json": {"error": "slow down"}}, "too_many_attempts"),
        ({"exc": ClientConnectionError("gone")}, "cannot_connect"),
    ],
)
async def test_password_errors(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    answer: dict[str, Any],
    error: str,
) -> None:
    mock_node(aioclient_mock, answers={("get", "/api/config"): answer})
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_OTA_PASSWORD: "wrong"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}


async def test_token_refused(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    mock_node(
        aioclient_mock,
        answers={
            ("post", "/api/agents"): {
                "status": 409,
                "json": {"error": "all 16 slots used"},
            }
        },
    )
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_OTA_PASSWORD: OTA_PASSWORD}
    )
    assert result["errors"] == {"base": "token_refused"}
    assert result["description_placeholders"]["error"] == "all 16 slots used"


async def test_token_not_accepted(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    mock_node(aioclient_mock)
    result = await _start(hass)
    with patch(
        "custom_components.dnscore.config_flow.DnscoreConfigFlow._async_token_works",
        return_value=False,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_OTA_PASSWORD: OTA_PASSWORD}
        )
    assert result["errors"] == {"base": "token_not_accepted"}


async def test_invalid_network(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    mock_node(aioclient_mock)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_OTA_PASSWORD: OTA_PASSWORD, CONF_RESTRICT_NETWORK: "not an address"},
    )
    assert result["errors"] == {CONF_RESTRICT_NETWORK: "invalid_network"}


async def test_already_configured(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
) -> None:
    mock_config_entry.add_to_hass(hass)
    mock_node(aioclient_mock)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_OTA_PASSWORD: OTA_PASSWORD}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
) -> None:
    mock_config_entry.add_to_hass(hass)
    mock_node(aioclient_mock, agents=[{"name": TOKEN_NAME}])
    result = await mock_config_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_OTA_PASSWORD: OTA_PASSWORD}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_config_entry.data[CONF_TOKEN] == NEW_TOKEN
    assert mock_config_entry.data[CONF_TOKEN_NAME] == TOKEN_NAME


async def test_reauth_wrong_cluster(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
) -> None:
    mock_config_entry.add_to_hass(hass)
    other = config(NODE01)
    other["tls"]["server_name"] = "dns.other.example"
    mock_node(aioclient_mock, answers={("get", "/api/config"): {"json": other}})
    result = await mock_config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_OTA_PASSWORD: OTA_PASSWORD}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_cluster"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", []),
        ("192.0.2.10", ["192.0.2.10/32"]),
        ("192.0.2.0/24, 2001:db8::1", ["192.0.2.0/24", "2001:db8::1/128"]),
    ],
)
def test_parse_networks(text: str, expected: list[str]) -> None:
    assert parse_networks(text) == expected


def test_parse_networks_limits() -> None:
    with pytest.raises(ValueError, match="too many"):
        parse_networks("192.0.2.1 192.0.2.2 192.0.2.3 192.0.2.4 192.0.2.5")


HASSIO = HassioServiceInfo(
    config={
        "host": "dns04.example.com",
        "port": 443,
        "ssl": True,
        "server_name": "dns.example.com",
    },
    name="DNS Core",
    slug="local_dnscore",
    uuid="1234",
)


def mock_app_node(aioclient_mock: AiohttpClientMocker) -> None:
    """dns04, the app's node, as the flow meets it."""
    base = "https://dns04.example.com"
    aioclient_mock.get(f"{base}/api/auth/status", json=load("auth_status.json"))
    aioclient_mock.get(f"{base}/api/config", json=config(NODE04))
    aioclient_mock.get(f"{base}/api/cluster/status", json=view(NODE04))
    aioclient_mock.get(f"{base}/api/agents", json={"agents": []})
    aioclient_mock.post(
        f"{base}/api/agents", json={**load("agents_create.json"), "token": NEW_TOKEN}
    )


async def test_hassio(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """The app's Supervisor discovery (0.95.0) leads to the password step."""
    mock_app_node(aioclient_mock)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_HASSIO}, data=HASSIO
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "hassio_confirm"
    assert result["description_placeholders"] == {
        "addon": "DNS Core",
        "host": "dns04.example.com",
    }
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "credentials"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_OTA_PASSWORD: OTA_PASSWORD}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == UNIQUE_ID
    assert result["data"][CONF_HOST] == "dns04.example.com"
    assert result["data"][CONF_PORT] == 443


async def test_hassio_unreachable(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(
        "https://dns04.example.com/api/auth/status", exc=ClientConnectionError()
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_HASSIO}, data=HASSIO
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "hassio_confirm"
    assert result["errors"] == {"base": "cannot_connect"}


@pytest.mark.parametrize(
    "payload", [{"port": 443}, {"host": "dns04.example.com", "port": "nope"}]
)
async def test_hassio_invalid(hass: HomeAssistant, payload: dict[str, Any]) -> None:
    info = HassioServiceInfo(
        config=payload, name="DNS Core", slug="local_dnscore", uuid="1"
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_HASSIO}, data=info
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "invalid_discovery_info"


async def test_hassio_already_configured(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """The app's cluster is already set up: recognised by its name."""
    mock_config_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_HASSIO}, data=HASSIO
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
