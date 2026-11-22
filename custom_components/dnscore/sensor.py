"""Sensors: the cluster's totals and shared state, and each node's counters."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import override

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfInformation,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.util import dt as dt_util

from . import DnscoreConfigEntry
from .coordinator import (
    ClusterData,
    DnscoreClusterCoordinator,
    DnscoreNodeCoordinator,
    DnscoreNodeManager,
    NodeData,
)
from .entity import (
    DnscoreClusterEntity,
    DnscoreClusterTotalEntity,
    DnscoreMemberEntity,
    DnscoreNodeEntity,
    async_track_nodes,
)
from .pydnscore import ClusterMember, epoch_to_datetime

PARALLEL_UPDATES = 0

UNIT_QUERIES = "queries"
UNIT_QUERIES_PER_SECOND = "queries/s"
ACME_STATES = ["idle", "running", "ok", "failed"]
CONFIG_STATES = ["self", "syncing", "in-sync", "diverged", "out-of-sync"]

# A start time that moves by less than this between readings is jitter.
UPTIME_TOLERANCE = timedelta(minutes=1)


def _sum_or_none(values: list[float | None]) -> float | None:
    known = [v for v in values if v is not None]
    return sum(known) if known else None


def _ratio(blocked: int, total: int) -> float | None:
    return round(blocked * 100 / total, 2) if total > 0 else None


# ---- the cluster device ------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class DnscoreClusterSensorDescription(SensorEntityDescription):
    """A cluster sensor read from the cluster coordinator."""

    value_fn: Callable[[ClusterData], StateType | datetime]
    available_fn: Callable[[ClusterData], bool] = lambda _: True


def _leader(data: ClusterData) -> str | None:
    leader = data.leader
    if leader is not None:
        return leader.label
    return data.cluster.leader or None


def _acme_state(data: ClusterData) -> str | None:
    acme = data.shared.acme
    if acme is None or acme.state not in ACME_STATES:
        return None
    return acme.state


def _blocking_resumes(data: ClusterData) -> datetime | None:
    config = data.config
    if config.blocking_enabled is not False:
        return None
    return epoch_to_datetime(config.blocking_resume_at)


CLUSTER_SENSORS: tuple[DnscoreClusterSensorDescription, ...] = (
    DnscoreClusterSensorDescription(
        key="members",
        translation_key="members",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: sum(1 for m in d.cluster.members if m.online),
        available_fn=lambda d: d.clustered,
    ),
    DnscoreClusterSensorDescription(
        key="leader",
        translation_key="leader",
        value_fn=_leader,
        available_fn=lambda d: d.clustered,
    ),
    DnscoreClusterSensorDescription(
        key="term",
        translation_key="term",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.cluster.term,
        available_fn=lambda d: d.clustered,
    ),
    DnscoreClusterSensorDescription(
        key="blocking_resumes",
        translation_key="blocking_resumes",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=_blocking_resumes,
        available_fn=lambda d: d.config.blocking_enabled is not None,
    ),
    DnscoreClusterSensorDescription(
        key="blocklist_domains",
        translation_key="blocklist_domains",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.shared.blocklist.domains if d.shared.blocklist else None,
        available_fn=lambda d: d.shared.blocklist is not None,
    ),
    DnscoreClusterSensorDescription(
        key="blocklist_built",
        translation_key="blocklist_built",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda d: d.shared.blocklist.built if d.shared.blocklist else None,
        available_fn=lambda d: d.shared.blocklist is not None,
    ),
    DnscoreClusterSensorDescription(
        key="blocklist_checked",
        translation_key="blocklist_checked",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: (
            d.shared.blocklist.last_success if d.shared.blocklist else None
        ),
        available_fn=lambda d: d.shared.blocklist is not None,
    ),
    DnscoreClusterSensorDescription(
        key="certificate_expiry",
        translation_key="certificate_expiry",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda d: d.shared.tls.not_after if d.shared.tls else None,
        available_fn=lambda d: d.shared.tls is not None and d.shared.tls.ready,
    ),
    DnscoreClusterSensorDescription(
        key="certificate_renewal",
        translation_key="certificate_renewal",
        device_class=SensorDeviceClass.ENUM,
        options=ACME_STATES,
        value_fn=_acme_state,
        available_fn=lambda d: d.shared.acme is not None and d.shared.acme.enabled,
    ),
    DnscoreClusterSensorDescription(
        key="certificate_renewed",
        translation_key="certificate_renewed",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.shared.acme.last_success if d.shared.acme else None,
        available_fn=lambda d: d.shared.acme is not None and d.shared.acme.enabled,
    ),
)


# ---- the cluster's totals over its nodes -------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class DnscoreTotalSensorDescription(SensorEntityDescription):
    """A cluster sensor computed from every node that answered."""

    value_fn: Callable[[ClusterData, list[NodeData]], StateType]


def _total_ratio(nodes: list[NodeData]) -> float | None:
    return _ratio(
        sum(n.stats.queries_blocked for n in nodes),
        sum(n.stats.queries_total for n in nodes),
    )


TOTAL_SENSORS: tuple[DnscoreTotalSensorDescription, ...] = (
    DnscoreTotalSensorDescription(
        key="queries_rate",
        translation_key="queries_rate",
        native_unit_of_measurement=UNIT_QUERIES_PER_SECOND,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda _, nodes: _sum_or_none([n.rates.queries for n in nodes]),
    ),
    DnscoreTotalSensorDescription(
        key="blocked_rate",
        translation_key="blocked_rate",
        native_unit_of_measurement=UNIT_QUERIES_PER_SECOND,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda _, nodes: _sum_or_none([n.rates.blocked for n in nodes]),
    ),
    DnscoreTotalSensorDescription(
        key="blocked_ratio",
        translation_key="blocked_ratio",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda _, nodes: _total_ratio(nodes),
    ),
)


# ---- each node: from the member list -----------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class DnscoreMemberSensorDescription(SensorEntityDescription):
    """A node sensor read from the cluster's member list."""

    value_fn: Callable[[ClusterMember], StateType]


