"""Typed views of the DNS Core admin API's answers (API version 1).

Each model is built with `from_dict` from the JSON a route returns, as the
firmware's JSON writers produce it (`web_api.cpp` in the DNS Core
repository). The API only ever adds fields, so parsing ignores members it
does not know and tolerates missing ones: a member written "only when ..."
becomes None. Every model keeps the answer it was built from as `raw`, for
diagnostics.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

# A board's wall clock is unset until NTP (or its RTC) answers; DNS Core
# reports such times as 0, and treats anything before this as unset.
_CLOCK_FLOOR = 1_700_000_000

# A member that is not in a cluster reports this member ID.
_NO_MEMBER_ID = "0" * 16


def _obj(data: Any) -> dict[str, Any]:
    return data if isinstance(data, dict) else {}


def _int(data: dict[str, Any], key: str, default: int = 0) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value


def _opt_int(data: dict[str, Any], key: str) -> int | None:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _num(data: dict[str, Any], key: str, default: float = 0.0) -> float:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


def _opt_num(data: dict[str, Any], key: str) -> float | None:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _str(data: dict[str, Any], key: str, default: str = "") -> str:
    value = data.get(key)
    return value if isinstance(value, str) else default


def _opt_str(data: dict[str, Any], key: str) -> str | None:
    """A string member; an empty one counts as absent."""
    value = data.get(key)
    return value if isinstance(value, str) and value else None


def _bool(data: dict[str, Any], key: str, default: bool = False) -> bool:
    value = data.get(key)
    return value if isinstance(value, bool) else default


def _opt_bool(data: dict[str, Any], key: str) -> bool | None:
    value = data.get(key)
    return value if isinstance(value, bool) else None


def _objects(data: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = data.get(key)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _strings(data: dict[str, Any], key: str) -> tuple[str, ...]:
    value = data.get(key)
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def epoch_to_datetime(value: int | float | None) -> datetime | None:
    """A Unix time as an aware datetime; None for 0, unset clocks and junk."""
    if value is None or value < _CLOCK_FLOOR:
        return None
    try:
        return datetime.fromtimestamp(value, tz=UTC)
    except OverflowError, OSError, ValueError:
        return None


def _utc_text(value: str | None) -> datetime | None:
    """`2026-06-27 02:51:47` (UTC, as the certificate status writes it)."""
    if not value:
        return None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")  # noqa: DTZ007
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC)


# ---- GET /api/auth/status (open) --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AuthStatus:
    """What a node says before anyone signs in (an open route, no credentials)."""

    enabled: bool
    authenticated: bool
    claimed: bool
    claim_window_s: int | None
    raw: dict[str, Any] = field(repr=False, compare=False)

    @property
    def setup_needed(self) -> bool:
        """A fresh node that nobody has claimed yet (no OTA password)."""
        return not self.claimed

    @classmethod
    def from_dict(cls, data: Any) -> AuthStatus:
        """Build from the route's JSON."""
        d = _obj(data)
        return cls(
            enabled=_bool(d, "enabled"),
            authenticated=_bool(d, "authenticated"),
            claimed=_bool(d, "claimed", default=True),
            claim_window_s=_opt_int(d, "claim_window_s"),
            raw=d,
        )


# ---- GET /api/config (the parts a client needs) -----------------------------------------


@dataclass(frozen=True, slots=True)
class ServerConfig:
    """The settings that identify a node and its cluster, and blocking.

    A token's `GET /api/config` carries only the keys of the areas it
    reads: `tls` and `host_name` are the system area's, the blocking keys
    the filtering area's.
    """

    generation: int
    server_name: str
    host_name: str
    port_https: int
    blocking_enabled: bool | None
    blocking_resume_at: int | None
    raw: dict[str, Any] = field(repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: Any) -> ServerConfig:
        """Build from the route's JSON."""
        d = _obj(data)
        tls = _obj(d.get("tls"))
        return cls(
            generation=_int(d, "config_generation"),
            server_name=_str(tls, "server_name").strip().rstrip(".").lower(),
            host_name=_str(d, "host_name").strip().rstrip(".").lower(),
            port_https=_int(tls, "port_https", 443) or 443,
            blocking_enabled=_opt_bool(d, "blocking_enabled"),
            blocking_resume_at=_opt_int(d, "blocking_resume_at"),
            raw=d,
        )


