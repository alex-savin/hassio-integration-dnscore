"""Binary sensors: the cluster's problems, and each node's state."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, override

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import DnscoreConfigEntry
from .const import CONFIG_PROBLEM_STATES
from .coordinator import (
    ClusterData,
    DnscoreClusterCoordinator,
    DnscoreNodeCoordinator,
    NodeData,
)
from .entity import (
    DnscoreClusterEntity,
    DnscoreMemberEntity,
    DnscoreNodeEntity,
    async_track_nodes,
)

PARALLEL_UPDATES = 0


# ---- the cluster device ------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class DnscoreClusterBinaryDescription(BinarySensorEntityDescription):
    """A cluster binary sensor read from the cluster coordinator."""

    value_fn: Callable[[ClusterData], bool | None]
    available_fn: Callable[[ClusterData], bool] = lambda _: True


def _certificate_problem(data: ClusterData) -> bool | None:
    tls, acme = data.shared.tls, data.shared.acme
    if tls is None and acme is None:
        return None
    if tls is not None and (tls.expired or not tls.ready):
        return True
    return acme is not None and acme.enabled and (acme.failing or acme.name_missing)


def _blocklist_problem(data: ClusterData) -> bool | None:
    blocklist = data.shared.blocklist
    if blocklist is None:
        return None
    return not blocklist.loaded or blocklist.last_ok is False


CLUSTER_BINARY_SENSORS: tuple[DnscoreClusterBinaryDescription, ...] = (
    DnscoreClusterBinaryDescription(
        key="certificate",
        translation_key="certificate",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=_certificate_problem,
        available_fn=lambda d: d.shared.tls is not None or d.shared.acme is not None,
    ),
    DnscoreClusterBinaryDescription(
        key="blocklist",
        translation_key="blocklist",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=_blocklist_problem,
        available_fn=lambda d: d.shared.blocklist is not None,
    ),
    DnscoreClusterBinaryDescription(
        key="shared_settings",
        translation_key="shared_settings",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: any(
            m.config_state in CONFIG_PROBLEM_STATES for m in d.cluster.members
        ),
        available_fn=lambda d: d.clustered,
    ),
)


# ---- each node: from the member list -----------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class DnscoreMemberBinaryDescription(BinarySensorEntityDescription):
    """A node binary sensor read from the cluster's member list.

    `value_fn` gets the cluster data and the node ID: a member the cluster
    does not list is off, not unknown (it is not there).
    """

    value_fn: Callable[[ClusterData, str], bool]


def _listed(data: ClusterData, node_id: str) -> bool:
    member = data.member(node_id)
    return member is not None and member.online


MEMBER_BINARY_SENSORS: tuple[DnscoreMemberBinaryDescription, ...] = (
    DnscoreMemberBinaryDescription(
        key="online",
        translation_key="online",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        value_fn=_listed,
    ),
    DnscoreMemberBinaryDescription(
        key="leader",
        translation_key="leader",
        value_fn=lambda d, node_id: d.cluster.is_leader(node_id),
    ),
    DnscoreMemberBinaryDescription(
        key="serving",
        translation_key="serving",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d, node_id: (m := d.member(node_id)) is not None and m.serving,
    ),
)


# ---- each node: from its own counters ----------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class DnscoreNodeBinaryDescription(BinarySensorEntityDescription):
    """A node binary sensor read from the node itself."""

    value_fn: Callable[[NodeData], bool | None]
    attributes_fn: Callable[[NodeData], dict[str, Any]] | None = None
    available_fn: Callable[[NodeData], bool] = lambda _: True


def _health_attributes(data: NodeData) -> dict[str, Any]:
    health = data.health
    if health is None:
        return {}
    return {
        "status": health.status,
        "conditions": [
            {"id": c.id, "severity": c.severity, "text": c.text}
            for c in health.conditions
        ],
    }


NODE_BINARY_SENSORS: tuple[DnscoreNodeBinaryDescription, ...] = (
    # DNS Core 0.95.0 (API-29): the node's own verdict on what needs a person.
    DnscoreNodeBinaryDescription(
        key="health",
        translation_key="health",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda d: d.health.problem if d.health is not None else None,
        attributes_fn=_health_attributes,
        available_fn=lambda d: d.health is not None,
    ),
    DnscoreNodeBinaryDescription(
        key="upstreams",
        translation_key="upstreams",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda d: any(u.failing for u in d.stats.upstreams),
        attributes_fn=lambda d: {
            "failing": sum(1 for u in d.stats.upstreams if u.failing),
            "upstreams": len(d.stats.upstreams),
        },
    ),
    DnscoreNodeBinaryDescription(
        key="blocking",
        translation_key="blocking",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.stats.blocking_in_effect,
    ),
)


def _node_entities(node: DnscoreNodeCoordinator) -> list[BinarySensorEntity]:
    entities: list[BinarySensorEntity] = [
        DnscoreNodeBinarySensor(node, d) for d in NODE_BINARY_SENSORS
    ]
    if node.clustered:
        entities.extend(
            DnscoreMemberBinarySensor(node, d) for d in MEMBER_BINARY_SENSORS
        )
    return entities


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DnscoreConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the cluster's binary sensors and each node's."""
    runtime = entry.runtime_data
    async_add_entities(
        DnscoreClusterBinarySensor(runtime.cluster, d) for d in CLUSTER_BINARY_SENSORS
    )

    @callback
    def _async_add_nodes(nodes: list[DnscoreNodeCoordinator]) -> None:
        async_add_entities([e for node in nodes for e in _node_entities(node)])

    async_track_nodes(entry, _async_add_nodes)


