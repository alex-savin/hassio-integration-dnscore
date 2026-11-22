"""An async client for one DNS Core node's admin API.

The client talks to one node, by its base URL. It authenticates with an
API token (`Authorization: Bearer dnsa_...`, HTTPS only) or, for the few
calls that need it, the OTA password (`X-OTA-Password`), which it never
keeps. A token cannot use DNS Core's relay (`/api/peer` is admin only), so
each node is asked directly; `address` reaches a node whose name the
resolver does not know.

The caller owns the aiohttp session: the client neither creates nor closes
one.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import json
from typing import Any, Final

import aiohttp
from yarl import URL

from .exceptions import (
    DnscoreAuthenticationError,
    DnscoreBadRequestError,
    DnscoreConflictError,
    DnscoreConnectionError,
    DnscoreForbiddenError,
    DnscoreHostNotAllowedError,
    DnscoreInvalidResponseError,
    DnscoreNotFoundError,
    DnscoreNotSupportedError,
    DnscoreRateLimitedError,
    DnscoreResponseError,
    DnscoreTlsError,
    DnscoreUnavailableError,
)
from .models import (
    AcmeStatus,
    AuthStatus,
    BlocklistStatus,
    ClusterStatus,
    Device,
    Health,
    OtaState,
    ServerConfig,
    Stats,
    TlsStatus,
    TokenInfo,
)

DEFAULT_TIMEOUT: Final = 10.0

# The areas a token may be granted, and the access each may have
# (`components/agent_access/include/agent_access_rule.h`).
TOKEN_AREAS: Final = (
    "status",
    "querylog",
    "records",
    "filtering",
    "resolution",
    "system",
)
TOKEN_ACCESS: Final = ("none", "read", "write")
TOKEN_PREFIX: Final = "dnsa_"  # noqa: S105 (the format's prefix, not a token)


def build_url(host: str, port: int | None = None, *, ssl: bool = True) -> str:
    """The base URL of a node's admin API, such as `https://dns01.example.com/`."""
    scheme = "https" if ssl else "http"
    name = host.strip().rstrip("/")
    if ":" in name and not name.startswith("["):
        name = f"[{name}]"  # an IPv6 address
    if port is None or port == (443 if ssl else 80):
        return f"{scheme}://{name}/"
    return f"{scheme}://{name}:{port}/"


def _error_message(body: Any, text: str) -> str:
    """The node's sentence: `error`, or `message` (the blocklist's refusals)."""
    if isinstance(body, dict):
        for key in ("error", "message"):
            if isinstance(body.get(key), str) and body[key]:
                return str(body[key])
    return text.strip()[:200] or "no message"


def _retry_after(headers: Mapping[str, str]) -> int | None:
    value = headers.get("Retry-After")
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _response_error(
    status: int, body: Any, text: str, path: str, headers: Mapping[str, str]
) -> DnscoreResponseError:
    message = _error_message(body, text)
    if status == 429:
        return DnscoreRateLimitedError(
            status, message, path=path, retry_after=_retry_after(headers)
        )
    by_status: dict[int, type[DnscoreResponseError]] = {
        400: DnscoreBadRequestError,
        401: DnscoreAuthenticationError,
        403: DnscoreForbiddenError,
        404: DnscoreNotFoundError,
        409: DnscoreConflictError,
        421: DnscoreHostNotAllowedError,
        422: DnscoreBadRequestError,
        501: DnscoreNotSupportedError,
        502: DnscoreUnavailableError,
        503: DnscoreUnavailableError,
        504: DnscoreUnavailableError,
        507: DnscoreUnavailableError,
    }
    return by_status.get(status, DnscoreResponseError)(status, message, path=path)


