"""Test nymea hub transport and background-task lifecycle."""

import asyncio
import socket
import ssl
from threading import Thread
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import HomeAssistant
import pytest

from custom_components.nymea.maveo_box import MaveoBox


async def test_connection_uses_executor() -> None:
    """Test connectivity probing never runs its socket call in the event loop."""
    hass = MagicMock()
    hass.async_add_executor_job = AsyncMock(return_value=True)
    hub = MaveoBox(hass, "192.0.2.1", 2222)

    assert await hub.test_connection()
    hass.async_add_executor_job.assert_awaited_once_with(hub._test_connection)


async def test_listener_start_and_stop_are_idempotent(
    hass: HomeAssistant,
) -> None:
    """Test one config-entry task is created and can be stopped repeatedly."""
    hub = MaveoBox(hass, "192.0.2.1", 2222)
    listener_started = asyncio.Event()
    keep_running = asyncio.Event()

    async def listener() -> None:
        listener_started.set()
        await keep_running.wait()

    entry = MagicMock()
    entry.async_create_background_task.side_effect = lambda task_hass, coro, name: (
        task_hass.async_create_task(coro)
    )

    with patch.object(hub, "_websocket_listener", side_effect=listener):
        hub.start_notification_listener(entry)
        task = hub._ws_task
        hub.start_notification_listener(entry)
        await listener_started.wait()

        entry.async_create_background_task.assert_called_once()
        assert hub._ws_task is task

        await hub.stop_notification_listener()
        await hub.stop_notification_listener()

    assert task is not None
    assert task.cancelled()
    assert hub._ws_task is None


async def test_listener_reconnects_with_bounded_backoff() -> None:
    """Test failed WebSocket attempts back off instead of spinning."""
    hass = MagicMock()
    hub = MaveoBox(hass, "192.0.2.1", 2222)
    hub._listen_websocket_once = AsyncMock(side_effect=[False, False])
    delays: list[int] = []

    async def record_sleep(delay: int) -> None:
        delays.append(delay)
        if len(delays) == 2:
            hub._stop_notification_listener = True

    with patch("custom_components.nymea.maveo_box.asyncio.sleep", record_sleep):
        await hub._websocket_listener()

    assert delays == [5, 10]
    assert hub._listen_websocket_once.await_count == 2
    assert not hub.online


async def test_plain_socket_is_closed_before_tls_fallback(
    hass: HomeAssistant,
) -> None:
    """Test a failed plaintext handshake is closed before the TLS attempt."""
    hub = MaveoBox(hass, "192.0.2.1", 2222)
    events: list[object] = []
    ssl_context = MagicMock()
    hello = {
        "params": {
            "initialSetupRequired": False,
            "authenticationRequired": False,
            "pushButtonAuthAvailable": False,
        }
    }

    def connect(context=None) -> None:
        events.append(("connect", context))

    def send_command(method, params):
        events.append(("send", method))
        if events.count(("send", "JSONRPC.Hello")) == 1:
            raise OSError("plaintext handshake failed")
        return hello

    with (
        patch.object(hub, "_connect_socket", side_effect=connect),
        patch.object(hub, "_close_socket", side_effect=lambda: events.append("close")),
        patch.object(hub, "_create_ssl_context", return_value=ssl_context),
        patch.object(hub, "send_command", side_effect=send_command),
        patch.object(hub, "_enable_notifications"),
    ):
        assert await hub.init_connection() is None

    assert events == [
        ("connect", None),
        ("send", "JSONRPC.Hello"),
        "close",
        ("connect", ssl_context),
        ("send", "JSONRPC.Hello"),
    ]


async def test_async_close_closes_listener_and_socket(
    hass: HomeAssistant,
) -> None:
    """Test the combined close operation really closes an OS socket."""
    hub = MaveoBox(hass, "192.0.2.1", 2222)
    hub.stop_notification_listener = AsyncMock()
    command_socket, peer_socket = socket.socketpair()
    hub._sock = command_socket

    try:
        await hub.async_close()
        await hub.async_close()
    finally:
        peer_socket.close()

    assert hub.stop_notification_listener.await_count == 2
    assert command_socket.fileno() == -1
    assert hub._sock is None


def test_connect_socket_closes_socket_after_connect_error(socket_enabled) -> None:
    """Test a real refused TCP connect cannot leak its local socket."""
    hass = MagicMock()
    socket_constructor = socket.socket
    reserved_socket = socket_constructor(socket.AF_INET, socket.SOCK_STREAM)
    reserved_socket.bind(("127.0.0.1", 0))
    refused_port = reserved_socket.getsockname()[1]
    hub = MaveoBox(hass, "127.0.0.1", refused_port)
    created_sockets: list[socket.socket] = []

    def create_real_socket(*args, **kwargs) -> socket.socket:
        command_socket = socket_constructor(*args, **kwargs)
        created_sockets.append(command_socket)
        return command_socket

    try:
        with (
            patch(
                "custom_components.nymea.maveo_box.socket.socket",
                side_effect=create_real_socket,
            ),
            pytest.raises(OSError),
        ):
            hub._connect_socket()
    finally:
        reserved_socket.close()

    assert created_sockets
    assert all(command_socket.fileno() == -1 for command_socket in created_sockets)
    assert hub._sock is None


def test_connect_socket_closes_socket_after_tls_error(socket_enabled) -> None:
    """Test a real failed TLS handshake cannot leak its TCP socket."""
    hass = MagicMock()
    socket_constructor = socket.socket
    server_socket = socket_constructor(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.bind(("127.0.0.1", 0))
    server_socket.listen()
    server_socket.settimeout(2)
    hub = MaveoBox(hass, "127.0.0.1", server_socket.getsockname()[1])
    hub._socket_timeout = 2
    created_sockets: list[socket.socket] = []

    def serve_plaintext() -> None:
        connection, _ = server_socket.accept()
        with connection:
            connection.sendall(b"HTTP/1.0 400 Bad Request\r\n\r\n")

    def create_real_socket(*args, **kwargs) -> socket.socket:
        command_socket = socket_constructor(*args, **kwargs)
        created_sockets.append(command_socket)
        return command_socket

    server_thread = Thread(target=serve_plaintext, daemon=True)
    server_thread.start()

    try:
        with (
            patch(
                "custom_components.nymea.maveo_box.socket.socket",
                side_effect=create_real_socket,
            ),
            pytest.raises(ssl.SSLError),
        ):
            hub._connect_socket(hub._create_ssl_context())
    finally:
        server_socket.close()
        server_thread.join(timeout=2)

    assert not server_thread.is_alive()
    assert created_sockets
    assert all(command_socket.fileno() == -1 for command_socket in created_sockets)
    assert hub._sock is None
