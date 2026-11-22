"""Constants for the DNS Core integration."""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Final

DOMAIN: Final = "dnscore"
LOGGER = logging.getLogger(__package__)

MANUFACTURER: Final = "DNS Core"
DEFAULT_PORT: Final = 443

# Config entry data (the OTA password is never among them).
CONF_TOKEN_NAME: Final = "token_name"
CONF_KNOWN_URLS: Final = "known_urls"

# Flow-only fields.
CONF_OTA_PASSWORD: Final = "ota_password"
CONF_RESTRICT_NETWORK: Final = "restrict_network"
SECTION_CONNECTION: Final = "connection"

# The token the flow mints: monitoring reads status and system (the
# certificate, its renewal and the image state live there); blocking, its
# pause and the blocklist refresh are filtering writes; the cache flush is
# a resolution write. Nothing here restarts, updates or changes credentials
# (system write is lent for 30 days at most, and no token installs).
TOKEN_NAME_PREFIX: Final = "home-assistant"
TOKEN_SCOPES: Final = (
    "status:read",
    "system:read",
    "filtering:write",
    "resolution:write",
)
TOKEN_NETWORKS_MAX: Final = 4

# A token minted through a follower reaches the other members with the
# shared settings, a second or so later: the flow retries the first call.
TOKEN_VERIFY_ATTEMPTS: Final = 15
TOKEN_VERIFY_DELAY: Final = 1.0

CLUSTER_SCAN_INTERVAL: Final = timedelta(seconds=30)
NODE_SCAN_INTERVAL: Final = timedelta(seconds=30)
DEVICE_REFRESH_INTERVAL: Final = timedelta(minutes=10)
# The certificate, its renewal and the blocklist: shared by every member,
# read from the leader every few minutes.
SHARED_REFRESH_INTERVAL: Final = timedelta(minutes=5)

# A node URL that failed is tried last for this long.
ENDPOINT_BACKOFF: Final = timedelta(minutes=5)

# A member the cluster no longer lists keeps its device this long before
# the integration removes it: a restart is not a departure.
NODE_REMOVE_AFTER: Final = timedelta(minutes=30)

# The node key of a node that runs no cluster (it has no node ID).
LOCAL_NODE: Final = "local"

# Pausing blocking: the console's limits.
PAUSE_MIN: Final = timedelta(minutes=1)
PAUSE_MAX: Final = timedelta(hours=24)

# Model names (GET /api/device says the chip, not the role).
MODEL_BOARD: Final = "ESP32-P4 board"
MODEL_HOST: Final = "Linux node"
MODEL_CLUSTER: Final = "Cluster"

# A member's settings that disagree with the answering node's.
CONFIG_PROBLEM_STATES: Final = frozenset({"diverged", "out-of-sync"})