# ---- GET /api/stats ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LatencyStats:
    """One kind of answer's latency, in microseconds."""

    count: int
    p50_us: int
    p90_us: int
    p99_us: int

    @classmethod
    def from_dict(cls, data: Any) -> LatencyStats:
        """Build from one member of `latency`."""
        d = _obj(data)
        return cls(
            count=_int(d, "count"),
            p50_us=_int(d, "p50_us"),
            p90_us=_int(d, "p90_us"),
            p99_us=_int(d, "p99_us"),
        )


@dataclass(frozen=True, slots=True)
class CacheStats:
    """The answer cache."""

    enabled: bool
    entries: int
    capacity: int
    hits: int
    misses: int
    hit_rate: float | None

    @classmethod
    def from_dict(cls, data: Any) -> CacheStats:
        """Build from `cache`."""
        d = _obj(data)
        return cls(
            enabled=_bool(d, "enabled"),
            entries=_int(d, "entries"),
            capacity=_int(d, "capacity"),
            hits=_int(d, "hits"),
            misses=_int(d, "misses"),
            hit_rate=_opt_num(d, "hit_rate"),
        )


@dataclass(frozen=True, slots=True)
class UpstreamHealth:
    """One upstream resolver as this node sees it."""

    spec: str
    avg_ms: float
    queries: int
    failures: int
    consec_failures: int
    down_s: int
    last_error: str

    @property
    def failing(self) -> bool:
        """The upstream is failing now: down, or its last queries failed."""
        return self.down_s > 0 or self.consec_failures > 0

    @classmethod
    def from_dict(cls, data: Any) -> UpstreamHealth:
        """Build from one member of `upstream_health`."""
        d = _obj(data)
        return cls(
            spec=_str(d, "spec"),
            avg_ms=_num(d, "avg_ms"),
            queries=_int(d, "queries"),
            failures=_int(d, "failures"),
            consec_failures=_int(d, "consec_failures"),
            down_s=_int(d, "down_s"),
            last_error=_str(d, "last_error"),
        )


@dataclass(frozen=True, slots=True)
class Stats:
    """A node's counters since it started, and its state now."""

    node_id: str
    firmware_version: str
    uptime_seconds: int
    time: int
    is_leader: bool
    cluster_leader: str
    blocking_in_effect: bool
    blocking_paused_until: int | None
    queries_total: int
    queries_blocked: int
    queries_cached: int
    queries_forwarded: int
    queries_rewritten: int
    block_percentage: float
    domains_in_filter: int
    rules_loaded: int
    filter_version_hash: str
    avg_processing_time_ms: float
    upstream_timeouts: int
    servfail_sent: int
    rate_limited: int
    memory_free_bytes: int | None
    dma_free_bytes: int | None
    dma_largest_block: int | None
    psram_free_bytes: int | None
    cache: CacheStats
    latency: dict[str, LatencyStats]
    upstreams: tuple[UpstreamHealth, ...]
    raw: dict[str, Any] = field(repr=False, compare=False)

    @property
    def clock(self) -> datetime | None:
        """The node's wall clock when it answered (None while unset)."""
        return epoch_to_datetime(self.time)

    @classmethod
    def from_dict(cls, data: Any) -> Stats:
        """Build from the route's JSON."""
        d = _obj(data)
        return cls(
            node_id=_str(d, "node_id"),
            firmware_version=_str(d, "firmware_version"),
            uptime_seconds=_int(d, "uptime_seconds"),
            time=_int(d, "time"),
            is_leader=_bool(d, "is_leader"),
            cluster_leader=_str(d, "cluster_leader"),
            blocking_in_effect=_bool(d, "blocking_in_effect", default=True),
            blocking_paused_until=_opt_int(d, "blocking_paused_until"),
            queries_total=_int(d, "dns_queries_total"),
            queries_blocked=_int(d, "dns_queries_blocked"),
            queries_cached=_int(d, "dns_queries_cached"),
            queries_forwarded=_int(d, "dns_queries_forwarded"),
            queries_rewritten=_int(d, "dns_queries_rewritten"),
            block_percentage=_num(d, "block_percentage"),
            domains_in_filter=_int(d, "domains_in_filter"),
            rules_loaded=_int(d, "rules_loaded"),
            filter_version_hash=_str(d, "filter_version_hash"),
            avg_processing_time_ms=_num(d, "avg_processing_time_ms"),
            upstream_timeouts=_int(d, "upstream_timeouts"),
            servfail_sent=_int(d, "servfail_sent"),
            rate_limited=_int(d, "rate_limited"),
            memory_free_bytes=_opt_int(d, "memory_free_bytes"),
            dma_free_bytes=_opt_int(d, "dma_free_bytes"),
            dma_largest_block=_opt_int(d, "dma_largest_block"),
            psram_free_bytes=_opt_int(d, "psram_free_bytes"),
            cache=CacheStats.from_dict(d.get("cache")),
            latency={
                kind: LatencyStats.from_dict(value)
                for kind, value in _obj(d.get("latency")).items()
                if isinstance(value, dict)
            },
            upstreams=tuple(
                UpstreamHealth.from_dict(u) for u in _objects(d, "upstream_health")
            ),
            raw=d,
        )


