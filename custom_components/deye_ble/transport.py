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
import re

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


def validate_read_only_command(payload: bytes) -> None:
    """Reject everything except identification and a valid Modbus 0x03 read.

    Used by the single-phase monitoring profile before sending any command.
    """
    if payload == b"AT+DTYPE\n":
        return
    match = re.fullmatch(rb"AT\+INVDATA=8,([0-9A-F]{16})\n", payload)
    if match is None:
        raise DeyeBleError("read-only profile: command blocked before Bluetooth transmission")
    frame = bytes.fromhex(match[1].decode("ascii"))
    address = int.from_bytes(frame[2:4], "big")
    count = int.from_bytes(frame[4:6], "big")
    if (frame[:2] != b"\x01\x03" or p.crc16(frame[:-2]) != frame[-2:]
            or not 1 <= count <= 125 or address + count > 0x10000):
        raise DeyeBleError("read-only profile: invalid or non-read Modbus frame blocked")



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
        self._register_profile: str | None = None

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
        try:
            self._client = await establish_connection(
                BleakClient,
                self._device,
                self._name,
                max_attempts=CONNECT_ATTEMPTS,
            )
        except Exception as e:  # noqa: BLE001 — connect can raise many bleak errors
            raise DeyeBleError(f"connect failed: {e}") from e
        # start_notify can raise a raw bleak error. The GATT connection is ALREADY
        # open at this point, so on failure we MUST release it: the logger accepts
        # a single central and stops advertising while held, so a leaked link
        # wedges the inverter (and eats a proxy slot) until the ESP32 proxy is
        # restarted — an integration reload cannot undo it. Re-raise as
        # DeyeBleError so the coordinator's failure grace rides it out.
        try:
            await self._client.start_notify(NOTIFY_CHAR, self._on_notify)
        except Exception as e:  # noqa: BLE001 — notify can raise many bleak errors
            await self.disconnect()
            raise DeyeBleError(f"start_notify failed: {e}") from e

    async def disconnect(self) -> None:
        """Tear down the session, but never block on it.

        stop_notify and disconnect are best-effort: a flaky proxy link can leave
        either hanging, and if disconnect stalls it would hold the coordinator's
        BLE lock forever, wedging every subsequent poll and write until HA
        restarts. Bound both with a timeout and always drop the client. Each call
        is also shielded, so a poll cancelled mid-teardown still lets the release
        finish in the background instead of abandoning a half-open link.
        """
        client, self._client = self._client, None
        if client is None:
            return
        for coro, what in (
            (client.stop_notify(NOTIFY_CHAR), "stop_notify"),
            (client.disconnect(), "disconnect"),
        ):
            try:
                await asyncio.wait_for(asyncio.shield(coro), self._disconnect_timeout)
            except Exception:  # noqa: BLE001 — teardown is best-effort, never raises
                _LOGGER.debug("BLE %s did not complete cleanly", what, exc_info=True)

    def _on_notify(self, _char, data: bytearray) -> None:
        text = bytes(data).decode("ascii", errors="replace").strip()
        if self._reply is not None and not self._reply.done():
            self._reply.set_result(text)

    async def _command(self, payload: bytes) -> str:
        if self._register_profile == "single_phase":
            validate_read_only_command(payload)
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

    async def get_register_profile(self) -> str:
        """Identify the inverter, independently of the logger's DTYPE reply.

        Type 3 is the single-phase hybrid tested on SUN-6K-SG04LP1-EU.
        Other types retain the original register map and control behavior.
        """
        if self._register_profile is None:
            identity = await self.read(0, 3)
            if len(identity) != 3:
                raise DeyeBleError("incomplete inverter identification response")
            self._register_profile = "single_phase" if identity[0] == 0x003 else "legacy"
            _LOGGER.debug("Inverter type 0x%04X, protocol %d.%d, profile %s",
                          identity[0], identity[2] >> 8, identity[2] & 0xFF,
                          self._register_profile)
        return self._register_profile

    async def _check_control_support(self) -> None:
        # Identify on a fresh write session too, before sending any settings.
        # The logger's accepted handshake reply does not identify the inverter.
        if await self.get_register_profile() == "single_phase":
            raise DeyeBleError("single-phase profile supports monitoring only; controls are disabled")

    async def read(self, address: int, count: int) -> list[int]:
        reply = await self._command(p.wrap_read(p.build_read(address, count)))
        try:
            return p.parse_read(reply)
        except p.ProtocolError as e:
            raise DeyeBleError(str(e)) from e

    async def write_block(self, address: int, values: list[int]) -> None:
        """Write a contiguous register range in ONE frame (see build_write_block).

        Used by the clock commit, where three separate single-register frames
        corrupt the year byte on this hardware.
        """
        await self._check_control_support()
        request = p.build_write_block(address, values)
        reply = await self._command(p.wrap_write(request))
        try:
            acked = p.parse_write_ack(reply, request)
        except p.ProtocolError as e:
            raise DeyeBleError(str(e)) from e
        if not acked:
            raise DeyeBleError(
                f"block write to 0x{address:04X} not acked: {reply!r}"
            )

    async def write(self, address: int, value: int) -> None:
        """Write a register and verify the ack echoes the address + quantity."""
        await self._check_control_support()
        request = p.build_write(address, value)
        reply = await self._command(p.wrap_write(request))
        try:
            acked = p.parse_write_ack(reply, request)
        except p.ProtocolError as e:
            raise DeyeBleError(str(e)) from e
        if not acked:
            raise DeyeBleError(f"write to 0x{address:04X} not acked: {reply!r}")