MEMBER_SENSORS: tuple[DnscoreMemberSensorDescription, ...] = (
    DnscoreMemberSensorDescription(
        key="config_state",
        translation_key="config_state",
        device_class=SensorDeviceClass.ENUM,
        options=CONFIG_STATES,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda m: m.config_state if m.config_state in CONFIG_STATES else None,
    ),
)


# ---- each node: from its own counters ----------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class DnscoreNodeSensorDescription(SensorEntityDescription):
    """A node sensor read from the node itself."""

    value_fn: Callable[[NodeData], StateType | datetime]
    boards_only: bool = False
    start_time: bool = False


def _started(data: NodeData) -> datetime:
    return dt_util.utcnow() - timedelta(seconds=data.stats.uptime_seconds)


def _forwarded_p90(data: NodeData) -> float | None:
    latency = data.stats.latency.get("forwarded")
    if latency is None or latency.count == 0:
        return None
    return latency.p90_us / 1000


def _cache_hit_rate(data: NodeData) -> float | None:
    """The node's percentage (it writes 0 before any lookup: unknown here)."""
    cache = data.stats.cache
    if not cache.enabled or cache.hits + cache.misses == 0:
        return None
    if cache.hit_rate is not None:
        return round(cache.hit_rate, 1)
    return round(cache.hits * 100 / (cache.hits + cache.misses), 1)


def _memory(
    value_fn: Callable[[NodeData], int | None],
) -> Callable[[NodeData], int | None]:
    def read(data: NodeData) -> int | None:
        if data.device is None:
            return None
        return value_fn(data)

    return read


