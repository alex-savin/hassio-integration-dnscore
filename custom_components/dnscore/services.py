"""Actions on a cluster: pause blocking for a while.

Each action names the cluster's config entry. Blocking is the cluster's: any
node takes the change and a follower passes it to the leader. The token
needs `filtering` write; the setup flow mints it with it.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import probatio

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

from .const import DOMAIN, PAUSE_MAX, PAUSE_MIN
from .errors import action_error
from .pydnscore import DnscoreError

ATTR_CONFIG_ENTRY = "config_entry_id"
ATTR_DURATION = "duration"

SERVICE_PAUSE_BLOCKING = "pause_blocking"

SCHEMA_PAUSE = probatio.Schema(
    {
        probatio.Required(ATTR_CONFIG_ENTRY): cv.string,
        probatio.Required(ATTR_DURATION): cv.positive_time_period,
    }
)


def _entry(hass: HomeAssistant, call: ServiceCall) -> Any:
    entry = hass.config_entries.async_get_entry(call.data[ATTR_CONFIG_ENTRY])
    if entry is None or entry.domain != DOMAIN:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="entry_not_found"
        )
    if entry.state is not ConfigEntryState.LOADED:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="entry_not_loaded",
            translation_placeholders={"title": entry.title},
        )
    return entry


async def _async_pause_blocking(call: ServiceCall) -> None:
    """Turn blocking off until the duration has passed; the nodes turn it back on."""
    entry = _entry(call.hass, call)
    duration: timedelta = call.data[ATTR_DURATION]
    if not PAUSE_MIN <= duration <= PAUSE_MAX:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="pause_out_of_range",
            translation_placeholders={
                "min": str(int(PAUSE_MIN.total_seconds() // 60)),
                "max": str(int(PAUSE_MAX.total_seconds() // 60)),
            },
        )
    resume_at = int((dt_util.utcnow() + duration).timestamp())
    cluster = entry.runtime_data.cluster
    try:
        answer = await cluster.async_call_any(
            lambda c: c.set_blocking(False, resume_at=resume_at)
        )
    except DnscoreError as err:
        raise action_error(err) from err
    await cluster.async_apply_config(answer)


def async_setup_services(hass: HomeAssistant) -> None:
    """Register the integration's actions."""
    hass.services.async_register(
        DOMAIN, SERVICE_PAUSE_BLOCKING, _async_pause_blocking, schema=SCHEMA_PAUSE
    )
