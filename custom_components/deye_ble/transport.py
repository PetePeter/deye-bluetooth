"""Bleak I/O layer for the Deye logger BLE local protocol.

This is the only module that touches hardware. It owns the GATT connection,
enables notifications, sends AT commands, and awaits the matching notification.
All byte construction / parsing is delegated to the pure `protocol` module so
the wire logic can be tested without a radio.

One BLE central at a time: the phone app and HA cannot both connect.
"""
from __future__ import annotations

import asyncio
import logging

from bleak import BleakClient
from bleak_retry_connector import establish_connection

from . import protocol as p

_LOGGER = logging.getLogger(__name__)

WRITE_CHAR = "0000fec7-0000-1000-8000-00805f9b34fb"   # write-with-response
NOTIFY_CHAR = "0000fed8-0000-1000-8000-00805f9b34fb"  # notify (CCCD enabled by bleak)

DEFAULT_TIMEOUT = 10.0  # seconds to await a notification reply
CONNECT_ATTEMPTS = 3    # establish_connection retries transient proxy failures
# A hung disconnect on a flaky proxy link must never hold the caller (and thus
# the coordinator's BLE lock) open — bound it, then drop the client regardless.
DISCONNECT_TIMEOUT = 10.0


class DeyeBleError(Exception):
    """Connection lost, timed out, or the logger rejected a command."""


class DeyeBleTransport:
    """A single AT-command request/response session over GATT.

    Usage:
        async with DeyeBleTransport(ble_device) as t:
            await t.handshake()
            regs = await t.read(0x008F, 1)
            await t.write(0x008F, 100)
    """

    def __init__(self, ble_device, timeout: float = DEFAULT_TIMEOUT):
        self._device = ble_device
        # For establish_connection logging; BLEDevice exposes name/address.
        self._name = getattr(ble_device, "name", None) or getattr(
            ble_device, "address", "deye"
        )
        self._timeout = timeout
        self._disconnect_timeout = DISCONNECT_TIMEOUT
        self._client: BleakClient | None = None
        self._reply: asyncio.Future[str] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    async def __aenter__(self) -> "DeyeBleTransport":
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.disconnect()

    async def connect(self) -> None:
        self._loop = asyncio.get_running_loop()
        # Use bleak-retry-connector — the resilient path HA expects. It routes
        # through the ESP32 proxies, retries transient failures, and handles the
        # reconnect churn that raw BleakClient.connect() does not.
        self._client = await establish_connection(
            BleakClient,
            self._device,
            self._name,
            max_attempts=CONNECT_ATTEMPTS,
        )
        await self._client.start_notify(NOTIFY_CHAR, self._on_notify)

    async def disconnect(self) -> None:
        """Tear down the session, but never block on it.

        stop_notify and disconnect are best-effort: a flaky proxy link can leave
        either hanging, and if disconnect stalls it would hold the coordinator's
        BLE lock forever, wedging every subsequent poll and write until HA
        restarts. Bound both with a timeout and always drop the client.
        """
        client, self._client = self._client, None
        if client is None:
            return
        try:
            await asyncio.wait_for(
                client.stop_notify(NOTIFY_CHAR), self._disconnect_timeout
            )
        except Exception:  # noqa: BLE001 — best-effort on teardown
            pass
        try:
            await asyncio.wait_for(
                client.disconnect(), self._disconnect_timeout
            )
        except Exception:  # noqa: BLE001 — a hung/failed disconnect must not wedge us
            _LOGGER.debug("BLE disconnect did not complete cleanly", exc_info=True)

    def _on_notify(self, _char, data: bytearray) -> None:
        text = bytes(data).decode("ascii", errors="replace").strip()
        if self._reply is not None and not self._reply.done():
            self._reply.set_result(text)

    async def _command(self, payload: bytes) -> str:
        if self._client is None or self._loop is None:
            raise DeyeBleError("not connected")
        self._reply = self._loop.create_future()
        await self._client.write_gatt_char(WRITE_CHAR, payload, response=True)
        try:
            return await asyncio.wait_for(self._reply, self._timeout)
        except asyncio.TimeoutError as e:
            raise DeyeBleError(f"no reply to {payload!r}") from e
        finally:
            self._reply = None

    async def handshake(self) -> None:
        reply = await self._command(b"AT+DTYPE\n")
        if not p.is_handshake_ack(reply):
            raise DeyeBleError(f"unexpected handshake reply: {reply!r}")

    async def read(self, address: int, count: int) -> list[int]:
        reply = await self._command(p.wrap_read(p.build_read(address, count)))
        try:
            return p.parse_read(reply)
        except p.ProtocolError as e:
            raise DeyeBleError(str(e)) from e

    async def write(self, address: int, value: int) -> None:
        """Write a register and verify the ack echoes the address + quantity."""
        request = p.build_write(address, value)
        reply = await self._command(p.wrap_write(request))
        try:
            acked = p.parse_write_ack(reply, request)
        except p.ProtocolError as e:
            raise DeyeBleError(str(e)) from e
        if not acked:
            raise DeyeBleError(f"write to 0x{address:04X} not acked: {reply!r}")