# ---- GET /api/device --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChipInfo:
    """The chip: `ESP32-P4` and its silicon revision on a board."""

    model: str
    revision: str
    cores: int
    cpu_mhz: int
    image: str
    mac: str

    @classmethod
    def from_dict(cls, data: Any) -> ChipInfo:
        """Build from `chip`."""
        d = _obj(data)
        return cls(
            model=_str(d, "model"),
            revision=_str(d, "revision"),
            cores=_int(d, "cores"),
            cpu_mhz=_int(d, "cpu_mhz"),
            image=_str(d, "image"),
            mac=_str(d, "mac").lower(),
        )


@dataclass(frozen=True, slots=True)
class DmaLow:
    """The lowest free DMA memory since boot, and what was running then."""

    free: int
    largest_then: int
    uptime_s: int
    during: str

    @classmethod
    def from_dict(cls, data: Any) -> DmaLow | None:
        """Build from `memory.dma_low` (absent before the first low point)."""
        if not isinstance(data, dict):
            return None
        return cls(
            free=_int(data, "free"),
            largest_then=_int(data, "largest_then"),
            uptime_s=_int(data, "uptime_s"),
            during=_str(data, "during"),
        )


@dataclass(frozen=True, slots=True)
class MemoryInfo:
    """A board's memory (meaningless on the Linux build: the host's)."""

    internal_free: int | None
    internal_min_free: int | None
    dma_free: int | None
    dma_min_free: int | None
    dma_largest: int | None
    psram_free: int | None
    dma_low: DmaLow | None

    @classmethod
    def from_dict(cls, data: Any) -> MemoryInfo:
        """Build from `memory`."""
        d = _obj(data)
        return cls(
            internal_free=_opt_int(d, "internal_free"),
            internal_min_free=_opt_int(d, "internal_min_free"),
            dma_free=_opt_int(d, "dma_free"),
            dma_min_free=_opt_int(d, "dma_min_free"),
            dma_largest=_opt_int(d, "dma_largest"),
            psram_free=_opt_int(d, "psram_free"),
            dma_low=DmaLow.from_dict(d.get("dma_low")),
        )


@dataclass(frozen=True, slots=True)
class FirmwareInfo:
    """What the node runs."""

    version: str
    project: str
    build: str
    idf: str
    image_valid: bool | None

    @classmethod
    def from_dict(cls, data: Any) -> FirmwareInfo:
        """Build from `firmware`."""
        d = _obj(data)
        return cls(
            version=_str(d, "version"),
            project=_str(d, "project"),
            build=_str(d, "build"),
            idf=_str(d, "idf"),
            image_valid=_opt_bool(d, "image_valid"),
        )


