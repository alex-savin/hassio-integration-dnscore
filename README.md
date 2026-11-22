# DNS Core for Home Assistant

A custom Home Assistant integration that monitors and controls a DNS Core
DNS server cluster over its HTTPS admin API. It covers:

- members and leader;
- queries, blocked queries and the blocked ratio;
- the cache and latency;
- the shared blocklist and certificate;
- each node's memory and upstreams.

It can also turn blocking off, on, or off for a while.

- Home Assistant 2026.11 or later (Python 3.14).
- DNS Core 0.94 or later (API version 1). Boards (ESP32-P4), the Linux
  node and the Home Assistant app are all supported.
- Status: **phase 2 of the integration plan** (monitoring, blocking, the
  blocklist and the cache), plus phase 1's consumers (the hassio step and
  the Health sensor, which need DNS Core 0.95.0), version 0.1.0. The plan is
  `docs/hassio-integration-plan.md` in the DNS Core repository. See
  [What comes next](#what-comes-next).

## Install

The repository is private while DNS Core is. By hand: copy
`custom_components/dnscore` into your configuration directory's
`custom_components/` and restart Home Assistant. Once it is published, add
it to the Marketplace as a custom repository of type **Integration**.

## Set up

Add **DNS Core** under Settings → Devices & services, and enter one node.

1. **Host and port.** Use any member's DNS name, the cluster's name, or an
   IP address, and its HTTPS port (443). Home Assistant learns the other
   members from `GET /api/cluster/status`.
   - HTTPS and certificate verification are on by default.
   - Turn verification off only for a self-signed certificate.
   - With verification on, use the name on the certificate, not an IP
     address.
2. **OTA password, once.** Home Assistant uses it to mint its own API token
   (`POST /api/agents`) and forgets it: the OTA password is never stored.
   - **The token's grant:** `status:read`, `system:read`, `filtering:write`
     and `resolution:write`. It reads the cluster, turns blocking on and
     off, refreshes the blocklist and flushes caches. It cannot restart,
     update or change credentials.
   - It never expires, and it is named `home-assistant-<8 hex digits of
     this Home Assistant's instance ID>`.
   - **Limit the token to** is filled in with the address Home Assistant
     reaches the node from. Leave it, or empty it to let the token work
     from anywhere.

There is one entry per cluster: every member answers with the same name
(`tls.server_name`), so adding a second member is refused as already
configured.

If the token stops working (revoked, or the address limit no longer
matches), Home Assistant asks for the OTA password again and mints a new
token under the same name. Removing the integration does not revoke the
token; revoke it in the DNS Core console (API clients).

**Discovery (DNS Core 0.95.0).** The DNS Core app announces itself to the
Supervisor. Home Assistant then offers **DNS Core** under Settings →
Devices & services with the app's node filled in. Confirm it, and the flow
goes on to the OTA password. A cluster that is already set up is recognised
by its name. The boards announce nothing, so without the app the node is
entered by hand.

## What you get

There is one device for the cluster and one per node, and each node's
device points to the cluster's.
- A member that joins gets its device and entities.
- A member the cluster has not listed for 30 minutes loses them.

**How nodes are read.** Each node is read directly, because DNS Core's
relay is closed to tokens:
- first at `https://<host_name>/`;
- then at the address the cluster reports for it, when Home Assistant's
  resolver does not know the name. The certificate is still checked
  against the name.
- A member with no DNS name is read at its address under the cluster's
  name, which every member's certificate carries.

The cluster itself is read from the configured node first, then from any
other member.

### The cluster device

| Entity | Type | Source |
|---|---|---|
| Members online | sensor | `GET /api/cluster/status` `active_nodes` |
| Leader | sensor | `leader` (the member's `host_name`, else its ID) |
| Election term (diagnostic, disabled) | sensor | `term` |
| Queries per second, Blocked per second | sensor | the nodes' counters, per second between two readings |
| Blocked ratio | sensor, % | blocked over queries, summed over the nodes |
| Blocking | switch | `GET /api/config` `blocking_enabled`; a pause shows as off, with `resumes_at` |
| Blocking resumes | sensor, timestamp | `blocking_resume_at` while paused |
| Domains on the blocklist | sensor | the leader's `GET /api/blocklist/update` `running.domains` |
| Blocklist built | sensor, timestamp | `running.build_epoch` |
| Blocklist checked (diagnostic) | sensor, timestamp | the leader's `last_success` |
| Certificate expiry | sensor, timestamp | `GET /api/tls/status` `not_after` |
| Certificate renewal | sensor, enum | the leader's `GET /api/acme` `state` |
| Last certificate renewal (diagnostic) | sensor, timestamp | `last_success` |
| Certificate | binary sensor, problem | expired, not ready, renewal failed, or a name left out |
| Blocklist | binary sensor, problem | not loaded, or the leader's last download failed |
| Shared settings (diagnostic) | binary sensor, problem | a member's `config_state` is `diverged` or `out-of-sync` |
| Refresh blocklist | button | `POST /api/blocklist/update` on the leader |

The certificate, its renewal and the blocklist are the same on every
member. They are read from the leader every five minutes, and again right
after the refresh button.

### Each node's device

The model is "ESP32-P4 board" (with the chip as model ID and its silicon
revision as hardware version) or "Linux node". The software version and
MAC come from `GET /api/device`.

| Entity | Type | Source |
|---|---|---|
| Online | binary sensor, connectivity | listed by the cluster (stays available while the node itself does not answer) |
| Leader | binary sensor | the cluster's `leader` |
| Serving (diagnostic) | binary sensor | the member's `serving` |
| Settings (diagnostic) | sensor, enum | the member's `config_state` |
| Queries, Queries blocked | sensor, total increasing | `GET /api/stats` `dns_queries_total`, `dns_queries_blocked` |
| Queries cached, Queries forwarded (diagnostic) | sensor, total increasing | `dns_queries_cached`, `dns_queries_forwarded` |
| Queries per second | sensor | per second between two readings |
| Blocked ratio | sensor, % | blocked over queries since the node started |
| Average processing time | sensor, ms | `avg_processing_time_ms` |
| Forwarded latency (p90, diagnostic) | sensor, ms | `latency.forwarded.p90_us` |
| Cache hit rate | sensor, % | `cache.hit_rate` |
| Cache entries, Upstream timeouts, SERVFAIL answers (diagnostic) | sensor | `cache.entries`, `upstream_timeouts`, `servfail_sent` |
| Upstreams | binary sensor, problem | any `upstream_health[]` down or failing |
| Health | binary sensor, problem | `GET /api/health` `status` not `ok`, with its `conditions`; DNS Core 0.95.0 (unavailable before, and not asked again until the node's version changes) |
| Blocking in effect (diagnostic) | binary sensor | `blocking_in_effect` |
| Started, Version (diagnostic) | sensor | `uptime_seconds`, `firmware_version` |
| Internal memory, DMA memory, DMA largest block, PSRAM free (diagnostic, boards) | sensor, data size | `memory_free_bytes`, `dma_free_bytes`, `dma_largest_block`, `psram_free_bytes` |
| DMA low point (diagnostic, boards, disabled) | sensor, data size | `GET /api/device` `memory.dma_low.free` |
| Flush cache | button | `POST /api/cache/flush` on that node |

Rates are unknown for one reading after a node restarts, because its
counters start again from zero.

### Actions

`dnscore.pause_blocking` turns blocking off on the whole cluster for 1
minute to 24 hours (`duration`). The nodes turn it back on by themselves
when the time is up. The switch turns it back on sooner.

### Diagnostics

Download diagnostics from the integration's page. These are redacted:
tokens, the token's name, host names, URLs, addresses, the MAC,
certificate names, upstreams, the blocklist and update URLs, and the
config's rules and member lists. Any known name or address left inside
other text is replaced too.

## Development

```sh
python3.14 -m venv .venv
.venv/bin/pip install -r requirements_test.txt
.venv/bin/ruff format --check . && .venv/bin/ruff check .
.venv/bin/mypy
.venv/bin/pytest
```

The tests use `pytest-homeassistant`, pinned to Home Assistant 2026.11.2.
The canned answers in `tests/fixtures` are a real node's: the Linux build,
run locally with clustering off. Names and addresses are replaced with
documentation ones, and the board-only parts follow `web_api.cpp`'s
writers.

`tests/test_live_node.py` sets the integration up against a running,
throwaway node. It mints and revokes a token, turns blocking off, on and
paused, flushes the cache, and checks reauth after a revoke. It is skipped
unless `DNSCORE_TEST_URL` and `DNSCORE_TEST_OTA_FILE` are set; its
docstring shows how to start the Linux build in a container for it. Never
point it at a member of a real cluster.

The client for the admin API is in `custom_components/dnscore/pydnscore/`.
It has no Home Assistant imports, so it can become the `pydnscore` library
on PyPI unchanged.

CI (`.github/workflows`):
- `validate.yml`: hassfest, and the HACS action (the Marketplace's checks).
- `tests.yml`: ruff, mypy and pytest, on the pinned release and on the
  newest beta.

## What comes next

From the plan (`docs/hassio-integration-plan.md` in DNS Core):

- **Phase 1 is in DNS Core 0.95.0:**
  - `GET /api/health` at `status:read` (the Health sensor);
  - the app's Supervisor discovery (the hassio step);
  - the `maintenance` token area and `GET /api/agents/self`, for phase 3.
- **Phase 3:** an update entity per board, restart and renew-certificate
  buttons, and repairs.
- **Phase 4:** allow and block names, dynamic DNS records, and clients and
  query-log lookups.
- **Phase 5:** a public repository and a Marketplace listing.
- **Brand images** (`custom_components/dnscore/brand/`): DNS Core has no
  logo file yet.

## License

There is no license yet: choosing one is the owner's decision. Apache-2.0
is recommended, as for MQTT Core's integration. Until a `LICENSE` file is
added, all rights are reserved.
