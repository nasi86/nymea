"""Nymea hub communication module using JSON-RPC and WebSocket."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import contextlib
import json
import logging
import socket
import ssl
from threading import Lock
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
import websockets

_LOGGER = logging.getLogger(__name__)

mutex: Lock = Lock()
WEBSOCKET_RECONNECT_MIN_DELAY = 5
WEBSOCKET_RECONNECT_MAX_DELAY = 60


class MaveoBox:
    """Maveo Box."""

    def __init__(
        self,
        hass: HomeAssistant,
        host: str,
        port: int,
        token: str | None = None,
        websocket_port: int = 4444,
    ) -> None:
        """Init maveo box."""
        self._host: str = host
        self._port: int = port
        self._ws_port: int = websocket_port
        self._hass: HomeAssistant = hass
        self._name: str = host
        self._id: str = host.lower()
        self._token: str | None = token
        self._pushButtonAuthAvailable: bool = False
        self._authenticationRequired: bool = True
        self._initialSetupRequired: bool = False
        self._commandId: int = 0

        self._sock: socket.socket | ssl.SSLSocket | None = None
        self._recv_buffer: bytes = b""
        self._socket_timeout: float = 10.0
        self._pairing_timeout: float = 35.0

        self._ws: Any = None
        self._ws_task: asyncio.Task[None] | None = None
        self._ws_use_ssl: bool | None = None

        self.maveoSticks: list[Any] = []
        self.things: list[Any] = []
        self.online: bool = True

        self.thing_classes: list[dict[str, Any]] = []
        self.vendors: dict[str, dict[str, Any]] = {}
        self.discovered_things: list[dict[str, Any]] = []

        self._notification_handlers: dict[
            str, list[Callable[[dict[str, Any]], None]]
        ] = {}
        self._stop_notification_listener: bool = False

    @property
    def hub_id(self) -> str:
        """ID for nymea hub."""
        return self._id

    async def test_connection(self) -> bool:
        """Tests initial connectivity during setup."""
        return await self._hass.async_add_executor_job(self._test_connection)

    def _test_connection(self) -> bool:
        """Test initial connectivity without blocking the event loop."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(5)
            try:
                sock.connect((self._host, self._port))
                return True
            except (TimeoutError, OSError):
                return False

    def _create_ssl_context(self) -> ssl.SSLContext:
        """Create SSL context with self-signed certificate support."""
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context

    async def init_connection(self) -> str | None:
        """Init JSON-RPC connection and authenticate; WS is not required here."""
        loop = self._hass.loop
        _LOGGER.debug(
            "Initializing nymea connection to %s:%s (websocket_port configured as %s)",
            self._host,
            self._port,
            self._ws_port,
        )

        await loop.run_in_executor(None, self._connect_socket)

        try:
            handshake_message = await loop.run_in_executor(
                None, self.send_command, "JSONRPC.Hello", {}
            )
        except Exception as ex:
            _LOGGER.debug(
                "Plain JSON-RPC handshake failed for %s:%s, retrying with SSL: %s",
                self._host,
                self._port,
                ex,
            )
            await loop.run_in_executor(None, self._close_socket)
            context = await loop.run_in_executor(None, self._create_ssl_context)
            await loop.run_in_executor(None, self._connect_socket, context)
            handshake_message = await loop.run_in_executor(
                None, self.send_command, "JSONRPC.Hello", {}
            )

        handshake_data = handshake_message["params"]
        self._initialSetupRequired = handshake_data["initialSetupRequired"]
        self._authenticationRequired = handshake_data["authenticationRequired"]
        self._pushButtonAuthAvailable = handshake_data["pushButtonAuthAvailable"]

        _LOGGER.debug(
            "Nymea hello for %s:%s -> auth_required=%s, initial_setup_required=%s, push_button_available=%s",
            self._host,
            self._port,
            self._authenticationRequired,
            self._initialSetupRequired,
            self._pushButtonAuthAvailable,
        )

        if not self._authenticationRequired:
            _LOGGER.warning(
                "Maveo box %s:%s allows unauthenticated requests; skipping authentication",
                self._host,
                self._port,
            )
            return None

        if self._initialSetupRequired:
            raise NotImplementedError(
                "An uninitialized maveo box is currently not supported"
            )

        if not self._pushButtonAuthAvailable:
            raise NotImplementedError(
                "A maveo box without push button is currently not supported"
            )

        if self._authenticationRequired and self._token is None:
            self._token = await loop.run_in_executor(
                None, self._pushbuttonAuthentication
            )

        self._enable_notifications()
        return self._token

    def _connect_socket(self, ssl_context: ssl.SSLContext | None = None) -> None:
        """Create and connect the command socket with sane timeouts."""
        self._close_socket()
        command_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            command_socket.settimeout(self._socket_timeout)
            command_socket.connect((self._host, self._port))
            if ssl_context is not None:
                command_socket = ssl_context.wrap_socket(command_socket)
                command_socket.settimeout(self._socket_timeout)
        except Exception:
            command_socket.close()
            raise

        self._sock = command_socket
        self._recv_buffer = b""

    def _close_socket(self) -> None:
        """Close and discard the command socket."""
        command_socket = self._sock
        self._sock = None
        self._recv_buffer = b""
        if command_socket is None:
            return

        with contextlib.suppress(OSError):
            command_socket.shutdown(socket.SHUT_RDWR)
        with contextlib.suppress(OSError):
            command_socket.close()

    def _command_socket(self) -> socket.socket | ssl.SSLSocket:
        """Return the active command socket."""
        if self._sock is None:
            raise RuntimeError("command socket is not connected")
        return self._sock

    def _recv_json_line(self) -> dict[str, Any]:
        """Receive one JSON line from the command socket."""
        while b"\n" not in self._recv_buffer:
            chunk = self._command_socket().recv(4096)
            if chunk == b"":
                raise RuntimeError("socket connection broken")
            self._recv_buffer += chunk

        line, _, remainder = self._recv_buffer.partition(b"\n")
        self._recv_buffer = remainder

        while line.strip() == b"":
            if b"\n" not in self._recv_buffer:
                chunk = self._command_socket().recv(4096)
                if chunk == b"":
                    raise RuntimeError("socket connection broken")
                self._recv_buffer += chunk
            line, _, remainder = self._recv_buffer.partition(b"\n")
            self._recv_buffer = remainder

        return json.loads(line.decode("utf-8"))

    def _pushbuttonAuthentication(self) -> str | None:
        """Authenticate using push button method."""
        if self._token is not None:
            return self._token

        _LOGGER.info(
            "Using push button authentication method for %s:%s over JSON-RPC",
            self._host,
            self._port,
        )

        command_id = self._commandId
        params: dict[str, str] = {"deviceName": "home assistant"}
        command_obj: dict[str, Any] = {
            "id": command_id,
            "params": params,
            "method": "JSONRPC.RequestPushButtonAuth",
        }

        command = json.dumps(command_obj) + "\n"
        self._command_socket().sendall(command.encode("utf-8"))

        response_id = -1
        while response_id != command_id:
            response = self._recv_json_line()
            if "notification" in response:
                _LOGGER.debug(
                    "Ignoring notification on command socket during auth init: %s",
                    response["notification"],
                )
                continue
            response_id = response["id"]

        self._commandId = command_id + 1

        _LOGGER.info(
            "Push button authentication initialized for %s:%s; waiting up to %.1fs for confirmation",
            self._host,
            self._port,
            self._pairing_timeout,
        )

        deadline = time.monotonic() + self._pairing_timeout
        while time.monotonic() < deadline:
            try:
                response = self._recv_json_line()
            except TimeoutError:
                _LOGGER.debug(
                    "Still waiting for push button auth confirmation from %s:%s",
                    self._host,
                    self._port,
                )
                continue

            if ("notification" in response) and response[
                "notification"
            ] == "JSONRPC.PushButtonAuthFinished":
                _LOGGER.info(
                    "Push button auth finished notification received from %s:%s",
                    self._host,
                    self._port,
                )
                if response["params"].get("success") is True:
                    _LOGGER.info(
                        "Authenticated successfully via push button for %s:%s",
                        self._host,
                        self._port,
                    )
                    return response["params"].get("token")
                raise RuntimeError("push button authentication failed")

        raise TimeoutError("push button authentication timed out")

    def _enable_notifications(self) -> None:
        """Enable notifications for relevant namespaces."""
        _LOGGER.info(
            "Notifications are enabled by default after authentication; WebSocket listener can be started later on port %s",
            self._ws_port,
        )

    def send_command(
        self, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        """Send a command via JSON-RPC socket and wait for response."""
        with mutex:
            command_obj: dict[str, Any] = {"id": self._commandId, "method": method}
            command_id: int = self._commandId
            self._commandId += 1

            if self._authenticationRequired and self._token is not None:
                command_obj["token"] = self._token

            if params is not None and len(params) > 0:
                command_obj["params"] = params

            command: str = json.dumps(command_obj) + "\n"
            self._command_socket().sendall(command.encode("utf-8"))

            responseId: int = -1
            while responseId != command_id:
                response: dict[str, Any] = self._recv_json_line()
                if "notification" in response:
                    _LOGGER.warning(
                        "Received notification on command socket: %s",
                        response["notification"],
                    )
                    continue
                responseId = response["id"]

            if response["status"] != "success":
                _LOGGER.error("JSON error happened: %s", response.get("error"))
                return None

            return response

    async def async_send_command(
        self, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        """Run a JSON-RPC command without blocking Home Assistant's event loop."""
        return await self._hass.async_add_executor_job(
            self.send_command, method, params
        )

    async def _websocket_listener(self) -> None:
        """Keep the WebSocket notification listener connected with backoff."""
        reconnect_delay = WEBSOCKET_RECONNECT_MIN_DELAY

        while not self._stop_notification_listener:
            connected = await self._listen_websocket_once()
            if self._stop_notification_listener:
                return

            self.online = False
            if connected:
                reconnect_delay = WEBSOCKET_RECONNECT_MIN_DELAY

            _LOGGER.warning(
                "Nymea WebSocket disconnected from %s:%s; reconnecting in %ss",
                self._host,
                self._ws_port,
                reconnect_delay,
            )
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, WEBSOCKET_RECONNECT_MAX_DELAY)

    async def _listen_websocket_once(self) -> bool:
        """Connect once, detecting TLS only until a transport has worked."""
        transports = (False, True) if self._ws_use_ssl is None else (self._ws_use_ssl,)

        for use_ssl in transports:
            scheme = "wss" if use_ssl else "ws"
            ws_url = f"{scheme}://{self._host}:{self._ws_port}"
            ssl_context = None
            if use_ssl:
                ssl_context = await self._hass.async_add_executor_job(
                    self._create_ssl_context
                )

            try:
                async with websockets.connect(ws_url, ssl=ssl_context) as websocket:
                    connected = await self._ws_listen_loop(websocket)
                    if connected:
                        self._ws_use_ssl = use_ssl
                    return connected
            except (websockets.exceptions.WebSocketException, OSError) as ex:
                _LOGGER.debug(
                    "Nymea WebSocket %s connection to %s:%s failed: %s",
                    scheme,
                    self._host,
                    self._ws_port,
                    ex,
                )

        return False

    async def _ws_listen_loop(self, websocket: Any) -> bool:
        """Main WebSocket listening loop."""
        connected = False
        try:
            hello_message: dict[str, Any] = {
                "id": 0,
                "method": "JSONRPC.Hello",
                "params": {},
            }
            await websocket.send(json.dumps(hello_message))
            hello_response: dict[str, Any] = json.loads(await websocket.recv())

            if hello_response.get("status") != "success":
                _LOGGER.error("WebSocket handshake failed: %s", hello_response)
                return False

            _LOGGER.debug(
                "WebSocket handshake successful: %s", hello_response.get("params", {})
            )

            if self._authenticationRequired and self._token:
                auth_hello = {
                    "id": 1,
                    "method": "JSONRPC.Hello",
                    "params": {},
                    "token": self._token,
                }
                await websocket.send(json.dumps(auth_hello))
                auth_response = json.loads(await websocket.recv())

                if auth_response.get("status") != "success":
                    _LOGGER.error(
                        "WebSocket token authentication failed: %s", auth_response
                    )
                    return False

                _LOGGER.debug("WebSocket authenticated with token")

            enable_notifications = {
                "id": 2,
                "method": "JSONRPC.SetNotificationStatus",
                "params": {"enabled": True},
            }

            if self._authenticationRequired and self._token:
                enable_notifications["token"] = self._token

            await websocket.send(json.dumps(enable_notifications))
            notif_response = json.loads(await websocket.recv())
            if notif_response.get("status") == "success":
                _LOGGER.debug("WebSocket notifications enabled")
            else:
                _LOGGER.warning("Failed to enable notifications: %s", notif_response)
                return False

            connected = True
            self.online = True

            while not self._stop_notification_listener:
                try:
                    message_str = await asyncio.wait_for(websocket.recv(), timeout=1.0)
                    message = json.loads(message_str)

                    if "notification" in message:
                        notification_name = message["notification"]
                        params = message.get("params", {})
                        _LOGGER.debug(
                            "WebSocket notification: %s with params: %s",
                            notification_name,
                            params,
                        )

                        if notification_name in self._notification_handlers:
                            for handler in self._notification_handlers[
                                notification_name
                            ]:
                                try:
                                    self._hass.loop.call_soon_threadsafe(
                                        handler, params
                                    )
                                except Exception as ex:
                                    _LOGGER.error(
                                        "Error calling notification handler: %s", ex
                                    )
                        else:
                            _LOGGER.debug(
                                "No handler registered for: %s", notification_name
                            )
                    else:
                        _LOGGER.debug(
                            "Received command response on WebSocket: %s", message
                        )

                except TimeoutError:
                    continue
                except websockets.exceptions.ConnectionClosed:
                    break
                except Exception as ex:
                    _LOGGER.exception("Error in WebSocket listener: %s", ex)
                    break

        except Exception as ex:
            _LOGGER.exception("WebSocket listen loop error: %s", ex)
        finally:
            _LOGGER.debug("WebSocket notification listener stopped")

        return connected

    def register_notification_handler(
        self, notification_name: str, handler: Callable[[dict[str, Any]], None]
    ) -> None:
        """Register a handler for a specific notification type."""
        if notification_name not in self._notification_handlers:
            self._notification_handlers[notification_name] = []
        self._notification_handlers[notification_name].append(handler)
        _LOGGER.debug("Registered handler for notification: %s", notification_name)

    def unregister_notification_handler(
        self, notification_name: str, handler: Callable[[dict[str, Any]], None]
    ) -> None:
        """Unregister a notification handler."""
        if notification_name in self._notification_handlers:
            self._notification_handlers[notification_name].remove(handler)
            if not self._notification_handlers[notification_name]:
                del self._notification_handlers[notification_name]
            _LOGGER.debug(
                "Unregistered handler for notification: %s", notification_name
            )

    def start_notification_listener(self, entry: ConfigEntry) -> None:
        """Start the WebSocket notification listener."""
        if self._ws_task is None or self._ws_task.done():
            self._stop_notification_listener = False
            self._ws_task = entry.async_create_background_task(
                self._hass,
                self._websocket_listener(),
                f"nymea websocket listener for {self._host}",
            )
            _LOGGER.debug("Scheduled WebSocket notification listener task")

    async def stop_notification_listener(self) -> None:
        """Stop the WebSocket notification listener."""
        self._stop_notification_listener = True
        task = self._ws_task
        if task is None:
            return

        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._ws_task = None

    async def async_close(self) -> None:
        """Stop background work and close the command connection."""
        try:
            await self.stop_notification_listener()
        finally:
            await self._hass.async_add_executor_job(self._close_socket)

    def get_thing_class_name(self, thingclass_id: str) -> str | None:
        """Get the display name of a thing class by its ID."""
        for thing_class in self.thing_classes:
            if thing_class.get("id") == thingclass_id:
                return thing_class.get("displayName")
        return None

    async def discover_and_log_all_things(self) -> None:
        """Load one discovery snapshot for all entity platforms."""
        vendors_response = await self.async_send_command("Integrations.GetVendors")
        thing_classes_response = await self.async_send_command(
            "Integrations.GetThingClasses"
        )
        things_response = await self.async_send_command("Integrations.GetThings")

        if not vendors_response or not thing_classes_response or not things_response:
            raise RuntimeError("Nymea discovery returned an empty response")

        vendors = vendors_response.get("params", {}).get("vendors", [])
        self.vendors = {vendor["id"]: vendor for vendor in vendors}
        self.thing_classes = thing_classes_response.get("params", {}).get(
            "thingClasses", []
        )
        self.discovered_things = things_response.get("params", {}).get("things", [])

        _LOGGER.debug(
            "Nymea discovery snapshot: %d vendors, %d thing classes, %d things",
            len(self.vendors),
            len(self.thing_classes),
            len(self.discovered_things),
        )