@dataclass(frozen=True, slots=True)
class Device:
    """What the node is and runs."""

    node_id: str
    is_leader: bool
    chip: ChipInfo
    firmware: FirmwareInfo
    memory: MemoryInfo
    network_active: str
    reset_reason: int | None
    raw: dict[str, Any] = field(repr=False, compare=False)

    @property
    def version(self) -> str:
        """The firmware version."""
        return self.firmware.version

    @property
    def mac(self) -> str:
        """The MAC address (on the Linux build, the host interface's)."""
        return self.chip.mac

    @property
    def is_board(self) -> bool:
        """An ESP32 board, not the Linux build (which says `idf: linux`)."""
        if self.firmware.idf == "linux" or self.firmware.project.endswith("-linux"):
            return False
        return self.chip.model.upper().startswith("ESP32")

    @classmethod
    def from_dict(cls, data: Any) -> Device:
        """Build from the route's JSON."""
        d = _obj(data)
        node = _obj(d.get("node"))
        return cls(
            node_id=_str(node, "id"),
            is_leader=_bool(node, "is_leader"),
            chip=ChipInfo.from_dict(d.get("chip")),
            firmware=FirmwareInfo.from_dict(d.get("firmware")),
            memory=MemoryInfo.from_dict(d.get("memory")),
            network_active=_str(_obj(d.get("network")), "active"),
            reset_reason=_opt_int(_obj(d.get("load")), "reset_reason"),
            raw=d,
        )


# ---- GET /api/cluster/status ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MemberCounters:
    """A member's query counters, as its heartbeats report them."""

    queries: int
    blocked: int
    cached: int
    forwarded: int

    @classmethod
    def from_dict(cls, data: Any) -> MemberCounters | None:
        """Build from a member's `counters` (absent from older members)."""
        if not isinstance(data, dict):
            return None
        return cls(
            queries=_int(data, "queries"),
            blocked=_int(data, "blocked"),
            cached=_int(data, "cached"),
            forwarded=_int(data, "forwarded"),
        )


@dataclass(frozen=True, slots=True)
class ClusterMember:
    """One member as the answering node hears it (itself first)."""

    node_id: str
    ip: str
    status: str
    member_id: str | None
    host_name: str | None
    firmware_version: str
    uptime_seconds: int
    serving: bool
    stands: bool
    trusted: bool
    config_state: str
    config_generation: int
    filter_version_hash: str
    last_seen_ms_ago: int
    services: dict[str, str]
    counters: MemberCounters | None
    certificate_fingerprint: str | None
    is_self: bool
    raw: dict[str, Any] = field(repr=False, compare=False)

    @property
    def label(self) -> str:
        """The member's DNS name, else its node ID."""
        return self.host_name or self.node_id

    @property
    def online(self) -> bool:
        """The member has a network (only the answering node can say otherwise)."""
        return self.status == "online"

    @classmethod
    def from_dict(cls, data: Any, *, is_self: bool = False) -> ClusterMember:
        """Build from one member of `active_nodes`."""
        d = _obj(data)
        member_id = _opt_str(d, "member_id")
        host_name = _opt_str(d, "host_name")
        return cls(
            node_id=_str(d, "id"),
            ip=_str(d, "ip"),
            status=_str(d, "status"),
            member_id=None if member_id in (None, _NO_MEMBER_ID) else member_id,
            host_name=host_name.strip().rstrip(".").lower() if host_name else None,
            firmware_version=_str(d, "firmware_version"),
            uptime_seconds=_int(d, "uptime_seconds"),
            serving=_bool(d, "serving", default=True),
            stands=_bool(d, "stands", default=True),
            trusted=_bool(d, "trusted", default=True),
            config_state=_str(d, "config_state"),
            config_generation=_int(d, "config_generation"),
            filter_version_hash=_str(d, "filter_version_hash"),
            last_seen_ms_ago=_int(d, "last_seen_ms_ago"),
            services={
                k: v for k, v in _obj(d.get("services")).items() if isinstance(v, str)
            },
            counters=MemberCounters.from_dict(d.get("counters")),
            certificate_fingerprint=_opt_str(_obj(d.get("certificate")), "fingerprint"),
            is_self=is_self,
            raw=d,
        )