class DnscoreClusterBinarySensor(DnscoreClusterEntity, BinarySensorEntity):
    """A binary sensor of the cluster device."""

    entity_description: DnscoreClusterBinaryDescription

    def __init__(
        self,
        coordinator: DnscoreClusterCoordinator,
        description: DnscoreClusterBinaryDescription,
    ) -> None:
        """Describe the binary sensor."""
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    @override
    def available(self) -> bool:
        """Available while the cluster answers and has the value."""
        return super().available and self.entity_description.available_fn(
            self.coordinator.data
        )

    @property
    @override
    def is_on(self) -> bool | None:
        """The state."""
        return self.entity_description.value_fn(self.coordinator.data)


class DnscoreMemberBinarySensor(DnscoreMemberEntity, BinarySensorEntity):
    """A node binary sensor read from the member list."""

    entity_description: DnscoreMemberBinaryDescription

    def __init__(
        self, node: DnscoreNodeCoordinator, description: DnscoreMemberBinaryDescription
    ) -> None:
        """Describe the binary sensor."""
        super().__init__(node, description.key)
        self.entity_description = description

    @property
    @override
    def is_on(self) -> bool:
        """The state."""
        return self.entity_description.value_fn(self.coordinator.data, self.node_id)


class DnscoreNodeBinarySensor(DnscoreNodeEntity, BinarySensorEntity):
    """A node binary sensor read from the node itself."""

    entity_description: DnscoreNodeBinaryDescription

    def __init__(
        self, node: DnscoreNodeCoordinator, description: DnscoreNodeBinaryDescription
    ) -> None:
        """Describe the binary sensor."""
        super().__init__(node, description.key)
        self.entity_description = description

    @property
    @override
    def available(self) -> bool:
        """Available while the node answers and has the value."""
        data: NodeData | None = self.coordinator.data
        return (
            super().available
            and data is not None
            and self.entity_description.available_fn(data)
        )

    @property
    @override
    def is_on(self) -> bool | None:
        """The state."""
        data: NodeData | None = self.coordinator.data
        return None if data is None else self.entity_description.value_fn(data)

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Counts behind the state, when the description has them."""
        data: NodeData | None = self.coordinator.data
        if data is None or self.entity_description.attributes_fn is None:
            return None
        return self.entity_description.attributes_fn(data)
