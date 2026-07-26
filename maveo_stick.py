"""Support for Maveo stick."""

from __future__ import annotations

from enum import Enum
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from .maveo_box import MaveoBox

_LOGGER = logging.getLogger(__name__)

State = Enum(
    "State", ["unknown", "open", "closed", "opening", "closing", "intermediate"]
)


class MaveoStick:
    """Represents a Maveo Stick attached to the garage door drive and connected to the maveo box."""

    manufacturer: str = "maveo"
    thingclassid: str = "ca6baab8-3708-4478-8ca2-7d4d6d542937"

    def __init__(
        self,
        thingid: str,
        name: str,
        version: str,
        maveoBox: MaveoBox,
        state_type_id: str,
        action_types: list[dict[str, Any]],
        initial_state: str | None = None,
    ) -> None:
        """Init stick."""
        self._id: str = thingid
        self.name: str = name
        self.firmware_version: str = version
        self.maveoBox: MaveoBox = maveoBox
        self.state_type_id = state_type_id
        self.action_types = action_types
        self._callbacks: set[Callable[[], None]] = set()
        self.state = (
            State[initial_state]
            if initial_state in State.__members__
            else State.unknown
        )

        # Register for state change notifications.
        self._register_for_notifications()

    def _register_for_notifications(self) -> None:
        """Register to receive state change notifications for this thing."""
        # Register handler for Integrations.StateChanged notifications.
        self.maveoBox.register_notification_handler(
            "Integrations.StateChanged", self._handle_state_changed
        )

    def _handle_state_changed(self, params: dict[str, Any]) -> None:
        """Handle state change notification from Nymea."""
        # Check if this notification is for this specific thing.
        thing_id = params.get("thingId")
        if thing_id != self._id:
            return

        # Get the value.
        value = params.get("value")

        # We only care about the "State" state type (need to check if it's the right one)
        # For now, update the state if we get any state change for this thing
        try:
            if value in State.__members__:
                old_state = self.state
                self.state = State[value]
                if old_state != self.state:
                    # This is logging, so use % formatting.
                    _LOGGER.info(
                        "MaveoStick %s state changed from %s to %s (via notification)",
                        self.name,
                        old_state.name,
                        self.state.name,
                    )
                    # Publish updates to Home Assistant.
                    self.maveoBox._hass.loop.call_soon_threadsafe(
                        self.maveoBox._hass.async_create_task, self.publish_updates()
                    )
        except Exception as ex:
            # This is logging, so use % formatting.
            _LOGGER.error("Error handling state change notification: %s", ex)

    def unregister_notifications(self) -> None:
        """Unregister from state change notifications."""
        self.maveoBox.unregister_notification_handler(
            "Integrations.StateChanged", self._handle_state_changed
        )

    @property
    def id(self) -> str:
        """Return ID for maveo stick."""
        return self._id

    def register_callback(self, callback: Callable[[], None]) -> None:
        """Register callback, called when MaveoStick changes state."""
        self._callbacks.add(callback)

    def remove_callback(self, callback: Callable[[], None]) -> None:
        """Remove previously registered callback."""
        self._callbacks.discard(callback)

    async def publish_updates(self) -> None:
        """Schedule call all registered callbacks."""
        for callback in self._callbacks:
            callback()

    @staticmethod
    async def add(maveoBox: MaveoBox):
        """Add all maveo sticks from the shared discovery snapshot."""
        thing_class = next(
            (
                item
                for item in maveoBox.thing_classes
                if item.get("id") == MaveoStick.thingclassid
            ),
            None,
        )
        if thing_class is None:
            return

        state_types = thing_class.get("stateTypes", [])

        statetype_version = next(
            (
                item
                for item in state_types
                if item.get("displayName") == "maveo-stick version"
            ),
            None,
        )
        statetype_state = next(
            (item for item in state_types if item.get("displayName") == "State"),
            None,
        )
        if statetype_state is None:
            _LOGGER.warning("Maveo stick thing class has no State state type")
            return

        for thing in maveoBox.discovered_things:
            if thing.get("thingClassId") != MaveoStick.thingclassid:
                continue

            states = {
                state.get("stateTypeId"): state.get("value")
                for state in thing.get("states", [])
            }
            version = (
                states.get(statetype_version.get("id"), "unknown")
                if statetype_version
                else "unknown"
            )
            maveoBox.maveoSticks.append(
                MaveoStick(
                    thing["id"],
                    thing["name"],
                    version,
                    maveoBox,
                    statetype_state["id"],
                    thing_class.get("actionTypes", []),
                    states.get(statetype_state["id"]),
                )
            )
