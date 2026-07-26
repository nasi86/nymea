"""The nymea integration for Home Assistant."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from . import maveo_box
from .const import DOMAIN
from .maveo_stick import MaveoStick
from .thing import Thing

PLATFORMS: list[str] = ["cover", "sensor", "binary_sensor", "switch", "button"]

_LOGGER = logging.getLogger(__name__)


async def _async_close_after_error(nymea_hub: maveo_box.MaveoBox) -> None:
    """Close hub resources without masking the active setup exception."""
    try:
        await nymea_hub.async_close()
    except Exception:
        _LOGGER.exception("Error closing nymea resources after setup failure")


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up nymea from a config entry.

    Args:
        hass: Home Assistant instance.
        entry: Config entry for the nymea integration.

    Returns:
        True if setup was successful.

    Raises:
        ConfigEntryNotReady: If unable to connect to the nymea device.
    """
    nymea_hub = maveo_box.MaveoBox(
        hass,
        entry.data["host"],
        entry.data["port"],
        entry.data["token"],
        websocket_port=entry.data.get("websocket_port", 4444),
    )

    try:
        try:
            await nymea_hub.init_connection()
        except Exception as ex:
            raise ConfigEntryNotReady(
                f"Error while connecting to {entry.data['host']}"
            ) from ex

        # Discover and log all available thing classes and things
        await nymea_hub.discover_and_log_all_things()

        # Store in runtime_data instead of hass.data.
        entry.runtime_data = nymea_hub

        await MaveoStick.add(nymea_hub)
        await Thing.add(nymea_hub)

        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

        # Register the long-running listener with this config entry so setup can
        # finish and Home Assistant owns cancellation during unload.
        nymea_hub.start_notification_listener(entry)
    except Exception:
        await _async_close_after_error(nymea_hub)
        raise

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry.

    Args:
        hass: Home Assistant instance.
        entry: Config entry to unload.

    Returns:
        True if unload was successful.
    """
    nymea_hub = entry.runtime_data
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await nymea_hub.async_close()
    return unload_ok
