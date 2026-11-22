"""What a failed call becomes in Home Assistant: DNS Core's own words where it gave some."""

from __future__ import annotations

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from .const import DOMAIN
from .pydnscore import (
    DnscoreBadRequestError,
    DnscoreConflictError,
    DnscoreError,
    DnscoreForbiddenError,
    DnscoreNotFoundError,
    DnscoreResponseError,
)


def action_error(err: DnscoreError) -> HomeAssistantError:
    """The error to raise for a refused or failed call."""
    if isinstance(err, DnscoreForbiddenError):
        return HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="token_cannot",
            translation_placeholders={"error": err.message},
        )
    if isinstance(
        err, (DnscoreBadRequestError, DnscoreConflictError, DnscoreNotFoundError)
    ):
        return ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="refused",
            translation_placeholders={"error": err.message},
        )
    message = err.message if isinstance(err, DnscoreResponseError) else str(err)
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="action_failed",
        translation_placeholders={"error": message},
    )
