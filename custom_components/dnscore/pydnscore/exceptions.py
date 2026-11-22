"""Errors raised by the DNS Core admin API client.

Programs act on the class (and so on the HTTP status), never on the
message: DNS Core's error sentences are meant for people.
"""

from __future__ import annotations


class DnscoreError(Exception):
    """Base class for every error this client raises."""


class DnscoreConnectionError(DnscoreError):
    """The node could not be reached, or did not answer in time."""


class DnscoreTlsError(DnscoreConnectionError):
    """The node's certificate did not verify (wrong name, untrusted, expired)."""


class DnscoreInvalidResponseError(DnscoreError):
    """The answer was not the JSON the route promises: probably not DNS Core."""


class DnscoreResponseError(DnscoreError):
    """The node answered with an error status.

    `status` is the HTTP status, `message` the node's own sentence (the
    `error` member of its JSON answer) and `path` the route that was asked.
    """

    def __init__(self, status: int, message: str, *, path: str) -> None:
        """Keep the status, the node's message and the route."""
        super().__init__(f"{path}: {status} {message}")
        self.status = status
        self.message = message
        self.path = path


class DnscoreBadRequestError(DnscoreResponseError):
    """400 or 422: the request was malformed, or a rule refused a value."""


class DnscoreAuthenticationError(DnscoreResponseError):
    """401: no credentials, wrong ones, or an unknown or revoked API token."""


class DnscoreForbiddenError(DnscoreResponseError):
    """403: the token lacks the scope or the network, or plain HTTP was used."""


class DnscoreNotFoundError(DnscoreResponseError):
    """404: no such route, or no such API client."""


class DnscoreConflictError(DnscoreResponseError):
    """409: the node's state refuses it (a follower, a changed generation, ...)."""


class DnscoreHostNotAllowedError(DnscoreResponseError):
    """421: the Host header is not a name this node answers to."""


class DnscoreRateLimitedError(DnscoreResponseError):
    """429: too many wrong passwords from this address."""

    def __init__(
        self, status: int, message: str, *, path: str, retry_after: int | None
    ) -> None:
        """Keep the seconds the node asked to wait, when it said."""
        super().__init__(status, message, path=path)
        self.retry_after = retry_after


class DnscoreNotSupportedError(DnscoreResponseError):
    """501: this build or node cannot do it (the Linux build and OTA, say)."""


class DnscoreUnavailableError(DnscoreResponseError):
    """502, 503, 504 or 507: the node is busy, out of room, or a fetch failed."""
