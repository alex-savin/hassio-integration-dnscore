"""Buttons: refresh the cluster's blocklist, and flush a node's answer cache."""

from __future__ import annotations

from typing import override

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import DnscoreConfigEntry
from .coordinator import DnscoreClusterCoordinator, DnscoreNodeCoordinator
from .entity import DnscoreClusterEntity, DnscoreNodeEntity, async_track_nodes
from .errors import action_error
from .pydnscore import DnscoreError

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DnscoreConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the blocklist button, and a cache button for each node."""
    async_add_entities([DnscoreRefreshBlocklistButton(entry.runtime_data.cluster)])

    @callback
    def _add(nodes: list[DnscoreNodeCoordinator]) -> None:
        async_add_entities(DnscoreFlushCacheButton(node) for node in nodes)

    async_track_nodes(entry, _add)


class DnscoreRefreshBlocklistButton(DnscoreClusterEntity, ButtonEntity):
    """Download the blocklist now (the leader downloads; the members copy it)."""

    _attr_translation_key = "refresh_blocklist"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: DnscoreClusterCoordinator) -> None:
        """Attach the button to the cluster device."""
        super().__init__(coordinator, "refresh_blocklist")

    @override
    async def async_press(self) -> None:
        """Ask the leader to download the blocklist."""
        try:
            await self.coordinator.async_call_leader(lambda c: c.refresh_blocklist())
        except DnscoreError as err:
            raise action_error(err) from err
        self.coordinator.async_expire_shared()
        await self.coordinator.async_request_refresh()


class DnscoreFlushCacheButton(DnscoreNodeEntity, ButtonEntity):
    """Empty one node's answer cache."""

    _attr_translation_key = "flush_cache"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, node: DnscoreNodeCoordinator) -> None:
        """Attach the button to the node's device."""
        super().__init__(node, "flush_cache")

    @override
    async def async_press(self) -> None:
        """Ask the node to flush its cache."""
        try:
            await self.coordinator.async_call(lambda c: c.flush_cache())
        except DnscoreError as err:
            raise action_error(err) from err
        await self.coordinator.async_request_refresh()
