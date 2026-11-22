"""The blocking switch: the cluster's filtering on or off (a pause shows as off)."""

from __future__ import annotations

from typing import Any, override

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import DnscoreConfigEntry
from .coordinator import DnscoreClusterCoordinator
from .entity import DnscoreClusterEntity
from .errors import action_error
from .pydnscore import DnscoreError, epoch_to_datetime

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DnscoreConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the blocking switch."""
    async_add_entities([DnscoreBlockingSwitch(entry.runtime_data.cluster)])


class DnscoreBlockingSwitch(DnscoreClusterEntity, SwitchEntity):
    """Blocking for the whole cluster (`blocking_enabled`, a shared setting).

    Off stays off until turned on; `dnscore.pause_blocking` turns it off
    until a time, after which the nodes turn it on themselves.
    """

    _attr_translation_key = "blocking"

    def __init__(self, coordinator: DnscoreClusterCoordinator) -> None:
        """Attach the switch to the cluster device."""
        super().__init__(coordinator, "blocking")

    @property
    @override
    def available(self) -> bool:
        """Available while the cluster answers and the token reads filtering."""
        data = self.coordinator.data
        return super().available and data.config.blocking_enabled is not None

    @property
    @override
    def is_on(self) -> bool | None:
        """Blocking is on (not off, not paused)."""
        return self.coordinator.data.config.blocking_enabled

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """When a pause ends, while one runs."""
        config = self.coordinator.data.config
        resumes = epoch_to_datetime(config.blocking_resume_at)
        if config.blocking_enabled is not False or resumes is None:
            return None
        return {"resumes_at": resumes.isoformat()}

    async def _async_set(self, enabled: bool) -> None:
        try:
            answer = await self.coordinator.async_call_any(
                lambda c: c.set_blocking(enabled)
            )
        except DnscoreError as err:
            raise action_error(err) from err
        await self.coordinator.async_apply_config(answer)

    @override
    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn blocking on (ends any pause)."""
        await self._async_set(True)

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn blocking off until it is turned on again."""
        await self._async_set(False)
