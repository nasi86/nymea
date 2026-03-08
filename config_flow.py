"""Config flow for nymea integration."""

from __future__ import annotations

import logging
import re
from typing import Any

from homeassistant import config_entries, exceptions
from homeassistant.components import zeroconf
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_TOKEN
from homeassistant.core import HomeAssistant
import voluptuous as vol

from .const import CONF_WEBSOCKET_PORT, DOMAIN  # pylint:disable=unused-import
from .maveo_box import MaveoBox

_LOGGER = logging.getLogger(__name__)

DEFAULT_JSONRPC_PORT = 2222
DEFAULT_WEBSOCKET_PORT = 4444

DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST, default="192.168.2.179"): str,
        vol.Required(CONF_PORT, default=DEFAULT_JSONRPC_PORT): int,
        vol.Required(CONF_WEBSOCKET_PORT, default=DEFAULT_WEBSOCKET_PORT): int,
    },
)


def _normalize_discovery_properties(
    discovery_info: zeroconf.ZeroconfServiceInfo,
) -> dict[str, str]:
    """Normalize zeroconf TXT properties to a plain lower-case string dict."""
    normalized: dict[str, str] = {}
    for key, value in (discovery_info.properties or {}).items():
        norm_key = key.decode("utf-8", errors="ignore") if isinstance(key, bytes) else str(key)
        if isinstance(value, bytes):
            norm_value = value.decode("utf-8", errors="ignore")
        else:
            norm_value = str(value)
        normalized[norm_key.lower()] = norm_value
    return normalized


def _parse_int_property(properties: dict[str, str], *keys: str) -> int | None:
    """Return first valid integer property for the provided keys."""
    for key in keys:
        raw_value = properties.get(key.lower())
        if raw_value in (None, ""):
            continue
        try:
            return int(raw_value)
        except (TypeError, ValueError):
            _LOGGER.debug("Ignoring non-integer zeroconf property %s=%r", key, raw_value)
    return None


def _resolve_websocket_port(
    discovery_info: zeroconf.ZeroconfServiceInfo,
) -> tuple[int, str]:
    """Resolve the WebSocket port from discovery properties, with safe fallback."""
    properties = _normalize_discovery_properties(discovery_info)
    websocket_port = _parse_int_property(
        properties,
        "websocket_port",
        "websocketport",
        "ws_port",
        "wsport",
        "websocket",
        "ws",
        "port_ws",
        "port-websocket",
    )
    if websocket_port is not None:
        return websocket_port, "zeroconf_property"

    return DEFAULT_WEBSOCKET_PORT, "default"


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> None:
    """Validate the user input allows us to connect.

    Args:
        hass: Home Assistant instance.
        data: User input data with keys from DATA_SCHEMA.

    Raises:
        InvalidHost: If the hostname format is invalid.
        CannotConnect: If connection to the device fails.
    """
    pattern: str = r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.[A-Za-z0-9-]{1,63})*$"
    if not re.match(pattern, data[CONF_HOST]):
        raise InvalidHost

    hub: MaveoBox = MaveoBox(hass, data[CONF_HOST], data[CONF_PORT])
    success: bool = await hub.test_connection()
    if not success:
        raise CannotConnect


class NymeaConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a nymea config flow."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the nymea config flow."""
        self.data: dict[str, Any] = {}
        self.discovery_info: zeroconf.ZeroconfServiceInfo | None = None

    async def async_step_zeroconf(
        self, discovery_info: zeroconf.ZeroconfServiceInfo
    ) -> ConfigFlowResult:
        """Handle zeroconf discovery."""
        _LOGGER.debug("Zeroconf discovery: %s", discovery_info)

        if "_ws._tcp" in discovery_info.type:
            _LOGGER.debug("Ignoring WebSocket discovery, we need JSON-RPC TCP")
            return self.async_abort(reason="not_supported")

        host = discovery_info.host
        port = discovery_info.port or DEFAULT_JSONRPC_PORT
        websocket_port, websocket_port_source = _resolve_websocket_port(discovery_info)

        await self.async_set_unique_id(discovery_info.hostname.replace(".local.", ""))
        self._abort_if_unique_id_configured(updates={CONF_HOST: host})

        self.discovery_info = discovery_info

        try:
            await validate_input(self.hass, {CONF_HOST: host, CONF_PORT: port})
        except (CannotConnect, InvalidHost):
            _LOGGER.warning(
                "Discovered nymea device %s:%s but JSON-RPC validation failed",
                host,
                port,
            )
            return self.async_abort(reason="cannot_connect")

        self.data = {
            CONF_HOST: host,
            CONF_PORT: port,
            CONF_WEBSOCKET_PORT: websocket_port,
        }
        _LOGGER.info(
            "Discovered nymea device at %s:%s (websocket_port=%s via %s)",
            host,
            port,
            websocket_port,
            websocket_port_source,
        )

        self.context["title_placeholders"] = {"name": f"nymea ({host})"}
        return await self.async_step_zeroconf_confirm()

    async def async_step_zeroconf_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm discovery."""
        _LOGGER.debug("Zeroconf confirm step, user_input: %s", user_input)
        if user_input is None:
            return self.async_show_form(
                step_id="zeroconf_confirm",
                description_placeholders={"host": self.data.get(CONF_HOST, "unknown")},
            )

        _LOGGER.debug("User confirmed discovery, proceeding to link")
        return await self.async_step_link()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        _LOGGER.debug("User config step, user_input: %s", user_input)
        errors = {}
        if user_input is not None:
            try:
                await validate_input(self.hass, user_input)
                self.data = user_input
                _LOGGER.info(
                    "Manual configuration validated for %s:%s (websocket_port=%s)",
                    user_input[CONF_HOST],
                    user_input[CONF_PORT],
                    user_input.get(CONF_WEBSOCKET_PORT),
                )
                return await self.async_step_link()
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidHost:
                errors["host"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="user", data_schema=DATA_SCHEMA, errors=errors
        )

    async def async_step_link(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Attempt to link with the nymea bridge."""
        if user_input is None:
            _LOGGER.debug("Showing link form")
            return self.async_show_form(step_id="link")

        if not self.data or CONF_HOST not in self.data:
            _LOGGER.error("Configuration data missing in link step: %s", self.data)
            return self.async_abort(reason="unknown")

        host = self.data[CONF_HOST]
        port = self.data[CONF_PORT]
        websocket_port = self.data.get(CONF_WEBSOCKET_PORT, DEFAULT_WEBSOCKET_PORT)
        _LOGGER.info(
            "Starting nymea pairing for %s:%s using JSON-RPC; websocket_port=%s is not required for initial pairing",
            host,
            port,
            websocket_port,
        )

        box: MaveoBox = MaveoBox(
            self.hass,
            host,
            port,
            websocket_port=websocket_port,
        )
        try:
            token: str | None = await box.init_connection()
        except TimeoutError:
            _LOGGER.warning(
                "Pairing timed out for %s:%s after waiting for push-button authentication",
                host,
                port,
            )
            return self.async_show_form(step_id="link", errors={"base": "cannot_connect"})
        except Exception as ex:
            _LOGGER.exception(
                "Pairing failed for %s:%s (jsonrpc_port=%s, websocket_port=%s): %s",
                host,
                port,
                port,
                websocket_port,
                ex,
            )
            return self.async_show_form(step_id="link", errors={"base": "cannot_connect"})

        self.data[CONF_TOKEN] = token
        _LOGGER.info(
            "Pairing succeeded for %s:%s; config entry will be created with websocket_port=%s",
            host,
            port,
            websocket_port,
        )

        return self.async_create_entry(
            title=f"nymea({self.data[CONF_HOST]})", data=self.data
        )


class CannotConnect(exceptions.HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidHost(exceptions.HomeAssistantError):
    """Error to indicate there is an invalid hostname."""
