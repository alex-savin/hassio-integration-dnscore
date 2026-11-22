"""Data coordinators for DNS Core: one for the cluster, one per node.

The cluster coordinator reads `GET /api/cluster/status` and the settings
(`GET /api/config`: the cluster's name, its HTTPS port, blocking) from one
node: the configured one when it answers, else any other member. Every few
minutes it also reads what every member shares from the leader: the
certificate (`GET /api/tls/status`), its renewal (`GET /api/acme`, which
only the leader runs) and the blocklist (`GET /api/blocklist/update`, whose
downloads only the leader makes).

Each node has its own coordinator for its `GET /api/stats` and, now and
then, `GET /api/device`. DNS Core's relay is closed to tokens, so a node is
always asked directly: at `https://<host_name>/`, then at the address the
cluster reports for it. A member without a DNS name is asked at its address
under the cluster's name, which every member's certificate carries and its
Host check accepts.

`DnscoreNodeManager` follows the member list: it adds a node coordinator
(and its entities) when a member joins, and removes a member's device when
the cluster has not listed it for a while.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
import time
from typing import TYPE_CHECKING, Any

from homeassistant.const import (
    CONF_HOST,
    CONF_PORT,
    CONF_SSL,
    CONF_TOKEN,
    CONF_VERIFY_SSL,
)
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CLUSTER_SCAN_INTERVAL,
    CONF_KNOWN_URLS,
    DEVICE_REFRESH_INTERVAL,
    DOMAIN,
    ENDPOINT_BACKOFF,
    LOCAL_NODE,
    LOGGER,
    NODE_REMOVE_AFTER,
    NODE_SCAN_INTERVAL,
    SHARED_REFRESH_INTERVAL,
)
from .devices import node_device_details, node_identifier, node_is_board
from .pydnscore import (
    AcmeStatus,
    BlocklistStatus,
    ClusterMember,
    ClusterStatus,
    Device,
    DnscoreAuthenticationError,
    DnscoreClient,
    DnscoreConnectionError,
    DnscoreError,
    DnscoreForbiddenError,
    Health,
    ServerConfig,
    Stats,
    TlsStatus,
    build_url,
)

if TYPE_CHECKING:
    from . import DnscoreConfigEntry


def _now() -> float:
    return time.monotonic()


def address_key(url: str, address: str) -> str:
    """The backoff key of a node URL reached at an address instead of its name."""
    return f"{url}@{address}"


@dataclass(frozen=True, slots=True)
class Route:
    """One way to a node: a client, and the key `mark_failed` and `mark_ok` take."""

    client: DnscoreClient
    key: str


@dataclass(slots=True)
class SharedData:
    """What every member shares, read from the leader now and then."""

    tls: TlsStatus | None = None
    acme: AcmeStatus | None = None
    blocklist: BlocklistStatus | None = None


@dataclass(slots=True)
class ClusterData:
    """What the cluster coordinator knows after a refresh."""

    cluster: ClusterStatus
    config: ServerConfig
    endpoint: str
    shared: SharedData = field(default_factory=SharedData)

    @property
    def clustered(self) -> bool:
        """The answering node runs a cluster."""
        return self.cluster.enabled

    def node_ids(self) -> list[str]:
        """The members that get a device: every one the cluster lists."""
        if not self.cluster.enabled:
            return [LOCAL_NODE]
        return [m.node_id for m in self.cluster.members if m.node_id]

    def member(self, node_id: str) -> ClusterMember | None:
        """A member's record, as the answering node sees it."""
        if node_id == LOCAL_NODE:
            return self.cluster.self_member
        return self.cluster.member(node_id)

    @property
    def leader(self) -> ClusterMember | None:
        """The leader's record, when the answering node knows one."""
        if not self.cluster.leader:
            return None
        return self.cluster.member(self.cluster.leader)


@dataclass(slots=True)
class Rates:
    """Per-second rates from two readings of a node's counters."""

    queries: float | None = None
    blocked: float | None = None


@dataclass(slots=True)
class NodeData:
    """What a node coordinator knows after a refresh."""

    stats: Stats
    device: Device | None
    rates: Rates = field(default_factory=Rates)
    health: Health | None = None


