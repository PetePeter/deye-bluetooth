"""Transport resilience tests — the disconnect must never wedge the BLE lock.

A hung ``BleakClient.disconnect()`` on a flaky ESP32-proxy link used to hold the
coordinator's ``_ble_lock`` forever (the ``async with transport`` teardown never
returned), leaving every entity ``unavailable`` until HA restarted. These tests
pin the bounded-teardown contract that prevents that wedge.

Fakes ``establish_connection`` so no radio/bleak backend is needed.
"""
from __future__ import annotations

import asyncio

import pytest

from custom_components.deye_ble import transport as t
from custom_components.deye_ble import protocol as p


class FakeBleakClient:
    """Minimal BleakClient stand-in with controllable teardown behaviour."""

    def __init__(
        self, *, disconnect_hangs: bool = False, start_notify_raises: bool = False,
        reply: str | None = None,
        identity: tuple[int, int, int] = (5, 1, 0x0201),
    ):
        self._disconnect_hangs = disconnect_hangs
        self._start_notify_raises = start_notify_raises
        self._reply = reply
        self._identity = identity
        self.written: list[bytes] = []
        self._notify_cb = None
        self.start_notify_called = False
        self.stop_notify_called = False
        self.disconnect_called = False

    async def start_notify(self, _char, _cb) -> None:
        self.start_notify_called = True
        self._notify_cb = _cb
        if self._start_notify_raises:
            raise RuntimeError("br-connection-canceled")

    async def write_gatt_char(self, _char, payload, response=True) -> None:
        """Record the AT payload and answer with the canned reply."""
        self.written.append(bytes(payload))
        reply = self._reply
        if payload == p.wrap_read(p.build_read(0, 3)):
            body = bytes((1, 3, 6)) + b"".join(word.to_bytes(2, "big") for word in self._identity)
            reply = "+ok=" + (body + p.crc16(body)).hex().upper()
        if reply is not None and self._notify_cb is not None:
            self._notify_cb(None, bytearray(reply.encode()))

    async def stop_notify(self, _char) -> None:
        self.stop_notify_called = True

    async def disconnect(self) -> None:
        self.disconnect_called = True
        if self._disconnect_hangs:
            await asyncio.Event().wait()  # never resolves


@pytest.fixture
def patch_connect(monkeypatch):
    """Return a helper that wires a given FakeBleakClient into connect()."""

    def _install(client: FakeBleakClient) -> None:
        async def _fake_establish(_cls, _device, _name, max_attempts=0):
            return client

        monkeypatch.setattr(t, "establish_connection", _fake_establish)

    return _install


@pytest.mark.asyncio
async def test_disconnect_returns_when_client_disconnect_hangs(patch_connect):
    # The wedge fix: a disconnect that never completes must not block the caller
    # (and therefore must not hold the coordinator's BLE lock) indefinitely.
    client = FakeBleakClient(disconnect_hangs=True)
    patch_connect(client)

    transport = DeyeBleTransport_with_short_timeout(client_timeout=0.05)
    await transport.connect()

    await asyncio.wait_for(transport.disconnect(), timeout=1.0)

    assert client.disconnect_called is True
    assert transport._client is None  # cleared even though the disconnect hung


@pytest.mark.asyncio
async def test_disconnect_calls_client_disconnect_on_happy_path(patch_connect):
    # The timeout wrapper must not skip the real teardown on a healthy link.
    client = FakeBleakClient()
    patch_connect(client)

    transport = t.DeyeBleTransport(ble_device=object())
    await transport.connect()
    await transport.disconnect()

    assert client.stop_notify_called is True
    assert client.disconnect_called is True
    assert transport._client is None


@pytest.mark.asyncio
async def test_connect_releases_link_when_start_notify_fails(patch_connect):
    # The leak fix: establish_connection has already opened the GATT link, so a
    # start_notify failure must release it. Otherwise the abandoned connection
    # holds the logger's single central slot, the logger stops advertising, and
    # no amount of reloading the integration recovers it — only a proxy restart.
    client = FakeBleakClient(start_notify_raises=True)
    patch_connect(client)

    transport = t.DeyeBleTransport(ble_device=object())

    with pytest.raises(t.DeyeBleError):
        await transport.connect()

    assert client.disconnect_called is True
    assert transport._client is None


@pytest.mark.asyncio
async def test_connect_raises_deye_error_when_establish_connection_fails(monkeypatch):
    # Bleak's own exception types must not leak past the transport: the
    # coordinator's failure grace keys off DeyeBleError.
    async def _boom(_cls, _device, _name, max_attempts=0):
        raise RuntimeError("no route to proxy")

    monkeypatch.setattr(t, "establish_connection", _boom)
    transport = t.DeyeBleTransport(ble_device=object())

    with pytest.raises(t.DeyeBleError):
        await transport.connect()

    assert transport._client is None


def DeyeBleTransport_with_short_timeout(*, client_timeout: float):
    """Build a transport whose disconnect timeout is short, so the hang test is
    fast. Kept as a helper so the production default stays untouched."""
    transport = t.DeyeBleTransport(ble_device=object())
    transport._disconnect_timeout = client_timeout
    return transport