class DnscoreClient:
    """One node's admin API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        url: str,
        *,
        token: str | None = None,
        verify_ssl: bool = True,
        request_timeout: float = DEFAULT_TIMEOUT,
        address: str | None = None,
    ) -> None:
        """Talk to the node at `url` (its base URL) over `session`.

        `address` connects to that IP address instead of what DNS answers
        for the URL's name. The node's certificate is still checked against
        the name, and the Host header still names it, so the node's host
        check sees a name it serves.
        """
        self._session = session
        self._base = URL(url)
        self._token = token
        self._verify_ssl = verify_ssl
        self._timeout = aiohttp.ClientTimeout(total=request_timeout)
        self._address = address or None

    @property
    def url(self) -> str:
        """The node's base URL."""
        return str(self._base)

    @property
    def host(self) -> str:
        """The host part of the base URL."""
        return self._base.host or ""

    @property
    def address(self) -> str | None:
        """The IP address connected to instead of the name's, if any."""
        return self._address

    @property
    def _where(self) -> str:
        """The node, for messages: its name, and the address when one is used."""
        return self.host if self._address is None else f"{self.host} ({self._address})"

    def _host_header(self) -> str:
        """The base URL's host and, when not the scheme's default, its port."""
        if self._base.is_default_port():
            return self.host
        return f"{self.host}:{self._base.port}"

    def _at_address(
        self, target: URL, headers: dict[str, str]
    ) -> tuple[URL, dict[str, Any]]:
        """`target` at the address, when one is set, and the request's extras.

        The Host header goes into `headers`; the extras name the server for
        TLS (SNI and the certificate's check).
        """
        if self._address is None or not self.host:
            return target, {}
        headers["Host"] = self._host_header()
        extra: dict[str, Any] = {}
        if self._base.scheme == "https":
            extra["server_hostname"] = self.host
        return target.with_host(self._address), extra

    # ---- transport ----------------------------------------------------------------------

    async def request(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
        query: Mapping[str, str] | None = None,
        ota_password: str | None = None,
        use_token: bool = True,
    ) -> Any:
        """Call a route and return its JSON answer.

        Raises a `DnscoreResponseError` subclass for an error status,
        `DnscoreConnectionError` when the node cannot be reached, and
        `DnscoreInvalidResponseError` for an answer that is not JSON.
        """
        headers = {"Accept": "application/json"}
        if use_token and self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        if ota_password is not None:
            headers["X-OTA-Password"] = ota_password
        data: str | None = None
        if method.upper() == "POST":
            headers["Content-Type"] = "application/json"
            data = json.dumps(dict(body or {}))
        target = self._base.with_path(path)
        if query:
            target = target.with_query(dict(query))
        target, extra = self._at_address(target, headers)
        try:
            async with self._session.request(
                method,
                target,
                headers=headers,
                data=data,
                timeout=self._timeout,
                allow_redirects=False,
                ssl=self._verify_ssl,
                **extra,
            ) as response:
                status = response.status
                response_headers = dict(response.headers)
                text = await response.text(errors="replace")
        except aiohttp.ClientConnectorCertificateError as err:
            raise DnscoreTlsError(
                f"{self._where}: certificate not trusted: {err}"
            ) from err
        except aiohttp.ClientSSLError as err:
            raise DnscoreTlsError(f"{self._where}: TLS failed: {err}") from err
        except TimeoutError as err:
            raise DnscoreConnectionError(f"{self._where}: no answer in time") from err
        except aiohttp.ClientError as err:
            raise DnscoreConnectionError(f"{self._where}: {err}") from err

        try:
            decoded: Any = json.loads(text) if text else None
        except ValueError:
            decoded = None
        if 200 <= status < 300:
            if not isinstance(decoded, (dict, list)):
                raise DnscoreInvalidResponseError(
                    f"{path}: answer {status} is not JSON: is this a DNS Core node?"
                )
            return decoded
        raise _response_error(status, decoded, text, path, response_headers)

    async def _get_object(
        self,
        path: str,
        *,
        query: Mapping[str, str] | None = None,
        ota_password: str | None = None,
        use_token: bool = True,
    ) -> dict[str, Any]:
        answer = await self.request(
            "GET", path, query=query, ota_password=ota_password, use_token=use_token
        )
        if not isinstance(answer, dict):
            raise DnscoreInvalidResponseError(f"{path}: expected a JSON object")
        return answer

    async def _post_object(
        self,
        path: str,
        body: Mapping[str, Any] | None = None,
        *,
        ota_password: str | None = None,
        use_token: bool = True,
    ) -> dict[str, Any]:
        answer = await self.request(
            "POST", path, body=body, ota_password=ota_password, use_token=use_token
        )
        return answer if isinstance(answer, dict) else {}

    def _credentials(self, ota_password: str | None) -> dict[str, Any]:
        """The OTA password instead of the token, when one is given."""
        return {"ota_password": ota_password, "use_token": ota_password is None}

    # ---- open routes --------------------------------------------------------------------

    async def get_auth_status(self) -> AuthStatus:
        """`GET /api/auth/status`: an open route; sends no credentials (a token gets 403)."""
        return AuthStatus.from_dict(
            await self._get_object("/api/auth/status", use_token=False)
        )

    # ---- status area (token: status read) -----------------------------------------------

    async def get_stats(self) -> Stats:
        """`GET /api/stats`: the node's counters since it started, and its state."""
        return Stats.from_dict(await self._get_object("/api/stats"))

    async def get_device(self, *, ota_password: str | None = None) -> Device:
        """`GET /api/device`: what the node is and runs."""
        return Device.from_dict(
            await self._get_object("/api/device", **self._credentials(ota_password))
        )

    async def get_cluster_status(
        self, *, ota_password: str | None = None
    ) -> ClusterStatus:
        """`GET /api/cluster/status`: the cluster as this node sees it."""
        return ClusterStatus.from_dict(
            await self._get_object(
                "/api/cluster/status", **self._credentials(ota_password)
            )
        )

    async def get_health(self, *, ota_password: str | None = None) -> Health:
        """`GET /api/health`: the node's verdict (admin only up to 0.94.5)."""
        return Health.from_dict(
            await self._get_object("/api/health", **self._credentials(ota_password))
        )

    # ---- settings (each key needs its own area) -----------------------------------------

    async def get_config(self, *, ota_password: str | None = None) -> ServerConfig:
        """`GET /api/config`: a token gets only the keys of the areas it reads."""
        return ServerConfig.from_dict(
            await self._get_object("/api/config", **self._credentials(ota_password))
        )

    async def set_config(
        self, changes: Mapping[str, Any], *, ota_password: str | None = None
    ) -> dict[str, Any]:
        """`POST /api/config`: change settings (a merge at the top level).

        A token needs write in the area of every key it sends; one key out
        of scope refuses the whole change.
        """
        return await self._post_object(
            "/api/config", changes, **self._credentials(ota_password)
        )

    async def set_blocking(
        self, enabled: bool, *, resume_at: int = 0
    ) -> dict[str, Any]:
        """Turn blocking on, off, or off until `resume_at` (the node's Unix time).

        On clears any pause; off with `resume_at` 0 stays off until turned
        on. The change is the cluster's (filtering write). Returns the
        settings as they now are.
        """
        changes: dict[str, Any] = {"blocking_enabled": enabled}
        if not enabled:
            changes["blocking_resume_at"] = resume_at
        return await self.set_config(changes)

    # ---- filtering and resolution areas -------------------------------------------------

    async def get_blocklist_status(self) -> BlocklistStatus:
        """`GET /api/blocklist/update`: the list the node runs and its downloads."""
        return BlocklistStatus.from_dict(
            await self._get_object("/api/blocklist/update")
        )

    async def refresh_blocklist(self) -> dict[str, Any]:
        """`POST /api/blocklist/update`: download now (the leader only; 409 elsewhere)."""
        return await self._post_object("/api/blocklist/update", {})

    async def flush_cache(self) -> dict[str, Any]:
        """`POST /api/cache/flush`: empty this node's answer cache."""
        return await self._post_object("/api/cache/flush", {})

    # ---- system area (token: system read) -----------------------------------------------

    async def get_tls_status(self) -> TlsStatus:
        """`GET /api/tls/status`: the certificate the node serves."""
        return TlsStatus.from_dict(await self._get_object("/api/tls/status"))

    async def get_acme(self) -> AcmeStatus:
        """`GET /api/acme`: the certificate's renewal (the leader orders)."""
        return AcmeStatus.from_dict(await self._get_object("/api/acme"))

    async def get_ota(self) -> OtaState:
        """`GET /api/ota`: the running image and any install's progress."""
        return OtaState.from_dict(await self._get_object("/api/ota"))

    # ---- API clients (admin: the OTA password, never a token) ---------------------------

    async def list_tokens(self, *, ota_password: str) -> list[TokenInfo]:
        """`GET /api/agents`: the API clients' names, grants and limits."""
        answer = await self._get_object(
            "/api/agents", ota_password=ota_password, use_token=False
        )
        return [
            TokenInfo.from_dict(a)
            for a in answer.get("agents") or []
            if isinstance(a, dict)
        ]

    async def create_token(
        self,
        *,
        ota_password: str,
        name: str,
        scopes: Iterable[str],
        networks: Iterable[str] = (),
        expires_in_s: int = 0,
    ) -> str:
        """`POST /api/agents` `create`: mint a token and return it (shown once).

        `scopes` are `area:access` strings, such as `status:read`.
        """
        body: dict[str, Any] = {
            "action": "create",
            "name": name,
            "scopes": list(scopes),
            "networks": list(networks),
        }
        if expires_in_s:
            body["expires_in_s"] = expires_in_s
        answer = await self._post_object(
            "/api/agents", body, ota_password=ota_password, use_token=False
        )
        token = answer.get("token")
        if not isinstance(token, str) or not token.startswith(TOKEN_PREFIX):
            raise DnscoreInvalidResponseError("/api/agents: no token in the answer")
        return token

    async def revoke_token(self, *, ota_password: str, name: str) -> None:
        """`POST /api/agents` `revoke`: the token fails from the next request."""
        await self._post_object(
            "/api/agents",
            {"action": "revoke", "name": name},
            ota_password=ota_password,
            use_token=False,
        )