class DnscoreClusterCoordinator(DataUpdateCoordinator[ClusterData]):
    """Reads the cluster from whichever member answers."""

    config_entry: DnscoreConfigEntry

    def __init__(self, hass: HomeAssistant, entry: DnscoreConfigEntry) -> None:
        """Set up the coordinator from the entry's connection data."""
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} cluster",
            update_interval=CLUSTER_SCAN_INTERVAL,
        )
        data = entry.data
        self._verify_ssl: bool = data[CONF_VERIFY_SSL]
        self._ssl: bool = data[CONF_SSL]
        self._token: str = data[CONF_TOKEN]
        self._session = async_get_clientsession(hass, verify_ssl=self._verify_ssl)
        self.configured_url = build_url(data[CONF_HOST], data[CONF_PORT], ssl=self._ssl)
        self._clients: dict[str, DnscoreClient] = {}
        self._backoff_until: dict[str, float] = {}
        self._shared = SharedData()
        self._shared_at: float | None = None
        self.active: Route | None = None

    # ---- endpoints ----------------------------------------------------------------------

    def client_for(self, url: str, address: str | None = None) -> DnscoreClient:
        """The client for one node URL, or for it at `address` (made once)."""
        key = url if address is None else address_key(url, address)
        if (client := self._clients.get(key)) is None:
            client = DnscoreClient(
                self._session,
                url,
                token=self._token,
                verify_ssl=self._verify_ssl,
                address=address,
            )
            self._clients[key] = client
        return client

    def _route(self, url: str, address: str | None = None) -> Route:
        key = url if address is None else address_key(url, address)
        return Route(self.client_for(url, address), key)

    def member_url(self, member: ClusterMember | None) -> str | None:
        """A member's console: its DNS name, on the cluster's HTTPS port."""
        if member is None or not member.host_name:
            return None
        port = self.data.config.port_https if self.data is not None else None
        return build_url(member.host_name, port, ssl=self._ssl)

    def member_routes(self, member: ClusterMember | None) -> list[Route]:
        """The ways to a member: its name, then its address; failed ones last.

        A member without a DNS name is reached at its address under the
        cluster's name (its certificate carries that name; asked by name,
        DNS would answer with the leader).
        """
        if member is None:
            return []
        routes: list[Route] = []
        if (url := self.member_url(member)) is not None:
            routes.append(self._route(url))
            if member.ip:
                routes.append(self._route(url, member.ip))
        elif member.ip and self.data is not None and self.data.config.server_name:
            port = self.data.config.port_https
            url = build_url(self.data.config.server_name, port, ssl=self._ssl)
            routes.append(self._route(url, member.ip))
        return [r for r in routes if not self.in_backoff(r.key)] + [
            r for r in routes if self.in_backoff(r.key)
        ]

    @property
    def active_client(self) -> DnscoreClient | None:
        """The client of the node that answered last."""
        return None if self.active is None else self.active.client

    @property
    def active_node_id(self) -> str | None:
        """The node ID of the node that answered last (None without a cluster)."""
        if self.data is None or not self.data.cluster.enabled:
            return None
        return self.data.cluster.node_id

    def in_backoff(self, key: str) -> bool:
        """The route failed recently: try it after the others."""
        return self._backoff_until.get(key, 0.0) > _now()

    def mark_failed(self, key: str) -> None:
        """Put a route that did not answer at the end of the queue for a while."""
        self._backoff_until[key] = _now() + ENDPOINT_BACKOFF.total_seconds()

    def mark_ok(self, key: str) -> None:
        """A route answered."""
        self._backoff_until.pop(key, None)

    def candidate_routes(self) -> list[Route]:
        """The configured node first, then the other members, failed ones last."""
        routes = [self._route(self.configured_url)]
        if self.data is not None:
            for member in self.data.cluster.members:
                routes.extend(self.member_routes(member))
        routes.extend(
            self._route(url) for url in self.config_entry.data.get(CONF_KNOWN_URLS, [])
        )
        unique = list({r.key: r for r in routes}.values())
        return [r for r in unique if not self.in_backoff(r.key)] + [
            r for r in unique if self.in_backoff(r.key)
        ]

    # ---- refresh ------------------------------------------------------------------------

    async def _async_update_data(self) -> ClusterData:
        """Read the cluster and its settings from the first node that answers."""
        last_error: DnscoreError | None = None
        for route in self.candidate_routes():
            try:
                cluster = await route.client.get_cluster_status()
                config = await route.client.get_config()
            except DnscoreAuthenticationError as err:
                raise ConfigEntryAuthFailed(
                    translation_domain=DOMAIN, translation_key="token_rejected"
                ) from err
            except DnscoreForbiddenError as err:
                raise ConfigEntryAuthFailed(
                    translation_domain=DOMAIN,
                    translation_key="token_refused",
                    translation_placeholders={"error": err.message},
                ) from err
            except DnscoreError as err:
                LOGGER.debug("%s did not answer: %s", route.key, err)
                self.mark_failed(route.key)
                last_error = err
                continue
            self.mark_ok(route.key)
            if self.active is None or route.key != self.active.key:
                LOGGER.debug("Reading the cluster from %s", route.key)
            self.active = route
            data = ClusterData(
                cluster=cluster, config=config, endpoint=route.client.url
            )
            await self._async_refresh_shared(route, data)
            data.shared = self._shared
            self._async_remember_urls(data)
            return data
        raise UpdateFailed(
            translation_domain=DOMAIN,
            translation_key="cluster_unreachable",
            translation_placeholders={"error": str(last_error)},
        )

    def leader_routes(self, data: ClusterData | None = None) -> list[Route]:
        """The ways to the leader, then the node that answered last."""
        data = data or self.data
        routes = self.member_routes(data.leader) if data is not None else []
        if self.active is not None and all(r.key != self.active.key for r in routes):
            routes.append(self.active)
        return routes

    async def _async_refresh_shared(self, answered: Route, data: ClusterData) -> None:
        """Read the certificate, its renewal and the blocklist, every few minutes.

        From the leader, which runs the renewal and the downloads; else from
        the node that answered. A route that does not connect moves on to
        the next; when none does, the next refresh tries again.
        """
        now = _now()
        if (
            self._shared_at is not None
            and now - self._shared_at < SHARED_REFRESH_INTERVAL.total_seconds()
        ):
            return
        for route in self.leader_routes(data):
            try:
                self._shared = await self._async_read_shared(route.client)
            except DnscoreConnectionError as err:
                LOGGER.debug("Shared readings via %s: %s", route.key, err)
                if route is not answered:
                    self.mark_failed(route.key)
                continue
            self._shared_at = now
            return

    async def _async_read_shared(self, client: DnscoreClient) -> SharedData:
        """One reading of what every member shares.

        Raises `DnscoreConnectionError` when the node is not reached; any
        other answer, an error one included, is the reading.
        """
        shared = SharedData()
        try:
            shared.tls = await client.get_tls_status()
        except DnscoreConnectionError:
            raise
        except DnscoreError as err:
            LOGGER.debug("Could not read the certificate: %s", err)
        try:
            shared.acme = await client.get_acme()
        except DnscoreConnectionError:
            raise
        except DnscoreError as err:
            LOGGER.debug("Could not read the renewal: %s", err)
        try:
            shared.blocklist = await client.get_blocklist_status()
        except DnscoreConnectionError:
            raise
        except DnscoreError as err:
            LOGGER.debug("Could not read the blocklist: %s", err)
        return shared

    def async_expire_shared(self) -> None:
        """Read the shared state again at the next refresh (after an action)."""
        self._shared_at = None

    async def async_apply_config(self, answer: dict[str, Any]) -> None:
        """Take the settings a `POST /api/config` answered with, at once.

        The node answers with the settings as they now are (the token's
        view of them). Waiting for a refresh instead would show the old
        state for up to the refresh debouncer's ten seconds after a second
        change in a row. An answer that is not the whole view asks for a
        refresh.
        """
        config = ServerConfig.from_dict(answer)
        if (
            self.data is None
            or "blocking_enabled" not in answer
            or not config.server_name
        ):
            await self.async_request_refresh()
            return
        self.async_set_updated_data(replace(self.data, config=config))

    @callback
    def _async_remember_urls(self, data: ClusterData) -> None:
        """Keep the members' URLs, so a restart can start from any of them."""
        urls = sorted(
            {url for m in data.cluster.members if (url := self.member_url(m))}
        )
        entry = self.config_entry
        if urls and urls != sorted(entry.data.get(CONF_KNOWN_URLS, [])):
            self.hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_KNOWN_URLS: urls}
            )

    # ---- actions ------------------------------------------------------------------------

    async def async_call_leader[T](
        self, call: Callable[[DnscoreClient], Awaitable[T]]
    ) -> T:
        """Make one call on the leader (else the answering node), by a route that connects."""
        last_error: DnscoreConnectionError | None = None
        for route in self.leader_routes():
            try:
                return await call(route.client)
            except DnscoreConnectionError as err:
                LOGGER.debug("Leader via %s: %s", route.key, err)
                last_error = err
        raise DnscoreConnectionError(str(last_error or "the cluster does not answer"))

    async def async_call_any[T](
        self, call: Callable[[DnscoreClient], Awaitable[T]]
    ) -> T:
        """Make one call on the node that answered last, else any member."""
        last_error: DnscoreConnectionError | None = None
        routes = [self.active] if self.active is not None else []
        routes += [r for r in self.candidate_routes() if r not in routes]
        for route in routes:
            try:
                return await call(route.client)
            except DnscoreConnectionError as err:
                LOGGER.debug("Call via %s: %s", route.key, err)
                last_error = err
        raise DnscoreConnectionError(str(last_error or "the cluster does not answer"))