@dataclass(frozen=True, slots=True)
class ClusterStatus:
    """The cluster as the answering node sees it."""

    leader: str
    term: int
    quorum_of: int
    config_generation: int
    cluster_key_id: str
    isolated: bool
    syncing: bool
    members: tuple[ClusterMember, ...]
    raw: dict[str, Any] = field(repr=False, compare=False)

    @property
    def self_member(self) -> ClusterMember | None:
        """The answering node's own entry (DNS Core lists it first)."""
        for member in self.members:
            if member.is_self:
                return member
        return None

    @property
    def enabled(self) -> bool:
        """The answering node runs a cluster (a lone node has no node ID in it)."""
        me = self.self_member
        return me is not None and bool(me.node_id)

    @property
    def node_id(self) -> str:
        """The answering node's ID ("" without a cluster)."""
        me = self.self_member
        return me.node_id if me is not None else ""

    def member(self, node_id: str) -> ClusterMember | None:
        """A member by node ID."""
        for member in self.members:
            if member.node_id == node_id:
                return member
        return None

    def is_leader(self, node_id: str) -> bool:
        """The member is the leader."""
        return bool(node_id) and node_id == self.leader

    @classmethod
    def from_dict(cls, data: Any) -> ClusterStatus:
        """Build from the route's JSON."""
        d = _obj(data)
        members: list[ClusterMember] = []
        for index, item in enumerate(_objects(d, "active_nodes")):
            state = _str(item, "config_state")
            is_self = index == 0 and state in ("self", "syncing")
            members.append(ClusterMember.from_dict(item, is_self=is_self))
        return cls(
            leader=_str(d, "leader"),
            term=_int(d, "term"),
            quorum_of=_int(d, "quorum_of"),
            config_generation=_int(d, "config_generation"),
            cluster_key_id=_str(d, "cluster_key_id"),
            isolated=_bool(d, "isolated"),
            syncing=_bool(d, "syncing"),
            members=tuple(members),
            raw=d,
        )


# ---- GET /api/tls/status and /api/acme --------------------------------------------------


@dataclass(frozen=True, slots=True)
class TlsStatus:
    """The certificate a node serves (every member serves the cluster's)."""

    ready: bool
    expired: bool
    clock_unset: bool
    subject: str
    issuer: str
    hostnames: tuple[str, ...]
    not_before: datetime | None
    not_after: datetime | None
    warning: str
    raw: dict[str, Any] = field(repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: Any) -> TlsStatus:
        """Build from the route's JSON."""
        d = _obj(data)
        names = _str(d, "hostnames")
        return cls(
            ready=_bool(d, "ready"),
            expired=_bool(d, "expired"),
            clock_unset=_bool(d, "clock_unset"),
            subject=_str(d, "subject"),
            issuer=_str(d, "issuer"),
            hostnames=tuple(n.strip() for n in names.split(",") if n.strip()),
            not_before=_utc_text(_opt_str(d, "not_before")),
            not_after=_utc_text(_opt_str(d, "not_after")),
            warning=_str(d, "warning"),
            raw=d,
        )


@dataclass(frozen=True, slots=True)
class AcmeStatus:
    """The certificate's renewal (Let's Encrypt over DNS-01, run by the leader)."""

    enabled: bool
    state: str
    step: str
    error: str
    last_success: datetime | None
    last_failure: datetime | None
    consecutive_failures: int
    days_until_expiry: int | None
    name_missing: bool
    raw: dict[str, Any] = field(repr=False, compare=False)

    @property
    def failing(self) -> bool:
        """The last renewal attempt failed."""
        return self.state == "failed"

    @classmethod
    def from_dict(cls, data: Any) -> AcmeStatus:
        """Build from the route's JSON."""
        d = _obj(data)
        days = _opt_int(d, "days_until_expiry")
        return cls(
            enabled=_bool(d, "enabled"),
            state=_str(d, "state", "idle"),
            step=_str(d, "step"),
            error=_str(d, "error"),
            last_success=epoch_to_datetime(_opt_int(d, "last_success")),
            last_failure=epoch_to_datetime(_opt_int(d, "last_failure")),
            consecutive_failures=_int(d, "consecutive_failures"),
            days_until_expiry=None if days is None or days < 0 else days,
            name_missing=_bool(d, "name_missing"),
            raw=d,
        )


