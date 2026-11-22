"""The integration against a real DNS Core node (opt-in).

Skipped unless `DNSCORE_TEST_URL` names a running node's HTTPS base URL and
`DNSCORE_TEST_OTA_FILE` a file holding its OTA password. The node must be
a throwaway, never a member of a real cluster: the test mints and revokes
tokens on it, and turns its blocking off and on. For example, the Linux
build in a container, with clustering off and no update URL:

    docker run -d --name dnscore-test --platform linux/amd64 \\
      -v $PWD/dnscore-linux-amd64:/dnscore:ro -v $PWD/work:/work \\
      -p 127.0.0.1:28443:443 alpine:3.24 /dnscore -d /work/data \\
      --config /work/config.json --tls-cert /work/cert.pem \\
      --tls-key /work/key.pem --upstream 127.0.0.1:5399

    DNSCORE_TEST_URL=https://127.0.0.1:28443 \\
    DNSCORE_TEST_OTA_FILE=work/ota.txt .venv/bin/pytest tests/test_live_node.py

`config.json` there holds `{"tls": {"enabled": true, "server_name":
"dns.example.com"}, "ota_password": "...", "update_url": ""}`.
"""

from __future__ import annotations

import os
from pathlib import Path
import time

import pytest
from yarl import URL

from custom_components.dnscore.const import (
    CONF_OTA_PASSWORD,
    CONF_RESTRICT_NETWORK,
    DOMAIN,
    LOCAL_NODE,
    SECTION_CONNECTION,
    TOKEN_SCOPES,
)
from custom_components.dnscore.pydnscore import DnscoreClient
from homeassistant.config_entries import SOURCE_USER, ConfigEntry, ConfigEntryState
from homeassistant.const import (
    ATTR_ENTITY_ID,
    CONF_HOST,
    CONF_PORT,
    CONF_SSL,
    CONF_VERIFY_SSL,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

NODE_URL = os.environ.get("DNSCORE_TEST_URL", "")
OTA_FILE = os.environ.get("DNSCORE_TEST_OTA_FILE", "")

pytestmark = [
    pytest.mark.live_node,
    pytest.mark.skipif(
        not NODE_URL or not OTA_FILE or not Path(OTA_FILE).is_file(),
        reason="set DNSCORE_TEST_URL and DNSCORE_TEST_OTA_FILE to a throwaway node",
    ),
    pytest.mark.usefixtures("socket_enabled"),
]


def _state(hass: HomeAssistant, platform: Platform, unique_id: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, unique_id)
    assert entity_id is not None, unique_id
    state = hass.states.get(entity_id)
    assert state is not None, entity_id
    return state.state


def _entity(hass: HomeAssistant, platform: Platform, unique_id: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, unique_id)
    assert entity_id is not None, unique_id
    return entity_id


async def _configure(hass: HomeAssistant, password: str) -> ConfigEntry:
    url = URL(NODE_URL)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_HOST: url.host,
            CONF_PORT: url.port,
            # A throwaway node's certificate is its own.
            SECTION_CONNECTION: {CONF_SSL: True, CONF_VERIFY_SSL: False},
        },
    )
    assert result["step_id"] == "credentials", result.get("errors")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_OTA_PASSWORD: password, CONF_RESTRICT_NETWORK: ""}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY, result.get("errors")
    await hass.async_block_till_done()
    return result["result"]


@pytest.fixture
def password() -> str:
    """The throwaway node's OTA password."""
    return Path(OTA_FILE).read_text().strip()


async def test_live_node(hass: HomeAssistant, password: str) -> None:
    entry = await _configure(hass, password)
    assert entry.state is ConfigEntryState.LOADED
    assert entry.unique_id == "dns.example.com"

    admin = DnscoreClient(
        async_get_clientsession(hass, verify_ssl=False),
        f"{NODE_URL}/",
        verify_ssl=False,
    )
    tokens = {t.name: t for t in await admin.list_tokens(ota_password=password)}
    token = tokens[entry.data["token_name"]]
    assert sorted(token.scopes) == sorted(TOKEN_SCOPES)
    assert token.expires_at == 0

    # A node with clustering off: one device, read where it was configured.
    runtime = entry.runtime_data
    assert list(runtime.nodes.nodes) == [LOCAL_NODE]
    node_key = f"{entry.unique_id}-{LOCAL_NODE}"
    cluster_key = f"cluster-{entry.unique_id}"
    device = await admin.get_device(ota_password=password)
    assert _state(hass, Platform.SENSOR, f"{node_key}_version") == device.version
    assert _state(hass, Platform.SENSOR, f"{cluster_key}_members") == "unavailable"
    assert _state(hass, Platform.SENSOR, f"{cluster_key}_certificate_expiry") not in (
        "unavailable",
        "unknown",
    )
    # The Linux build: no board memory sensors.
    assert (
        er.async_get(hass).async_get_entity_id(
            Platform.SENSOR, DOMAIN, f"{node_key}_dma_free"
        )
        is None
    )

    # Blocking off, on, and paused, as the node itself then reports it.
    switch = _entity(hass, Platform.SWITCH, f"{cluster_key}_blocking")
    assert hass.states.get(switch).state == "on"
    await hass.services.async_call(
        "switch", "turn_off", {ATTR_ENTITY_ID: switch}, blocking=True
    )
    await hass.async_block_till_done()
    assert hass.states.get(switch).state == "off"
    assert (await admin.get_config(ota_password=password)).blocking_enabled is False
    await hass.services.async_call(
        "switch", "turn_on", {ATTR_ENTITY_ID: switch}, blocking=True
    )
    await hass.async_block_till_done()
    assert hass.states.get(switch).state == "on"

    await hass.services.async_call(
        DOMAIN,
        "pause_blocking",
        {"config_entry_id": entry.entry_id, "duration": {"minutes": 5}},
        blocking=True,
    )
    await hass.async_block_till_done()
    paused = await admin.get_config(ota_password=password)
    assert paused.blocking_enabled is False
    assert paused.blocking_resume_at is not None
    assert abs(paused.blocking_resume_at - (time.time() + 300)) < 30
    assert hass.states.get(switch).attributes["resumes_at"]
    await hass.services.async_call(
        "switch", "turn_on", {ATTR_ENTITY_ID: switch}, blocking=True
    )

    # The cache flush works; the blocklist refresh is refused in the node's words
    # (this throwaway node has no blocklist URL).
    await hass.services.async_call(
        "button",
        "press",
        {ATTR_ENTITY_ID: _entity(hass, Platform.BUTTON, f"{node_key}_flush_cache")},
        blocking=True,
    )
    with pytest.raises(ServiceValidationError, match="blocklist_url"):
        await hass.services.async_call(
            "button",
            "press",
            {
                ATTR_ENTITY_ID: _entity(
                    hass, Platform.BUTTON, f"{cluster_key}_refresh_blocklist"
                )
            },
            blocking=True,
        )

    # Revoked behind Home Assistant's back: the next refresh asks for reauth.
    await admin.revoke_token(ota_password=password, name=token.name)
    await runtime.cluster.async_refresh()
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flows] == ["reauth"]

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
