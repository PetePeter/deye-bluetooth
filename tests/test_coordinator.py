"""P3 — coordinator tests (pure async_poll + SN validation).

Uses a FakeTransport that returns register words from the captured +ok= frames
in test_registers.py — no bleak, no homeassistant imports needed.
"""
from __future__ import annotations

import asyncio

import pytest

from custom_components.deye_ble import protocol as p
from custom_components.deye_ble import registers as r
from custom_components.deye_ble.helpers import async_poll, validate_logger_sn
from tests.test_registers import FRAMES


# --- Fake transport --------------------------------------------------------

class FakeTransport:
    """Duck-typed transport that returns pre-canned register words.

    handshake() succeeds by default.  read() returns the parsed words for a
    given block start from the FRAMES fixture.
    """

    def __init__(
        self,
        frames: dict[int, str] | None = None,
        *,
        fail_on_block: int | None = None,
    ):
        self._frames = frames or FRAMES
        self._fail_on_block = fail_on_block
        self.handshake_called = False
        self.reads: list[tuple[int, int]] = []

    async def __aenter__(self) -> "FakeTransport":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def handshake(self) -> None:
        if self._fail_on_block == -1:
            raise RuntimeError("handshake failed")
        self.handshake_called = True

    async def read(self, address: int, count: int) -> list[int]:
        if self._fail_on_block == address:
            raise RuntimeError(f"read failed at 0x{address:04X}")
        self.reads.append((address, count))
        return p.parse_read(self._frames[address])


# --- async_poll tests -------------------------------------------------------

@pytest.mark.asyncio
async def test_async_poll_returns_all_telemetry_keys():
    transport = FakeTransport()
    data = await async_poll(transport, with_config=False)

    # Must have all telemetry keys from a full READ_BLOCKS poll
    assert "solar_power" in data
    assert "battery_soc" in data
    assert "grid_power" in data
    assert "house_load" in data
    assert "battery_power" in data
    assert "inverter_power_l1" in data
    assert "inverter_power_l2" in data
    assert "inverter_power_l3" in data
    assert "battery_voltage" in data
    assert "battery_temp" in data
    assert "inverter_temp" in data


@pytest.mark.asyncio
async def test_async_poll_known_values():
    transport = FakeTransport()
    data = await async_poll(transport, with_config=False)

    # Spot-check against known decoded values from test_registers
    assert data["solar_power"] == 1783
    assert data["battery_soc"] == 46
    assert data["grid_power"] == -42
    assert data["house_load"] == 1309


@pytest.mark.asyncio
async def test_async_poll_with_config_includes_work_mode_and_max_sell():
    transport = FakeTransport()
    # Add a control block frame: work_mode=0 (Selling First), max_sell=100
    control_frames = dict(transport._frames)
    control_frames[0x008E] = "+ok=01030400000064FBD8"
    transport._frames = control_frames

    data = await async_poll(transport, with_config=True)

    assert data["work_mode"] == "Selling First"
    assert data["max_sell_power"] == 100


@pytest.mark.asyncio
async def test_async_poll_without_config_excludes_work_mode():
    transport = FakeTransport()
    data = await async_poll(transport, with_config=False)

    assert "work_mode" not in data
    assert "max_sell_power" not in data


@pytest.mark.asyncio
async def test_async_poll_calls_handshake():
    transport = FakeTransport()
    await async_poll(transport)
    assert transport.handshake_called is True


@pytest.mark.asyncio
async def test_async_poll_reads_all_blocks():
    transport = FakeTransport()
    await async_poll(transport, with_config=False)

    starts = [addr for addr, _ in transport.reads]
    for block_start, _ in r.READ_BLOCKS:
        assert block_start in starts


@pytest.mark.asyncio
async def test_async_poll_reads_control_blocks_when_with_config():
    transport = FakeTransport()
    control_frames = dict(transport._frames)
    control_frames[0x008E] = "+ok=01030400000064FBD8"
    transport._frames = control_frames

    await async_poll(transport, with_config=True)

    starts = [addr for addr, _ in transport.reads]
    for block_start, _ in r.READ_BLOCKS:
        assert block_start in starts
    for block_start, _ in r.CONTROL_BLOCKS:
        assert block_start in starts


@pytest.mark.asyncio
async def test_async_poll_propagates_handshake_failure():
    transport = FakeTransport(fail_on_block=-1)
    with pytest.raises(RuntimeError, match="handshake failed"):
        await async_poll(transport)


@pytest.mark.asyncio
async def test_async_poll_propagates_read_failure():
    transport = FakeTransport(fail_on_block=0x024A)
    with pytest.raises(RuntimeError, match="read failed"):
        await async_poll(transport)


@pytest.mark.asyncio
async def test_async_poll_config_block_failure_is_non_fatal():
    # A control-block read failure must NOT discard a good telemetry cycle.
    transport = FakeTransport(fail_on_block=0x008E)
    data = await async_poll(transport, with_config=True)
    assert data["solar_power"] == 1783      # telemetry still present
    assert "work_mode" not in data          # config omitted, not fabricated
    assert "max_sell_power" not in data


# --- SN validation tests ----------------------------------------------------

def test_real_logger_advert_name_is_valid_sn():
    # The Deye logger advertises its SN as the BLE name; config_flow auto-skips
    # the SN step when validate_logger_sn accepts the advert name.
    assert validate_logger_sn("DEYE00000001") == "DEYE00000001"


@pytest.mark.parametrize("good", [
    "A1234567",        # 8 chars
    "DEYELOGGER123",   # 13 chars
    "ABCDEF0123456789", # 16 chars
    "12345678901234567890", # 20 chars
    "a1b2c3d4",        # lowercase
    "  A1B2C3D4  ",    # whitespace trimmed
])
def test_validate_logger_sn_accepts(good):
    result = validate_logger_sn(good)
    assert result == good.strip().upper()


@pytest.mark.parametrize("bad", [
    "ABC123",      # too short (6)
    "A" * 21,      # too long (21)
    "AB CD 1234",  # space in middle
    "SN-123456",   # hyphen
    "SN_12345",    # underscore
    "",            # empty
    "   ",         # whitespace only
])
def test_validate_logger_sn_rejects(bad):
    with pytest.raises(ValueError):
        validate_logger_sn(bad)
