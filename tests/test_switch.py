"""The blocking switch and the pause action."""

from __future__ import annotations

import json
import time

import pytest
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.test_util.aiohttp import AiohttpClientMocker

from custom_components.dnscore.const import DOMAIN
from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er

from .common import NODES, URL01, cluster_unique_id, config, mock_cluster


def _switch(hass: HomeAssistant) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(
        "switch", DOMAIN, cluster_unique_id("blocking")
    )
    assert entity_id is not None
    return entity_id


def _config_posts(aioclient_mock: AiohttpClientMocker) -> list[dict]:
    return [
        json.loads(data)
        for method, url, data, _ in aioclient_mock.mock_calls
        if method == "POST" and str(url) == f"{URL01}/api/config"
    ]


async def test_on(hass: HomeAssistant, init_integration: MockConfigEntry) -> None:
    assert hass.states.get(_switch(hass)).state == STATE_ON


async def test_turn_off_and_on(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    entity_id = _switch(hass)
    await hass.services.async_call(
        "switch", "turn_off", {ATTR_ENTITY_ID: entity_id}, blocking=True
    )
    await hass.services.async_call(
        "switch", "turn_on", {ATTR_ENTITY_ID: entity_id}, blocking=True
    )
    assert _config_posts(aioclient_mock) == [
        {"blocking_enabled": False, "blocking_resume_at": 0},
        {"blocking_enabled": True},
    ]


async def test_state_follows_the_answer_at_once(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """The node's answer is the new state: no wait for a (debounced) refresh."""
    off = {"blocking_enabled": False, "blocking_resume_at": 0}
    mock_cluster(
        aioclient_mock,
        post_overrides={(n, "/api/config"): (200, config(n, **off)) for n in NODES},
    )
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    entity_id = _switch(hass)
    gets_before = sum(
        1
        for method, url, _, _ in aioclient_mock.mock_calls
        if method == "GET" and str(url).endswith("/api/cluster/status")
    )
    await hass.services.async_call(
        "switch", "turn_off", {ATTR_ENTITY_ID: entity_id}, blocking=True
    )
    assert hass.states.get(entity_id).state == STATE_OFF
    gets_after = sum(
        1
        for method, url, _, _ in aioclient_mock.mock_calls
        if method == "GET" and str(url).endswith("/api/cluster/status")
    )
    assert gets_after == gets_before


async def test_paused_shows_off_with_resume_time(
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
    state = hass.states.get(_switch(hass))
    assert state.state == STATE_OFF
    assert state.attributes["resumes_at"] == "2026-11-22T04:00:00+00:00"


async def test_refused_by_the_token(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    mock_cluster(
        aioclient_mock,
        post_overrides={
            (n, "/api/config"): (403, {"error": "it needs filtering:write"})
            for n in NODES
        },
    )
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    with pytest.raises(HomeAssistantError, match="filtering:write") as caught:
        await hass.services.async_call(
            "switch", "turn_off", {ATTR_ENTITY_ID: _switch(hass)}, blocking=True
        )
    assert caught.value.translation_key == "token_cannot"


async def test_pause_blocking(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    before = int(time.time())
    await hass.services.async_call(
        DOMAIN,
        "pause_blocking",
        {"config_entry_id": init_integration.entry_id, "duration": {"minutes": 10}},
        blocking=True,
    )
    after = int(time.time())
    [body] = _config_posts(aioclient_mock)
    assert body["blocking_enabled"] is False
    assert before + 600 <= body["blocking_resume_at"] <= after + 600


@pytest.mark.parametrize("duration", [{"seconds": 30}, {"hours": 25}])
async def test_pause_out_of_range(
    hass: HomeAssistant, init_integration: MockConfigEntry, duration: dict[str, int]
) -> None:
    with pytest.raises(ServiceValidationError) as caught:
        await hass.services.async_call(
            DOMAIN,
            "pause_blocking",
            {"config_entry_id": init_integration.entry_id, "duration": duration},
            blocking=True,
        )
    assert caught.value.translation_key == "pause_out_of_range"


async def test_pause_unknown_entry(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    with pytest.raises(ServiceValidationError) as caught:
        await hass.services.async_call(
            DOMAIN,
            "pause_blocking",
            {"config_entry_id": "nope", "duration": {"minutes": 5}},
            blocking=True,
        )
    assert caught.value.translation_key == "entry_not_found"