NODE_SENSORS: tuple[DnscoreNodeSensorDescription, ...] = (
    DnscoreNodeSensorDescription(
        key="queries",
        translation_key="queries",
        native_unit_of_measurement=UNIT_QUERIES,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda d: d.stats.queries_total,
    ),
    DnscoreNodeSensorDescription(
        key="blocked",
        translation_key="blocked",
        native_unit_of_measurement=UNIT_QUERIES,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda d: d.stats.queries_blocked,
    ),
    DnscoreNodeSensorDescription(
        key="cached",
        translation_key="cached",
        native_unit_of_measurement=UNIT_QUERIES,
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.stats.queries_cached,
    ),
    DnscoreNodeSensorDescription(
        key="forwarded",
        translation_key="forwarded",
        native_unit_of_measurement=UNIT_QUERIES,
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.stats.queries_forwarded,
    ),
    DnscoreNodeSensorDescription(
        key="queries_rate",
        translation_key="queries_rate",
        native_unit_of_measurement=UNIT_QUERIES_PER_SECOND,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda d: d.rates.queries,
    ),
    DnscoreNodeSensorDescription(
        key="blocked_ratio",
        translation_key="blocked_ratio",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda d: _ratio(d.stats.queries_blocked, d.stats.queries_total),
    ),
    DnscoreNodeSensorDescription(
        key="processing_time",
        translation_key="processing_time",
        native_unit_of_measurement=UnitOfTime.MILLISECONDS,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda d: d.stats.avg_processing_time_ms,
    ),
    DnscoreNodeSensorDescription(
        key="forwarded_p90",
        translation_key="forwarded_p90",
        native_unit_of_measurement=UnitOfTime.MILLISECONDS,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        suggested_display_precision=0,
        value_fn=_forwarded_p90,
    ),
    DnscoreNodeSensorDescription(
        key="cache_hit_rate",
        translation_key="cache_hit_rate",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=_cache_hit_rate,
    ),
    DnscoreNodeSensorDescription(
        key="cache_entries",
        translation_key="cache_entries",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.stats.cache.entries,
    ),
    DnscoreNodeSensorDescription(
        key="upstream_timeouts",
        translation_key="upstream_timeouts",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.stats.upstream_timeouts,
    ),
    DnscoreNodeSensorDescription(
        key="servfail_sent",
        translation_key="servfail_sent",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.stats.servfail_sent,
    ),
    DnscoreNodeSensorDescription(
        key="uptime",
        translation_key="uptime",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_started,
        start_time=True,
    ),
    DnscoreNodeSensorDescription(
        key="version",
        translation_key="version",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.stats.firmware_version or None,
    ),
    DnscoreNodeSensorDescription(
        key="internal_free",
        translation_key="internal_free",
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.KIBIBYTES,
        device_class=SensorDeviceClass.DATA_SIZE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        boards_only=True,
        value_fn=_memory(lambda d: d.stats.memory_free_bytes),
    ),
    DnscoreNodeSensorDescription(
        key="dma_free",
        translation_key="dma_free",
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.KIBIBYTES,
        device_class=SensorDeviceClass.DATA_SIZE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        boards_only=True,
        value_fn=_memory(lambda d: d.stats.dma_free_bytes),
    ),
    DnscoreNodeSensorDescription(
        key="dma_largest",
        translation_key="dma_largest",
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.KIBIBYTES,
        device_class=SensorDeviceClass.DATA_SIZE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        boards_only=True,
        value_fn=_memory(lambda d: d.stats.dma_largest_block),
    ),
    DnscoreNodeSensorDescription(
        key="dma_low",
        translation_key="dma_low",
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.KIBIBYTES,
        device_class=SensorDeviceClass.DATA_SIZE,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        boards_only=True,
        value_fn=_memory(
            lambda d: (
                d.device.memory.dma_low.free
                if d.device is not None and d.device.memory.dma_low is not None
                else None
            )
        ),
    ),
    DnscoreNodeSensorDescription(
        key="psram_free",
        translation_key="psram_free",
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.MEBIBYTES,
        device_class=SensorDeviceClass.DATA_SIZE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        boards_only=True,
        value_fn=_memory(lambda d: d.stats.psram_free_bytes),
    ),
)


