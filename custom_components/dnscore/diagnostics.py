"""Diagnostics for DNS Core, with every token, address and name redacted."""

from __future__ import annotations

import ipaddress
from typing import Any

from yarl import URL

from homeassistant.components.diagnostics import REDACTED, async_redact_data
from homeassistant.const import CONF_HOST, CONF_TOKEN
from homeassistant.core import HomeAssistant

from . import DnscoreConfigEntry
from .const import CONF_KNOWN_URLS, CONF_TOKEN_NAME
from .coordinator import debug_snapshot

# Tokens and secrets; addresses, DNS names and URLs; the MAC; upstreams (a
# DoH URL can carry a personal path) and the config's lists, which name the
# site's hosts.
TO_REDACT = {
    CONF_TOKEN,
    CONF_TOKEN_NAME,
    CONF_HOST,
    CONF_KNOWN_URLS,
    "password",
    "ota_password",
    "secret",
    "token",
    "api_key",
    "cluster_password",
    # addresses and names
    "ip",
    "leader_ip",
    "address",
    "host",
    "host_name",
    "server_name",
    "url",
    "endpoint",
    "title",
    "unique_id",
    "hostnames",
    "subject",
    "issuer",
    "domain",
    "acme_domain",
    "acme_contact",
    "zone",
    "names",
    "names_left_out",
    "leader_vip",
    "cluster_members",
    "cluster_peers",
    "networks",
    "mac",
    "spec",
    "upstream_dns",
    "fallback_dns",
    "bootstrap_dns",
    "private_dns",
    "custom_rules",
    "custom_whitelist",
    "custom_blacklist",
    "blocklist_url",
    "update_url",
    "unifi_host",
    "unifi_pin",
    "last_error",
    "requested_by",
}


def _names(entry: DnscoreConfigEntry) -> set[str]:
    """Every DNS name and address the entry knows, and the domains of the names."""
    found: set[str] = {str(entry.data.get(CONF_HOST) or ""), entry.title}
    found.add(entry.unique_id or "")
    found.update(URL(url).host or "" for url in entry.data.get(CONF_KNOWN_URLS, []))
    data = entry.runtime_data.cluster.data
    if data is not None:
        found.update((data.config.server_name, data.config.host_name))
        for member in data.cluster.members:
            found.update((member.host_name or "", member.ip, member.node_id))
        if data.shared.tls is not None:
            found.update(data.shared.tls.hostnames)
    domains = set()
    for name in found:
        try:
            ipaddress.ip_address(name)
        except ValueError:
            if name.count(".") >= 2:
                domains.add(name.split(".", 1)[1])
    return {n for n in found | domains if len(n) >= 4}


def _scrub(value: Any, names: set[str]) -> Any:
    """Replace the known names inside strings that key redaction let through."""
    if isinstance(value, dict):
        return {k: _scrub(v, names) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v, names) for v in value]
    if isinstance(value, str) and value != REDACTED:
        for name in sorted(names, key=len, reverse=True):
            value = value.replace(name, REDACTED)
    return value


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: DnscoreConfigEntry
) -> dict[str, Any]:
    """Return what the entry knows, redacted."""
    runtime = entry.runtime_data
    cluster = runtime.cluster
    data = cluster.data
    shared = data.shared if data is not None else None
    redacted = async_redact_data(
        {
            "entry": {
                "title": entry.title,
                "unique_id": entry.unique_id,
                "data": dict(entry.data),
            },
            "cluster": {
                "available": cluster.last_update_success,
                "endpoint": cluster.active.key if cluster.active else None,
                "cluster": data.cluster.raw if data is not None else None,
                "config": data.config.raw if data is not None else None,
                "tls": shared.tls.raw if shared and shared.tls else None,
                "acme": shared.acme.raw if shared and shared.acme else None,
                "blocklist": shared.blocklist.raw
                if shared and shared.blocklist
                else None,
            },
            "nodes": {
                f"node_{index}": debug_snapshot(node)
                for index, node in enumerate(runtime.nodes.nodes.values())
            },
        },
        TO_REDACT,
    )
    return _scrub(redacted, _names(entry))  # type: ignore[no-any-return]