# ---- GET /api/blocklist/update ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BlocklistStatus:
    """The blocklist a node runs, and (on the leader) its downloads."""

    is_leader: bool
    busy: bool
    stage: str
    refresh_hours: int
    loaded: bool
    domains: int
    crc: str
    built: datetime | None
    last_attempt: datetime | None
    last_success: datetime | None
    last_change: datetime | None
    last_ok: bool | None
    last_result: str
    raw: dict[str, Any] = field(repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: Any) -> BlocklistStatus:
        """Build from the route's JSON."""
        d = _obj(data)
        running = _obj(d.get("running"))
        return cls(
            is_leader=_bool(d, "is_leader"),
            busy=_bool(d, "busy"),
            stage=_str(d, "stage", "idle"),
            refresh_hours=_int(d, "refresh_hours"),
            loaded=_bool(running, "loaded"),
            domains=_int(running, "domains"),
            crc=_str(running, "crc"),
            built=epoch_to_datetime(_opt_int(running, "build_epoch")),
            last_attempt=epoch_to_datetime(_opt_int(d, "last_attempt")),
            last_success=epoch_to_datetime(_opt_int(d, "last_success")),
            last_change=epoch_to_datetime(_opt_int(d, "last_change")),
            last_ok=_opt_bool(d, "last_ok"),
            last_result=_str(d, "last_result"),
            raw=d,
        )


# ---- GET /api/ota -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OtaProgress:
    """An install the board runs itself (`POST /api/update/install`)."""

    state: str
    version: str | None
    message: str | None
    bytes_done: int | None
    total: int | None

    @property
    def active(self) -> bool:
        """An install is under way."""
        return self.state not in ("idle", "failed", "")

    @property
    def percent(self) -> int | None:
        """How far the download is, when the board says."""
        if self.bytes_done is None or not self.total:
            return None
        return max(0, min(100, self.bytes_done * 100 // self.total))

    @classmethod
    def from_dict(cls, data: Any) -> OtaProgress:
        """Build from `update`."""
        d = _obj(data)
        return cls(
            state=_str(d, "state", "idle"),
            version=_opt_str(d, "version"),
            message=_opt_str(d, "message"),
            bytes_done=_opt_int(d, "bytes"),
            total=_opt_int(d, "total"),
        )


@dataclass(frozen=True, slots=True)
class OtaState:
    """The running image, whether it is confirmed, and any install's progress."""

    app_version: str
    confirmed: bool
    last_failure: str
    update: OtaProgress
    raw: dict[str, Any] = field(repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: Any) -> OtaState:
        """Build from the route's JSON."""
        d = _obj(data)
        return cls(
            app_version=_str(d, "app_version"),
            confirmed=_bool(d, "confirmed", default=True),
            last_failure=_str(d, "last_failure"),
            update=OtaProgress.from_dict(d.get("update")),
            raw=d,
        )


# ---- GET /api/health --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HealthCondition:
    """One thing the node judges wrong (`cert_expiring`, `blocklist_stale`, ...)."""

    id: str
    severity: str
    text: str


@dataclass(frozen=True, slots=True)
class Health:
    """The node's own verdict: `ok`, `warn` or `error`, and why."""

    status: str
    conditions: tuple[HealthCondition, ...]
    raw: dict[str, Any] = field(repr=False, compare=False)

    @property
    def problem(self) -> bool:
        """Anything is wrong."""
        return self.status != "ok"

    @classmethod
    def from_dict(cls, data: Any) -> Health:
        """Build from the route's JSON."""
        d = _obj(data)
        return cls(
            status=_str(d, "status", "ok"),
            conditions=tuple(
                HealthCondition(
                    id=_str(c, "id"), severity=_str(c, "severity"), text=_str(c, "text")
                )
                for c in _objects(d, "conditions")
            ),
            raw=d,
        )


# ---- GET /api/agents (the OTA password) -------------------------------------------------


@dataclass(frozen=True, slots=True)
class TokenInfo:
    """An API client: its name, grant and limits (never the token itself)."""

    name: str
    scopes: tuple[str, ...]
    effective_scopes: tuple[str, ...]
    networks: tuple[str, ...]
    expires_at: int
    created_at: int
    raw: dict[str, Any] = field(repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: Any) -> TokenInfo:
        """Build from one member of `agents`, or from a create's `agent`."""
        d = _obj(data)
        return cls(
            name=_str(d, "name"),
            scopes=_strings(d, "scopes"),
            effective_scopes=_strings(d, "effective_scopes"),
            networks=_strings(d, "networks"),
            expires_at=_int(d, "expires_at"),
            created_at=_int(d, "created_at"),
            raw=d,
        )
