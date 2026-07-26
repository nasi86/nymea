"""Support for nymea cover entities."""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.cover import (
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN

if TYPE_CHECKING:
    from .maveo_stick import MaveoStick

from .maveo_stick import State

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(seconds=5)


# This function is called as part of the __init__.async_setup_entry (via the
# hass.config_entries.async_forward_entry_setup call).
async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add cover entities for passed config_entry in HA.

    Args:
        hass: Home Assistant instance.
        config_entry: Config entry for this integration.
        async_add_entities: Callback to add entities to HA.
    """
    # The maveoBox is loaded from the config entry's runtime_data.
    maveoBox = config_entry.runtime_data

    # Add all entities to HA.
    async_add_entities(GarageDoor(stick) for stick in maveoBox.maveoSticks)


class GarageDoor(CoverEntity):
    """Representation of a GarageDoor."""

    device_class = CoverDeviceClass.GARAGE
    has_entity_name = True

    # Polling disabled - using push notifications
    should_poll = False
    supported_features = CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE

    def __init__(self, maveoStick: MaveoStick) -> None:
        """Initialize the garage door."""
        # Usual setup is done here. Callbacks are added in async_added_to_hass.
        self._maveoStick: MaveoStick = maveoStick
        self._attr_unique_id: str = f"{self._maveoStick.id}_cover"

        # This is the name for this *entity*, the "name" attribute from "device_info"
        # is used as the device name for device screens in the UI. This name is used on
        # entity screens, and used to build the Entity ID that's used is automations etc.
        self._attr_name: str = self._maveoStick.name
        self.stateTypeIdState = self._maveoStick.state_type_id
        self._available: bool = True

    async def async_added_to_hass(self) -> None:
        """Run when this Entity has been added to HA."""
        # Fetch initial state before notifications start
        await self.async_update()

        # Register callback for push notifications
        self._maveoStick.register_callback(self.async_write_ha_state)

    async def async_update(self) -> None:
        """Fetch initial state (called once before notification listener starts)."""
        if self._maveoStick.state is not State.unknown:
            self._available = True
            return

        params: dict[str, str] = {}
        params["thingId"] = self._maveoStick.id
        params["stateTypeId"] = self.stateTypeIdState
        try:
            response = await self._maveoStick.maveoBox.async_send_command(
                "Integrations.GetStateValue", params
            )
            if response is None:
                raise RuntimeError("Nymea returned no cover state")
            value: str = response["params"]["value"]
            self._maveoStick.state = State[value]
            self._available = True
        except Exception as ex:
            self._available = False
            # This is logging, so use % formatting.
            _LOGGER.error(
                "Error fetching initial state for %s: %s", self._maveoStick.id, ex
            )

    async def async_will_remove_from_hass(self) -> None:
        """Entity being removed from hass."""
        # The opposite of async_added_to_hass. Remove any registered call backs here.
        self._maveoStick.remove_callback(self.async_write_ha_state)

    @property
    def device_info(self) -> DeviceInfo:
        """Information about this entity/device."""
        return {
            "identifiers": {(DOMAIN, self._maveoStick.id)},
            "name": "maveo Stick",
            "model": "maveo Stick",
            "sw_version": self._maveoStick.firmware_version,
            "manufacturer": "Marantec",
        }

    @property
    def is_closed(self) -> bool:
        """Return if the cover is closed."""
        return (
            self._maveoStick.state == State.closed
            or self._maveoStick.state == State.intermediate
        )

    @property
    def is_closing(self) -> bool:
        """Return if the cover is closing or not."""
        return self._maveoStick.state == State.closing

    @property
    def is_opening(self) -> bool:
        """Return if the cover is opening or not."""
        return self._maveoStick.state == State.opening

    @property
    def available(self) -> bool:
        """Return the state of the sensor."""
        return self._available

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open the cover."""
        actionType_open: dict[str, Any] | None = next(
            (
                item
                for item in self._maveoStick.action_types
                if item.get("displayName") == "Open"
            ),
            None,
        )
        if actionType_open is None:
            raise RuntimeError("Nymea maveo stick has no Open action")

        params: dict[str, str] = {}
        params["actionTypeId"] = actionType_open["id"]  # type: ignore[index]
        params["thingId"] = self._maveoStick.id
        await self._maveoStick.maveoBox.async_send_command(
            "Integrations.ExecuteAction", params
        )

        self._maveoStick.state = State.opening
        await self._maveoStick.publish_updates()

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close the cover."""
        action_type_close: dict[str, Any] | None = next(
            (
                item
                for item in self._maveoStick.action_types
                if item.get("displayName") == "Close"
            ),
            None,
        )
        if action_type_close is None:
            raise RuntimeError("Nymea maveo stick has no Close action")

        params: dict[str, str] = {}
        params["actionTypeId"] = action_type_close["id"]
        params["thingId"] = self._maveoStick.id
        await self._maveoStick.maveoBox.async_send_command(
            "Integrations.ExecuteAction", params
        )

        self._maveoStick.state = State.closing
        await self._maveoStick.publish_updates()