class DnscoreNodeCoordinator(DataUpdateCoordinator[NodeData]):
    """Reads one node, directly."""

    config_entry: DnscoreConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: DnscoreConfigEntry,
        cluster: DnscoreClusterCoordinator,
        node_id: str,
    ) -> None:
        """Follow the node `node_id` (`LOCAL_NODE` without a cluster)."""
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} node {node_id}",
            update_interval=NODE_SCAN_INTERVAL,
        )
        self.node_id = node_id
        self.cluster = cluster
        self._device: Device | None = None
        self._device_at: float | None = None
        self._previous: tuple[float, Stats] | None = None
        # GET /api/health reaches a token from DNS Core 0.95.0 (API-29). A node
        # that refused it is asked again only once its version changes.
        self._health: Health | None = None
        self._health_refused_on: str | None = None

    @property
    def member(self) -> ClusterMember | None:
        """The node's record in the cluster's member list."""
        data: ClusterData | None = self.cluster.data  # None before the first refresh
        return None if data is None else data.member(self.node_id)

    @property
    def clustered(self) -> bool:
        """The node belongs to a cluster."""
        return self.node_id != LOCAL_NODE

    @property
    def url(self) -> str | None:
        """The node's console."""
        if not self.clustered:
            return self.cluster.configured_url
        return self.cluster.member_url(self.member)

    def _routes(self) -> list[Route]:
        """Ways to reach the node: its name, its address, and the answering node's route."""
        cc = self.cluster
        if not self.clustered:
            return [] if cc.active is None else [cc.active]
        routes = cc.member_routes(self.member)
        if (
            cc.active is not None
            and cc.active_node_id == self.node_id
            and all(r.key != cc.active.key for r in routes)
        ):
            routes.append(cc.active)
        return routes

    @property
    def is_board(self) -> bool:
        """The node is a board."""
        device = self.data.device if self.data is not None else None
        return node_is_board(device, self.member)

    async def async_call[T](self, call: Callable[[DnscoreClient], Awaitable[T]]) -> T:
        """Make one call on the node, by its first route that connects.

        Only an unreachable route moves on to the next; any answer, an error
        one included, is the node's.
        """
        last_error: DnscoreConnectionError | None = None
        for route in self._routes():
            try:
                return await call(route.client)
            except DnscoreConnectionError as err:
                LOGGER.debug("Node %s via %s: %s", self.node_id, route.key, err)
                last_error = err
        raise DnscoreConnectionError(str(last_error or "no route to the node"))

    async def _async_update_data(self) -> NodeData:
        """Read the node's counters and, now and then, its device."""
        last_error: Exception | None = None
        for route in self._routes():
            try:
                stats = await route.client.get_stats()
            except DnscoreError as err:
                LOGGER.debug("Node %s via %s: %s", self.node_id, route.key, err)
                self.cluster.mark_failed(route.key)
                last_error = err
                continue
            if self.clustered and stats.node_id != self.node_id:
                # A name DNS answers with another member (the cluster's).
                last_error = DnscoreError(
                    f"{route.client.url} answered for {stats.node_id}"
                )
                LOGGER.debug("Node %s: %s", self.node_id, last_error)
                self.cluster.mark_failed(route.key)
                continue
            self.cluster.mark_ok(route.key)
            await self._async_refresh_device(route.client, stats)
            await self._async_refresh_health(route.client, stats)
            return NodeData(
                stats=stats,
                device=self._device,
                rates=self._rates(stats),
                health=self._health,
            )
        raise UpdateFailed(
            translation_domain=DOMAIN,
            translation_key="node_unreachable",
            translation_placeholders={"error": str(last_error or "no route")},
        )

    async def _async_refresh_device(self, client: DnscoreClient, stats: Stats) -> None:
        """Read `GET /api/device` at first, now and then, and after an update."""
        now = _now()
        stale = (
            self._device is None
            or self._device_at is None
            or now - self._device_at >= DEVICE_REFRESH_INTERVAL.total_seconds()
            or stats.firmware_version != self._device.version
        )
        if not stale:
            return
        try:
            device = await client.get_device()
        except DnscoreError as err:
            LOGGER.debug("Node %s: could not read the device: %s", self.node_id, err)
            return
        self._device_at = now
        changed = self._device is None or self._device.raw != device.raw
        self._device = device
        if changed:
            self._async_update_device_registry()

    async def _async_refresh_health(self, client: DnscoreClient, stats: Stats) -> None:
        """Read `GET /api/health`, unless this version of the node refused it."""
        if self._health_refused_on == stats.firmware_version:
            return
        try:
            self._health = await client.get_health()
        except DnscoreForbiddenError:
            # Before 0.95.0 the route is the admin's: no health, and no asking
            # again on every refresh.
            self._health = None
            self._health_refused_on = stats.firmware_version
        except DnscoreError as err:
            LOGGER.debug("Node %s: could not read its health: %s", self.node_id, err)
        else:
            self._health_refused_on = None

    def _rates(self, stats: Stats) -> Rates:
        """Per-second rates since the last reading; none after a restart."""
        now = _now()
        previous, self._previous = self._previous, (now, stats)
        if previous is None:
            return Rates()
        then, old = previous
        elapsed = now - then
        if (
            elapsed <= 0
            or stats.uptime_seconds < old.uptime_seconds
            or stats.queries_total < old.queries_total
            or stats.queries_blocked < old.queries_blocked
        ):
            return Rates()  # the node started again: its counters did too
        return Rates(
            queries=(stats.queries_total - old.queries_total) / elapsed,
            blocked=(stats.queries_blocked - old.queries_blocked) / elapsed,
        )

    @callback
    def _async_update_device_registry(self) -> None:
        """Put the model and versions from `GET /api/device` on the node's device."""
        device_registry = dr.async_get(self.hass)
        device = device_registry.async_get_device_by_identifier(
            (DOMAIN, node_identifier(self.config_entry, self.node_id)),
            self.config_entry.entry_id,
        )
        if device is None or self._device is None:
            return
        device_registry.async_update_device(
            device.id, **node_device_details(self._device)
        )