# --- Contiguous block write -------------------------------------------------
# The clock is written as ONE frame because three single-register frames corrupt
# the year byte on this hardware (see protocol.build_write_block).

@pytest.mark.asyncio
async def test_block_write_sends_one_frame_and_accepts_the_ack(patch_connect):
    from custom_components.deye_ble import protocol as p

    values = [0x1A08, 0x0F10, 0x2B00]
    request = p.build_write_block(0x003E, values)
    # A GENUINE ack: 8 bytes, no payload. Echoing the request back would not
    # exercise the real response shape.
    body = bytes((p.SLAVE, p.FUNC_WRITE, 0x00, 0x3E, 0x00, 0x03))
    client = FakeBleakClient(reply="+ok=" + (body + p.crc16(body)).hex().upper())
    patch_connect(client)

    async with t.DeyeBleTransport(object()) as transport:
        await transport.write_block(0x003E, values)

    at_frames = [w for w in client.written if w.startswith(b"AT+INVDATA=")
                 and w.split(b",", 1)[1].startswith(b"0110")]
    assert len(at_frames) == 1, "the clock must go out as a single frame"
    assert request.hex().upper().encode() in at_frames[0]


@pytest.mark.asyncio
async def test_block_write_raises_when_the_ack_does_not_match(patch_connect):
    # An unacked write must surface. It cannot confirm the VALUES — the ack
    # echoes only address and quantity — but a missing or mismatched ack means
    # the frame did not land at all, and silently continuing would leave the
    # sequence believing it had written a clock it never wrote.
    from custom_components.deye_ble import protocol as p

    body = bytes((p.SLAVE, p.FUNC_WRITE, 0x00, 0x40, 0x00, 0x03))  # wrong address
    client = FakeBleakClient(reply="+ok=" + (body + p.crc16(body)).hex().upper())
    patch_connect(client)

    async with t.DeyeBleTransport(object()) as transport:
        with pytest.raises(t.DeyeBleError, match="not acked"):
            await transport.write_block(0x003E, [0x1A08, 0x0F10, 0x2B00])


@pytest.mark.asyncio
@pytest.mark.parametrize("device_type,expected", [(3, "single_phase"), (5, "legacy")])
async def test_profile_uses_inverter_type_and_caches_identity(patch_connect, device_type, expected):
    client = FakeBleakClient(reply="+ok=21511,21511", identity=(device_type, 1, 0x0201))
    patch_connect(client)
    async with t.DeyeBleTransport(object()) as transport:
        await transport.handshake()
        assert await transport.get_register_profile() == expected
        assert await transport.get_register_profile() == expected
    assert client.written == [b"AT+DTYPE\n", p.wrap_read(p.build_read(0, 3))]


@pytest.mark.asyncio
@pytest.mark.parametrize("block", [False, True])
async def test_single_phase_control_blocked_even_on_fresh_session(patch_connect, block):
    client = FakeBleakClient(identity=(3, 1, 0x0201))
    patch_connect(client)
    async with t.DeyeBleTransport(object()) as transport:
        with pytest.raises(t.DeyeBleError, match="monitoring only"):
            if block:
                await transport.write_block(62, [1, 2, 3])
            else:
                await transport.write(143, 100)
    # Identification is a read; no function-10 write reached the fake hardware.
    assert client.written == [p.wrap_read(p.build_read(0, 3))]


@pytest.mark.asyncio
async def test_single_phase_rejects_raw_control_command(patch_connect):
    client = FakeBleakClient(identity=(3, 1, 0x0201))
    patch_connect(client)
    async with t.DeyeBleTransport(object()) as transport:
        await transport.get_register_profile()
        with pytest.raises(t.DeyeBleError, match="read-only profile"):
            await transport._command(p.wrap_write(p.build_write(143, 100)))
    assert client.written == [p.wrap_read(p.build_read(0, 3))]


@pytest.mark.parametrize("payload", [
    b"AT+RESET\n", b"AT+DTYPE=1\n", b"AT+DTYPE\nAT+RESET\n",
    p.wrap_write(p.build_write(143, 100)),
    p.wrap_read(p.build_read(183, 0)), p.wrap_read(p.build_read(183, 126)),
    b"AT+INVDATA=8,010300B700020000\n",
])
def test_read_only_policy_rejects_invalid_or_setting_commands(payload):
    with pytest.raises(t.DeyeBleError, match="read-only profile"):
        t.validate_read_only_command(payload)


def test_read_only_policy_accepts_identification_and_valid_read():
    t.validate_read_only_command(b"AT+DTYPE\n")
    t.validate_read_only_command(p.wrap_read(p.build_read(183, 2)))


def test_read_only_policy_rejects_valid_crc_for_write_single_register():
    body = bytes.fromhex("0106008F0064")
    with pytest.raises(t.DeyeBleError, match="non-read"):
        t.validate_read_only_command(p.wrap_read(body + p.crc16(body)))
