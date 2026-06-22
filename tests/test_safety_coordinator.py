"""P5 — coordinator write-safety tests: dry-run, readback, reassert.

Requires homeassistant (guarded by pytest.importorskip).
Uses WriteableFakeTransport — no bleak.
"""
from __future__ import annotations

import logging

import pytest

pytest.importorskip("homeassistant")

from custom_components.deye_ble import protocol as p
from custom_components.deye_ble.registers import REG_MAX_SELL_POWER
from tests.test_coordinator import FakeTransport
from tests.test_registers import FRAMES

# Control block frame: work_mode=0 (Selling First), max_sell=100
CONTROL_FRAME_100 = "+ok=01030400000064FBD8"


def _frames_with_control(*, max_sell: int = 100) -> dict[int, str]:
    """Return default telemetry frames plus a control block at 0x008E.

    Builds a valid CRC'd frame for the given max_sell_power value.
    """
    frames = dict(FRAMES)
    # Modbus response: slave=01, func=03, byte_count=04, work_mode=0x0000, max_sell=hi/lo
    body = bytes([0x01, 0x03, 0x04, 0x00, 0x00, (max_sell >> 8) & 0xFF, max_sell & 0xFF])
    frames[0x008E] = "+ok=" + (body + p.crc16(body)).hex().upper()
    return frames


# --- Extended FakeTransport ---------------------------------------------------

class WriteableFakeTransport(FakeTransport):
    """Extends FakeTransport with write() support and read-back simulation."""

    def __init__(
        self,
        frames: dict[int, str] | None = None,
        *,
        fail_on_block: int | None = None,
        readback_values: dict[int, int] | None = None,
        write_fail: bool = False,
    ):
        super().__init__(frames=frames, fail_on_block=fail_on_block)
        self.writes: list[tuple[int, int]] = []
        self._readback_values = readback_values or {}
        self._write_fail = write_fail

    async def write(self, address: int, value: int) -> None:
        if self._write_fail:
            raise RuntimeError("write failed")
        self.writes.append((address, value))

    async def read(self, address: int, count: int) -> list[int]:
        if address in self._readback_values:
            return [self._readback_values[address]]
        return await super().read(address, count)


# --- dry-run tests -----------------------------------------------------------

class TestDryRun:
    @pytest.mark.asyncio
    async def test_dry_run_on_blocks_write(self):
        from custom_components.deye_ble.coordinator import DeyeBleCoordinator

        transport = WriteableFakeTransport()
        coordinator = DeyeBleCoordinator(
            hass=None,
            address="AA:BB:CC:DD:EE:FF",
            transport_factory=lambda _dev: transport,
            dry_run=True,
        )
        coordinator._resolve_device = lambda: None

        await coordinator.async_write(REG_MAX_SELL_POWER, 200)
        assert transport.writes == []

    @pytest.mark.asyncio
    async def test_dry_run_on_logs_intent(self, caplog):
        from custom_components.deye_ble.coordinator import DeyeBleCoordinator

        transport = WriteableFakeTransport()
        coordinator = DeyeBleCoordinator(
            hass=None,
            address="AA:BB:CC:DD:EE:FF",
            transport_factory=lambda _dev: transport,
            dry_run=True,
        )
        coordinator._resolve_device = lambda: None

        with caplog.at_level(logging.INFO, logger="custom_components.deye_ble.coordinator"):
            await coordinator.async_write(REG_MAX_SELL_POWER, 200)

        assert "0x008F" in caplog.text
        assert "200" in caplog.text
        assert "dry-run" in caplog.text.lower()

    @pytest.mark.asyncio
    async def test_dry_run_off_calls_write(self):
        from custom_components.deye_ble.coordinator import DeyeBleCoordinator

        transport = WriteableFakeTransport(
            readback_values={REG_MAX_SELL_POWER: 200},
        )
        coordinator = DeyeBleCoordinator(
            hass=None,
            address="AA:BB:CC:DD:EE:FF",
            transport_factory=lambda _dev: transport,
            dry_run=False,
        )
        coordinator._resolve_device = lambda: None

        await coordinator.async_write(REG_MAX_SELL_POWER, 200)
        assert len(transport.writes) == 1
        assert transport.writes[0] == (REG_MAX_SELL_POWER, 200)

    @pytest.mark.asyncio
    async def test_dry_run_off_verifies_readback(self):
        from custom_components.deye_ble.coordinator import DeyeBleCoordinator

        transport = WriteableFakeTransport(
            readback_values={REG_MAX_SELL_POWER: 200},
        )
        coordinator = DeyeBleCoordinator(
            hass=None,
            address="AA:BB:CC:DD:EE:FF",
            transport_factory=lambda _dev: transport,
            dry_run=False,
        )
        coordinator._resolve_device = lambda: None

        await coordinator.async_write(REG_MAX_SELL_POWER, 200)
        assert len(transport.writes) == 1

    @pytest.mark.asyncio
    async def test_dry_run_off_readback_mismatch_raises(self):
        from custom_components.deye_ble.coordinator import DeyeBleCoordinator

        transport = WriteableFakeTransport(
            readback_values={REG_MAX_SELL_POWER: 50},
        )
        coordinator = DeyeBleCoordinator(
            hass=None,
            address="AA:BB:CC:DD:EE:FF",
            transport_factory=lambda _dev: transport,
            dry_run=False,
        )
        coordinator._resolve_device = lambda: None

        with pytest.raises(ValueError, match="readback"):
            await coordinator.async_write(REG_MAX_SELL_POWER, 200)


