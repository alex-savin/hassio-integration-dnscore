"""Config flow for DNS Core.

The flow takes one node's address, then the OTA password once: with it, it
reads who the node and its cluster are, and mints a scoped API token
(`POST /api/agents`), optionally limited to Home Assistant's address. The
OTA password is never stored. One entry per cluster: its unique ID is the
cluster's DNS name (`tls.server_name`, shared by every member and carried
by its certificate), else the cluster key's ID, else the node's MAC.

The DNS Core app (0.95.0) announces itself to the Supervisor, which starts
the flow at the hassio step with the node's address filled in. The boards
announce nothing (no mDNS): they are set up by address.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import ipaddress
import socket
from typing import Any, override

import probatio

from homeassistant.components import network
from homeassistant.config_entries import SOURCE_REAUTH, ConfigFlow, ConfigFlowResult
from homeassistant.const import (
    CONF_HOST,
    CONF_PORT,
    CONF_SSL,
    CONF_TOKEN,
    CONF_VERIFY_SSL,
)
from homeassistant.data_entry_flow import AbortFlow, section
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv, instance_id
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.helpers.service_info.hassio import HassioServiceInfo
from homeassistant.helpers.typing import UNDEFINED, UndefinedType

from .const import (
    CONF_KNOWN_URLS,
    CONF_OTA_PASSWORD,
    CONF_RESTRICT_NETWORK,
    CONF_TOKEN_NAME,
    DEFAULT_PORT,
    DOMAIN,
    LOGGER,
    SECTION_CONNECTION,
    TOKEN_NAME_PREFIX,
    TOKEN_NETWORKS_MAX,
    TOKEN_SCOPES,
    TOKEN_VERIFY_ATTEMPTS,
    TOKEN_VERIFY_DELAY,
)
from .pydnscore import (
    ClusterStatus,
    Device,
    DnscoreAuthenticationError,
    DnscoreBadRequestError,
    DnscoreClient,
    DnscoreConflictError,
    DnscoreConnectionError,
    DnscoreError,
    DnscoreForbiddenError,
    DnscoreHostNotAllowedError,
    DnscoreInvalidResponseError,
    DnscoreNotFoundError,
    DnscoreRateLimitedError,
    DnscoreTlsError,
    ServerConfig,
    build_url,
)

USER_SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_HOST): str,
        probatio.Required(CONF_PORT, default=DEFAULT_PORT): cv.port,
        probatio.Required(SECTION_CONNECTION): section(
            probatio.Schema(
                {
                    probatio.Required(CONF_SSL, default=True): bool,
                    probatio.Required(CONF_VERIFY_SSL, default=True): bool,
                }
            ),
            {"collapsed": True},
        ),
    }
)

CREDENTIALS_SCHEMA = probatio.Schema(
    {
        probatio.Required(probatio.Secret(CONF_OTA_PASSWORD)): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="off")
        ),
        probatio.Optional(CONF_RESTRICT_NETWORK): str,
    }
)


class FlowError(Exception):
    """A step failed with an error the form shows."""

    def __init__(
        self, code: str, placeholders: Mapping[str, str] | None = None
    ) -> None:
        """Keep the error code (a key under `config.error`) and its placeholders."""
        super().__init__(code)
        self.code = code
        self.placeholders = dict(placeholders or {})


def parse_networks(text: str) -> list[str]:
    """Up to four addresses or ranges, comma or space separated; raises ValueError."""
    items = [item for item in text.replace(",", " ").split() if item]
    if len(items) > TOKEN_NETWORKS_MAX:
        raise ValueError("too many networks")
    return [str(ipaddress.ip_network(item, strict=False)) for item in items]


def cluster_unique_id(
    config: ServerConfig, cluster: ClusterStatus, device: Device | None, fallback: str
) -> str:
    """The identity of a cluster, the same whichever member is asked."""
    if config.server_name:
        return config.server_name
    if cluster.enabled and cluster.cluster_key_id:
        return f"cluster:{cluster.cluster_key_id}"
    if device is not None and device.mac:
        return f"node:{device.mac}"
    return f"node:{fallback}"


def _error_code(err: DnscoreError, *, ssl: bool) -> str:
    """The form error for a failed call made with the OTA password."""
    if isinstance(err, DnscoreRateLimitedError):
        return "too_many_attempts"
    if isinstance(err, DnscoreAuthenticationError):
        return "invalid_auth"
    if isinstance(err, DnscoreForbiddenError):
        # On plain HTTP DNS Core refuses credentials while HTTPS works.
        return "invalid_auth" if ssl else "https_required"
    if isinstance(err, DnscoreTlsError):
        return "invalid_certificate"
    if isinstance(err, DnscoreHostNotAllowedError):
        return "host_not_allowed"
    if isinstance(err, (DnscoreInvalidResponseError, DnscoreNotFoundError)):
        return "not_dnscore"
    return "cannot_connect"


class DnscoreConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up a cluster from one of its nodes."""

    VERSION = 1
    MINOR_VERSION = 1

    def __init__(self) -> None:
        """Start with nothing known."""
        self._host: str | None = None
        self._port: int = DEFAULT_PORT
        self._ssl: bool = True
        self._verify_ssl: bool = True
        self._url: str = ""
        self._local_address: str | None = None
        self._app_name: str | None = None

    # ---- helpers ------------------------------------------------------------------------

    def _set_connection(
        self, host: str, port: int, ssl: bool, verify_ssl: bool
    ) -> None:
        self._host = host.strip()
        self._port = port
        self._ssl = ssl
        self._verify_ssl = verify_ssl
        self._url = build_url(self._host, port, ssl=ssl)

    def _client(
        self, url: str | None = None, token: str | None = None
    ) -> DnscoreClient:
        return DnscoreClient(
            async_get_clientsession(self.hass, verify_ssl=self._verify_ssl),
            url or self._url,
            token=token,
            verify_ssl=self._verify_ssl,
        )

    async def _async_probe(self, url: str | None = None) -> str | None:
        """Check that a DNS Core node answers, set up; the error code if not."""
        try:
            status = await self._client(url).get_auth_status()
        except DnscoreTlsError:
            return "invalid_certificate"
        except DnscoreConnectionError:
            return "cannot_connect"
        except DnscoreHostNotAllowedError:
            return "host_not_allowed"
        except DnscoreRateLimitedError:
            return "too_many_attempts"
        except DnscoreInvalidResponseError, DnscoreNotFoundError:
            return "not_dnscore"
        except DnscoreError:
            return "cannot_connect"
        if status.setup_needed:
            return "setup_needed"
        self._local_address = await self._async_local_address()
        return None

    async def _async_local_address(self) -> str | None:
        """The address Home Assistant reaches the node from: the suggested limit."""
        target: str | UndefinedType = UNDEFINED
        host = self._host or ""
        try:
            if ipaddress.ip_address(host).version == 4:
                target = host
        except ValueError:
            try:
                infos = await self.hass.loop.getaddrinfo(
                    host, self._port, family=socket.AF_INET, type=socket.SOCK_STREAM
                )
            except OSError, RuntimeError:
                infos = []
            if infos:
                target = str(infos[0][4][0])
        try:
            return await network.async_get_source_ip(self.hass, target)
        except HomeAssistantError:
            return None

    async def _async_token_name(self) -> str:
        if self.source == SOURCE_REAUTH:
            name = self._get_reauth_entry().data.get(CONF_TOKEN_NAME)
            if isinstance(name, str) and name:
                return name
        iid = await instance_id.async_get(self.hass)
        return f"{TOKEN_NAME_PREFIX}-{iid.replace('-', '')[:8]}"

    async def _async_token_works(self, token: str) -> bool:
        """Use the new token once; a follower takes it a second or so later.

        The other members get it with the shared settings; one that lacks it
        still answers its node coordinator at the next refresh.
        """
        client = self._client(token=token)
        for attempt in range(TOKEN_VERIFY_ATTEMPTS):
            try:
                await client.get_cluster_status()
            except (DnscoreAuthenticationError, DnscoreConnectionError) as err:
                LOGGER.debug(
                    "The new token is not taken yet (%s): %s", attempt + 1, err
                )
                await asyncio.sleep(TOKEN_VERIFY_DELAY)
                continue
            except DnscoreError as err:
                LOGGER.debug("The new token was refused: %s", err)
                return False
            return True
        return False

    async def _async_mint(self, password: str, networks: list[str]) -> ConfigFlowResult:
        """Identify the cluster with the OTA password, mint the token, finish."""
        client = self._client()
        try:
            config = await client.get_config(ota_password=password)
            cluster = await client.get_cluster_status(ota_password=password)
            device = (
                None
                if cluster.enabled
                else await client.get_device(ota_password=password)
            )
        except DnscoreError as err:
            raise FlowError(_error_code(err, ssl=self._ssl)) from err

        await self.async_set_unique_id(
            cluster_unique_id(config, cluster, device, f"{self._host}:{self._port}"),
            raise_on_progress=False,
        )
        if self.source == SOURCE_REAUTH:
            self._abort_if_unique_id_mismatch(reason="wrong_cluster")
        else:
            self._abort_if_unique_id_configured()

        name = await self._async_token_name()
        try:
            existing = await client.list_tokens(ota_password=password)
            if any(token.name == name for token in existing):
                await client.revoke_token(ota_password=password, name=name)
            token = await client.create_token(
                ota_password=password, name=name, scopes=TOKEN_SCOPES, networks=networks
            )
        except (DnscoreBadRequestError, DnscoreConflictError) as err:
            raise FlowError("token_refused", {"error": err.message}) from err
        except DnscoreError as err:
            raise FlowError(_error_code(err, ssl=self._ssl)) from err

        if not await self._async_token_works(token):
            raise FlowError("token_not_accepted")

        known_urls = sorted(
            build_url(m.host_name, config.port_https, ssl=self._ssl)
            for m in cluster.members
            if m.host_name
        )
        if self.source == SOURCE_REAUTH:
            return self.async_update_reload_and_abort(
                self._get_reauth_entry(),
                data_updates={CONF_TOKEN: token, CONF_TOKEN_NAME: name},
            )
        return self.async_create_entry(
            title=config.server_name or self._host or "DNS Core",
            data={
                CONF_HOST: self._host,
                CONF_PORT: self._port,
                CONF_SSL: self._ssl,
                CONF_VERIFY_SSL: self._verify_ssl,
                CONF_TOKEN: token,
                CONF_TOKEN_NAME: name,
                CONF_KNOWN_URLS: known_urls,
            },
        )

    async def _async_credentials_step(
        self, step_id: str, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        """Ask for the OTA password (and an optional network limit) and finish."""
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {"host": self._host or "", "error": ""}
        if user_input is not None:
            try:
                networks = parse_networks(user_input.get(CONF_RESTRICT_NETWORK, ""))
            except ValueError:
                errors[CONF_RESTRICT_NETWORK] = "invalid_network"
            else:
                try:
                    return await self._async_mint(
                        user_input[CONF_OTA_PASSWORD], networks
                    )
                except FlowError as err:
                    errors["base"] = err.code
                    placeholders.update(err.placeholders)
                except AbortFlow:
                    raise
                except Exception:  # noqa: BLE001 (a form error, never a stack trace)
                    LOGGER.exception("Unexpected error while making the API token")
                    errors["base"] = "unknown"
        suggested = {
            CONF_RESTRICT_NETWORK: (user_input or {}).get(
                CONF_RESTRICT_NETWORK, self._local_address or ""
            )
        }
        return self.async_show_form(
            step_id=step_id,
            data_schema=self.add_suggested_values_to_schema(
                CREDENTIALS_SCHEMA, suggested
            ),
            errors=errors,
            description_placeholders=placeholders,
        )

    # ---- user ---------------------------------------------------------------------------

    @override
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a node's address."""
        errors: dict[str, str] = {}
        if user_input is not None:
            connection = user_input[SECTION_CONNECTION]
            self._set_connection(
                user_input[CONF_HOST],
                user_input[CONF_PORT],
                connection[CONF_SSL],
                connection[CONF_VERIFY_SSL],
            )
            if (error := await self._async_probe()) is None:
                return await self.async_step_credentials()
            errors["base"] = error
        suggested = {
            CONF_HOST: self._host or "",
            CONF_PORT: self._port,
            SECTION_CONNECTION: {
                CONF_SSL: self._ssl,
                CONF_VERIFY_SSL: self._verify_ssl,
            },
        }
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(USER_SCHEMA, suggested),
            errors=errors,
        )

    async def async_step_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Mint the API token with the OTA password."""
        return await self._async_credentials_step("credentials", user_input)

    # ---- discovery ----------------------------------------------------------------------

    @override
    async def async_step_hassio(
        self, discovery_info: HassioServiceInfo
    ) -> ConfigFlowResult:
        """The DNS Core app announced itself to the Supervisor (DNS Core 0.95.0).

        Its payload (linux/app/supervisor.h): `host` and `port` of this
        node's console (its own DNS name, else the cluster's), `ssl`, and
        `server_name`, the cluster's name, which identifies the cluster.
        """
        config = discovery_info.config
        host = str(config.get(CONF_HOST) or "").strip()
        if not host:
            return self.async_abort(reason="invalid_discovery_info")
        if name := str(config.get("server_name") or "").strip().rstrip(".").lower():
            await self.async_set_unique_id(name)
            self._abort_if_unique_id_configured()
        self._async_abort_entries_match({CONF_HOST: host})
        ssl = bool(config.get(CONF_SSL, True))
        try:
            port = int(config.get(CONF_PORT) or (DEFAULT_PORT if ssl else 80))
        except TypeError, ValueError:
            return self.async_abort(reason="invalid_discovery_info")
        self._set_connection(host, port, ssl, True)
        self._app_name = str(discovery_info.name or "DNS Core")
        self.context["title_placeholders"] = {"name": self._app_name}
        return await self.async_step_hassio_confirm()

    async def async_step_hassio_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm the app's node, then ask for the OTA password."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if (error := await self._async_probe()) is None:
                return await self.async_step_credentials()
            errors["base"] = error
        return self.async_show_form(
            step_id="hassio_confirm",
            description_placeholders={
                "addon": self._app_name or "DNS Core",
                "host": self._host or "",
            },
            errors=errors,
        )

    # ---- reauth -------------------------------------------------------------------------

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """The token stopped working: mint a new one."""
        self._set_connection(
            entry_data[CONF_HOST],
            entry_data[CONF_PORT],
            entry_data[CONF_SSL],
            entry_data[CONF_VERIFY_SSL],
        )
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the OTA password again; any member that answers will do."""
        if user_input is None:
            entry = self._get_reauth_entry()
            for url in [self._url, *entry.data.get(CONF_KNOWN_URLS, [])]:
                if await self._async_probe(url) is None:
                    self._url = url
                    break
        return await self._async_credentials_step("reauth_confirm", user_input)