type NodeListener = Callable[[list[DnscoreNodeCoordinator]], None]


class DnscoreNodeManager:
    """Keeps one node coordinator per member, as the member list changes."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: DnscoreConfigEntry,
        cluster: DnscoreClusterCoordinator,
    ) -> None:
        """Follow the members `cluster` reports."""
        self.hass = hass
        self.entry = entry
        self.cluster = cluster
        self.nodes: dict[str, DnscoreNodeCoordinator] = {}
        self._node_listeners: list[NodeListener] = []
        self._remove_listeners: list[Callable[[str], None]] = []
        self._update_listeners: list[CALLBACK_TYPE] = []
        self._node_unsubs: dict[str, CALLBACK_TYPE] = {}
        self._missing_since: dict[str, float] = {}
        self._unsub_cluster: CALLBACK_TYPE | None = None

    async def async_setup(self) -> None:
        """Make the first coordinators, read each node once, and start following."""
        if self.cluster.data is not None:
            created = [
                self._create(node_id) for node_id in self.cluster.data.node_ids()
            ]
            await asyncio.gather(*(c.async_refresh() for c in created))
        self._unsub_cluster = self.cluster.async_add_listener(
            self._async_cluster_updated
        )

    async def async_shutdown(self) -> None:
        """Stop every node coordinator."""
        if self._unsub_cluster is not None:
            self._unsub_cluster()
            self._unsub_cluster = None
        for node_id in list(self.nodes):
            await self._async_drop(node_id)

    def _create(self, node_id: str) -> DnscoreNodeCoordinator:
        coordinator = DnscoreNodeCoordinator(
            self.hass, self.entry, self.cluster, node_id
        )
        self.nodes[node_id] = coordinator
        self._node_unsubs[node_id] = coordinator.async_add_listener(
            self._async_node_updated
        )
        return coordinator

    async def _async_drop(self, node_id: str) -> None:
        if (unsub := self._node_unsubs.pop(node_id, None)) is not None:
            unsub()
        if (coordinator := self.nodes.pop(node_id, None)) is not None:
            await coordinator.async_shutdown()

    @callback
    def async_add_node_listener(self, listener: NodeListener) -> CALLBACK_TYPE:
        """Call `listener` with the coordinators of members that joined."""
        self._node_listeners.append(listener)
        return lambda: self._node_listeners.remove(listener)

    @callback
    def async_add_remove_listener(
        self, listener: Callable[[str], None]
    ) -> CALLBACK_TYPE:
        """Call `listener` with the node ID of a member whose device was removed."""
        self._remove_listeners.append(listener)
        return lambda: self._remove_listeners.remove(listener)

    @callback
    def async_add_update_listener(self, listener: CALLBACK_TYPE) -> CALLBACK_TYPE:
        """Call `listener` whenever any node coordinator refreshed (for totals)."""
        self._update_listeners.append(listener)
        return lambda: self._update_listeners.remove(listener)

    @callback
    def _async_node_updated(self) -> None:
        for listener in list(self._update_listeners):
            listener()

    @callback
    def _async_cluster_updated(self) -> None:
        """Add the members that joined; drop those gone for long enough."""
        data = self.cluster.data
        if data is None or not self.cluster.last_update_success:
            return
        current = set(data.node_ids())
        new = [node_id for node_id in current if node_id not in self.nodes]
        if new:
            created = [self._create(node_id) for node_id in new]
            self.entry.async_create_task(
                self.hass, self._async_announce(created), "dnscore new members"
            )
        now = _now()
        for node_id in current:
            self._missing_since.pop(node_id, None)
        for node_id in list(self.nodes):
            if node_id in current:
                continue
            since = self._missing_since.setdefault(node_id, now)
            if now - since >= NODE_REMOVE_AFTER.total_seconds():
                self._missing_since.pop(node_id, None)
                self.entry.async_create_task(
                    self.hass, self._async_remove(node_id), "dnscore member gone"
                )

    async def _async_announce(self, created: list[DnscoreNodeCoordinator]) -> None:
        for coordinator in created:
            await coordinator.async_refresh()
        LOGGER.debug("New members: %s", [c.node_id for c in created])
        for listener in list(self._node_listeners):
            listener(created)

    async def _async_remove(self, node_id: str) -> None:
        """Remove a departed member's coordinator and device."""
        LOGGER.info("Member %s left the cluster: removing its device", node_id)
        await self._async_drop(node_id)
        device_registry = dr.async_get(self.hass)
        device = device_registry.async_get_device_by_identifier(
            (DOMAIN, node_identifier(self.entry, node_id)), self.entry.entry_id
        )
        if device is not None:
            device_registry.async_remove_device(device.id)
        for listener in list(self._remove_listeners):
            listener(node_id)


def debug_snapshot(coordinator: DnscoreNodeCoordinator) -> dict[str, Any]:
    """The raw answers a node coordinator holds (diagnostics redact them)."""
    data: NodeData | None = coordinator.data  # None before the first refresh
    if data is None:
        return {"available": False}
    return {
        "available": coordinator.last_update_success,
        "stats": data.stats.raw,
        "device": data.device.raw if data.device is not None else None,
        "health": data.health.raw if data.health is not None else None,
    }