# --- reassert tests ----------------------------------------------------------

class TestReassert:
    @pytest.mark.asyncio
    async def test_reassert_off_no_extra_writes(self):
        """When reassert=False, no drift correction happens during poll."""
        from custom_components.deye_ble.coordinator import DeyeBleCoordinator

        transport = WriteableFakeTransport(
            frames=_frames_with_control(max_sell=100),
            readback_values={REG_MAX_SELL_POWER: 200},
        )
        coordinator = DeyeBleCoordinator(
            hass=None,
            address="AA:BB:CC:DD:EE:FF",
            transport_factory=lambda _dev: transport,
            dry_run=False,
            reassert=False,
        )
        coordinator._resolve_device = lambda: None
        coordinator._tracked_values = {REG_MAX_SELL_POWER: 200}

        await coordinator._async_update_data()
        # reassert is off — even though drift exists, no writes.
        assert transport.writes == []

    @pytest.mark.asyncio
    async def test_reassert_on_drift_triggers_rewrite(self):
        """When reassert=True and drift detected, the value is re-applied."""
        from custom_components.deye_ble.coordinator import DeyeBleCoordinator

        transport = WriteableFakeTransport(
            frames=_frames_with_control(max_sell=100),
            readback_values={REG_MAX_SELL_POWER: 200},
        )
        coordinator = DeyeBleCoordinator(
            hass=None,
            address="AA:BB:CC:DD:EE:FF",
            transport_factory=lambda _dev: transport,
            dry_run=False,
            reassert=True,
        )
        coordinator._resolve_device = lambda: None
        # Device reports max_sell=100 but we tracked 200 → drift.
        coordinator._tracked_values = {REG_MAX_SELL_POWER: 200}

        data = await coordinator._async_update_data()
        assert data["max_sell_power"] == 100  # from poll decode
        assert any(w == (REG_MAX_SELL_POWER, 200) for w in transport.writes)

    @pytest.mark.asyncio
    async def test_reassert_on_no_drift_no_extra_writes(self):
        """When reassert=True but no drift, no corrective writes happen."""
        from custom_components.deye_ble.coordinator import DeyeBleCoordinator

        transport = WriteableFakeTransport(
            frames=_frames_with_control(max_sell=200),
            readback_values={REG_MAX_SELL_POWER: 200},
        )
        coordinator = DeyeBleCoordinator(
            hass=None,
            address="AA:BB:CC:DD:EE:FF",
            transport_factory=lambda _dev: transport,
            dry_run=False,
            reassert=True,
        )
        coordinator._resolve_device = lambda: None
        # Tracked value matches what device reports — no drift.
        coordinator._tracked_values = {REG_MAX_SELL_POWER: 200}

        await coordinator._async_update_data()
        assert transport.writes == []


# --- BLE busy / not-found handling -------------------------------------------

class TestBLEBusy:
    @pytest.mark.asyncio
    async def test_write_device_not_found_raises_clear_error(self):
        from custom_components.deye_ble.coordinator import DeyeBleCoordinator
        from homeassistant.helpers.update_coordinator import UpdateFailed

        coordinator = DeyeBleCoordinator(
            hass=None,
            address="AA:BB:CC:DD:EE:FF",
            transport_factory=lambda _dev: WriteableFakeTransport(),
            dry_run=False,
        )

        def _not_found():
            raise UpdateFailed(f"BLE device AA:BB:CC:DD:EE:FF not found")

        coordinator._resolve_device = _not_found

        with pytest.raises(UpdateFailed, match="not found"):
            await coordinator.async_write(REG_MAX_SELL_POWER, 200)

    @pytest.mark.asyncio
    async def test_poll_device_not_found_raises_update_failed(self):
        from custom_components.deye_ble.coordinator import DeyeBleCoordinator
        from homeassistant.helpers.update_coordinator import UpdateFailed

        coordinator = DeyeBleCoordinator(
            hass=None,
            address="AA:BB:CC:DD:EE:FF",
            transport_factory=lambda _dev: WriteableFakeTransport(),
        )
        coordinator._resolve_device = lambda: None

        with pytest.raises(UpdateFailed, match="not found"):
            await coordinator._async_update_data()