def _node_entities(node: DnscoreNodeCoordinator) -> list[SensorEntity]:
    board = node.is_board
    entities: list[SensorEntity] = [
        DnscoreNodeSensor(node, description)
        for description in NODE_SENSORS
        if board or not description.boards_only
    ]
    if node.clustered:
        entities.extend(DnscoreMemberSensor(node, d) for d in MEMBER_SENSORS)
    return entities


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DnscoreConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the cluster's sensors and each node's, now and as members join."""
    runtime = entry.runtime_data
    entities: list[SensorEntity] = [
        DnscoreClusterSensor(runtime.cluster, description)
        for description in CLUSTER_SENSORS
    ]
    entities.extend(
        DnscoreTotalSensor(runtime.cluster, runtime.nodes, description)
        for description in TOTAL_SENSORS
    )
    async_add_entities(entities)

    @callback
    def _async_add_nodes(nodes: list[DnscoreNodeCoordinator]) -> None:
        async_add_entities([e for node in nodes for e in _node_entities(node)])

    async_track_nodes(entry, _async_add_nodes)


class DnscoreClusterSensor(DnscoreClusterEntity, SensorEntity):
    """A sensor of the cluster device."""

    entity_description: DnscoreClusterSensorDescription

    def __init__(
        self,
        coordinator: DnscoreClusterCoordinator,
        description: DnscoreClusterSensorDescription,
    ) -> None:
        """Describe the sensor."""
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
    def native_value(self) -> StateType | datetime:
        """The value."""
        return self.entity_description.value_fn(self.coordinator.data)


class DnscoreTotalSensor(DnscoreClusterTotalEntity, SensorEntity):
    """A cluster sensor computed over the nodes."""

    entity_description: DnscoreTotalSensorDescription

    def __init__(
        self,
        coordinator: DnscoreClusterCoordinator,
        nodes: DnscoreNodeManager,
        description: DnscoreTotalSensorDescription,
    ) -> None:
        """Describe the sensor."""
        super().__init__(coordinator, nodes, description.key)
        self.entity_description = description

    @property
    @override
    def native_value(self) -> StateType:
        """The value over the nodes that answered."""
        return self.entity_description.value_fn(self.coordinator.data, self.node_data)


class DnscoreMemberSensor(DnscoreMemberEntity, SensorEntity):
    """A node sensor read from the member list."""

    entity_description: DnscoreMemberSensorDescription

    def __init__(
        self, node: DnscoreNodeCoordinator, description: DnscoreMemberSensorDescription
    ) -> None:
        """Describe the sensor."""
        super().__init__(node, description.key)
        self.entity_description = description

    @property
    @override
    def available(self) -> bool:
        """Available while the cluster lists the node."""
        return super().available and self.member is not None

    @property
    @override
    def native_value(self) -> StateType:
        """The value."""
        member = self.member
        return None if member is None else self.entity_description.value_fn(member)


class DnscoreNodeSensor(DnscoreNodeEntity, SensorEntity):
    """A node sensor read from the node itself."""

    entity_description: DnscoreNodeSensorDescription

    def __init__(
        self, node: DnscoreNodeCoordinator, description: DnscoreNodeSensorDescription
    ) -> None:
        """Describe the sensor."""
        super().__init__(node, description.key)
        self.entity_description = description
        self._start: datetime | None = None

    @property
    @override
    def native_value(self) -> StateType | datetime:
        """The value; a start time keeps still unless it really moved."""
        data: NodeData | None = self.coordinator.data  # None until it first answered
        if data is None:
            return None
        value = self.entity_description.value_fn(data)
        if not self.entity_description.start_time or not isinstance(value, datetime):
            return value
        if self._start is None or abs(value - self._start) > UPTIME_TOLERANCE:
            self._start = value
        return self._start
