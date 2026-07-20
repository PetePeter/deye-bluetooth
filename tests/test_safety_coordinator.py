"""P5 — coordinator write-safety tests: dry-run, readback, reassert.

Requires homeassistant (guarded by pytest.importorskip).
Uses WriteableFakeTransport — no bleak.
"""
from __future__ import annotations

import logging

import pytest

pytest.importorskip("homeassistant")

from custom_components.deye_ble import protocol as p
from custom_components.deye_ble.registers import DISCHARGE_SOC_REGS, REG_MAX_SELL_POWER
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
        write_fail_times: int = 0,
    ):
        super().__init__(frames=frames, fail_on_block=fail_on_block)
        self.writes: list[tuple[int, int]] = []
        self._readback_values = readback_values or {}
        self._write_fail = write_fail
        # Number of leading write() calls that raise a transient error before the
        # transport starts succeeding — models a flaky BLE link.
        self._write_fail_times = write_fail_times
        self.write_attempts = 0

    async def write(self, address: int, value: int) -> None:
        self.write_attempts += 1
        if self._write_fail:
            raise RuntimeError("write failed")
        if self._write_fail_times > 0:
            self._write_fail_times -= 1
            raise TimeoutError("BLE write timed out")
        self.writes.append((address, value))

    async def read(self, address: int, count: int) -> list[int]:
        if address in self._readback_values:
            return [self._readback_values[address]]
        return await super().read(address, count)


# --- shared helpers ----------------------------------------------------------

def _stub_ble_device(monkeypatch, *, present=True):
    """Stub the HA bluetooth device lookup used by the write path.

    ``async_ble_device_from_address`` needs a real ``hass``; these tests run
    without one. Pass ``present=False`` to simulate a missing device.
    """
    from custom_components.deye_ble import coordinator as coord_mod

    device = object() if present else None
    monkeypatch.setattr(
        coord_mod, "async_ble_device_from_address", lambda _hass, _addr: device
    )


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
    async def test_dry_run_off_calls_write(self, monkeypatch):
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
        _stub_ble_device(monkeypatch)

        await coordinator.async_write(REG_MAX_SELL_POWER, 200)
        assert len(transport.writes) == 1
        assert transport.writes[0] == (REG_MAX_SELL_POWER, 200)

    @pytest.mark.asyncio
    async def test_dry_run_off_verifies_readback(self, monkeypatch):
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
        _stub_ble_device(monkeypatch)

        await coordinator.async_write(REG_MAX_SELL_POWER, 200)
        assert len(transport.writes) == 1

    @pytest.mark.asyncio
    async def test_dry_run_off_readback_mismatch_raises(self, monkeypatch):
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
        _stub_ble_device(monkeypatch)

        with pytest.raises(ValueError, match="readback"):
            await coordinator.async_write(REG_MAX_SELL_POWER, 200)


# --- async_write_many (discharge SOC multi-slot write) -----------------------

class TestWriteMany:
    def _coordinator(self, transport, *, dry_run, monkeypatch=None):
        from custom_components.deye_ble.coordinator import DeyeBleCoordinator

        coordinator = DeyeBleCoordinator(
            hass=None,
            address="AA:BB:CC:DD:EE:FF",
            transport_factory=lambda _dev: transport,
            dry_run=dry_run,
        )
        coordinator._resolve_device = lambda: None
        # The write path resolves the device via the HA bluetooth helper, which
        # needs a real hass. Stub it for the non-dry-run tests.
        if monkeypatch is not None:
            _stub_ble_device(monkeypatch)
        return coordinator

    @pytest.mark.asyncio
    async def test_dry_run_on_writes_nothing_logs_each(self, caplog):
        transport = WriteableFakeTransport()
        coordinator = self._coordinator(transport, dry_run=True)

        with caplog.at_level(logging.INFO, logger="custom_components.deye_ble.coordinator"):
            await coordinator.async_write_many({reg: 20 for reg in DISCHARGE_SOC_REGS})

        assert transport.writes == []
        for reg in DISCHARGE_SOC_REGS:
            assert f"0x{reg:04X}" in caplog.text

    @pytest.mark.asyncio
    async def test_dry_run_off_writes_all_and_verifies(self, monkeypatch):
        transport = WriteableFakeTransport(
            readback_values={reg: 20 for reg in DISCHARGE_SOC_REGS},
        )
        coordinator = self._coordinator(transport, dry_run=False, monkeypatch=monkeypatch)

        await coordinator.async_write_many({reg: 20 for reg in DISCHARGE_SOC_REGS})

        assert transport.writes == [(reg, 20) for reg in DISCHARGE_SOC_REGS]

    @pytest.mark.asyncio
    async def test_readback_mismatch_on_any_reg_raises(self, monkeypatch):
        # Third slot reads back wrong -> the whole operation surfaces an error.
        readback = {reg: 20 for reg in DISCHARGE_SOC_REGS}
        readback[DISCHARGE_SOC_REGS[2]] = 99
        transport = WriteableFakeTransport(readback_values=readback)
        coordinator = self._coordinator(transport, dry_run=False, monkeypatch=monkeypatch)

        with pytest.raises(ValueError, match="readback"):
            await coordinator.async_write_many({reg: 20 for reg in DISCHARGE_SOC_REGS})

    @pytest.mark.asyncio
    async def test_all_regs_tracked_for_reassert(self, monkeypatch):
        transport = WriteableFakeTransport(
            readback_values={reg: 20 for reg in DISCHARGE_SOC_REGS},
        )
        coordinator = self._coordinator(transport, dry_run=False, monkeypatch=monkeypatch)

        await coordinator.async_write_many({reg: 20 for reg in DISCHARGE_SOC_REGS})

        for reg in DISCHARGE_SOC_REGS:
            assert coordinator._tracked_values[reg] == 20


class TestCarryForward:
    @pytest.mark.asyncio
    async def test_discharge_soc_carried_forward_across_polls(self):
        from custom_components.deye_ble.coordinator import DeyeBleCoordinator

        transport = WriteableFakeTransport()
        coordinator = DeyeBleCoordinator(
            hass=None,
            address="AA:BB:CC:DD:EE:FF",
            transport_factory=lambda _dev: transport,
        )
        coordinator._resolve_device = lambda: None
        # Prior cycle had an optimistic discharge_soc; a telemetry-only poll must
        # not drop it.
        coordinator.async_set_updated_data({"discharge_soc": 25})

        data = await coordinator._async_update_data()
        assert data["discharge_soc"] == 25

    @pytest.mark.asyncio
    @pytest.mark.parametrize("key,value", [
        ("max_charge_current", 210),
        ("max_discharge_current", 200),
        ("batt_shutdown_soc", 4),
        ("batt_low_soc", 5),
        ("batt_restart_soc", 6),
    ])
    async def test_control_value_carried_forward_across_polls(self, key, value):
        # These control mirrors are only read on the slow config cycle. A
        # telemetry-only poll must carry them forward, or the number entity
        # blanks between config reads.
        from custom_components.deye_ble.coordinator import DeyeBleCoordinator

        transport = WriteableFakeTransport()
        coordinator = DeyeBleCoordinator(
            hass=None,
            address="AA:BB:CC:DD:EE:FF",
            transport_factory=lambda _dev: transport,
        )
        coordinator._resolve_device = lambda: None
        coordinator.async_set_updated_data({key: value})

        data = await coordinator._async_update_data()
        assert data[key] == value


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
    async def test_reassert_on_drift_triggers_rewrite(self, monkeypatch):
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
        _stub_ble_device(monkeypatch)  # reassert re-write resolves the device
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
    async def test_write_device_not_found_raises_clear_error(self, monkeypatch):
        from custom_components.deye_ble.coordinator import DeyeBleCoordinator
        from homeassistant.exceptions import HomeAssistantError

        coordinator = DeyeBleCoordinator(
            hass=None,
            address="AA:BB:CC:DD:EE:FF",
            transport_factory=lambda _dev: WriteableFakeTransport(),
            dry_run=False,
        )
        _stub_ble_device(monkeypatch, present=False)

        with pytest.raises(HomeAssistantError, match="not found"):
            await coordinator.async_write(REG_MAX_SELL_POWER, 200)

    @pytest.mark.asyncio
    async def test_poll_device_not_found_raises_update_failed(self, monkeypatch):
        from custom_components.deye_ble.coordinator import DeyeBleCoordinator
        from homeassistant.helpers.update_coordinator import UpdateFailed

        coordinator = DeyeBleCoordinator(
            hass=None,
            address="AA:BB:CC:DD:EE:FF",
            transport_factory=lambda _dev: WriteableFakeTransport(),
        )
        # Real _resolve_device runs; the bluetooth lookup returns no device.
        _stub_ble_device(monkeypatch, present=False)

        with pytest.raises(UpdateFailed, match="not found"):
            await coordinator._async_update_data()


# --- Transient failure tolerance ---------------------------------------------

class TestFailureTolerance:
    """A flaky BLE poll must not flip every entity to unknown — the last good
    values are kept until max_failures consecutive failures accumulate."""

    def _make_coordinator(self, transport, *, max_failures=10):
        from custom_components.deye_ble.coordinator import DeyeBleCoordinator

        coordinator = DeyeBleCoordinator(
            hass=None,
            address="AA:BB:CC:DD:EE:FF",
            transport_factory=lambda _dev: transport,
            max_failures=max_failures,
        )
        coordinator._resolve_device = lambda: None
        return coordinator

    @pytest.mark.asyncio
    async def test_transient_failure_keeps_last_good_data(self):
        coordinator = self._make_coordinator(WriteableFakeTransport())

        good = await coordinator._async_update_data()
        coordinator.async_set_updated_data(good)  # populate coordinator.data

        # Next poll fails — but we have prior data and are under threshold.
        coordinator._transport_factory = (
            lambda _dev: WriteableFakeTransport(fail_on_block=0x024A)
        )
        result = await coordinator._async_update_data()

        assert result["solar_power"] == good["solar_power"]
        assert coordinator._consecutive_failures == 1

    @pytest.mark.asyncio
    async def test_failures_reaching_threshold_raise_update_failed(self):
        from homeassistant.helpers.update_coordinator import UpdateFailed

        coordinator = self._make_coordinator(
            WriteableFakeTransport(), max_failures=3
        )
        good = await coordinator._async_update_data()
        coordinator.async_set_updated_data(good)

        coordinator._transport_factory = (
            lambda _dev: WriteableFakeTransport(fail_on_block=0x024A)
        )

        # Failures 1 and 2 are tolerated.
        await coordinator._async_update_data()
        await coordinator._async_update_data()
        # Failure 3 hits the threshold and surfaces.
        with pytest.raises(UpdateFailed, match="read failed"):
            await coordinator._async_update_data()

    @pytest.mark.asyncio
    async def test_success_resets_failure_count(self):
        coordinator = self._make_coordinator(WriteableFakeTransport())
        good = await coordinator._async_update_data()
        coordinator.async_set_updated_data(good)

        coordinator._transport_factory = (
            lambda _dev: WriteableFakeTransport(fail_on_block=0x024A)
        )
        await coordinator._async_update_data()
        assert coordinator._consecutive_failures == 1

        # A recovered poll clears the run.
        coordinator._transport_factory = lambda _dev: WriteableFakeTransport()
        await coordinator._async_update_data()
        assert coordinator._consecutive_failures == 0

    @pytest.mark.asyncio
    async def test_first_failure_with_no_prior_data_raises(self):
        from homeassistant.helpers.update_coordinator import UpdateFailed

        coordinator = self._make_coordinator(
            WriteableFakeTransport(fail_on_block=0x024A)
        )
        # No prior data — nothing to carry forward, so it must surface at once.
        with pytest.raises(UpdateFailed, match="read failed"):
            await coordinator._async_update_data()


# --- Write retry on flaky BLE ------------------------------------------------

class TestWriteRetry:
    """A transient BLE write failure must be retried inside the coordinator, so
    every caller (UI sliders and automations alike) gets resilience for free.
    A genuine readback mismatch or a missing device is NOT a transient failure
    and must surface without burning retries."""

    def _coordinator(self, transport, monkeypatch, *, attempts=3):
        from custom_components.deye_ble.coordinator import DeyeBleCoordinator

        coordinator = DeyeBleCoordinator(
            hass=None,
            address="AA:BB:CC:DD:EE:FF",
            transport_factory=lambda _dev: transport,
            dry_run=False,
            write_attempts=attempts,
        )
        coordinator._resolve_device = lambda: None
        coordinator._write_backoff = 0  # keep tests instant
        _stub_ble_device(monkeypatch)
        return coordinator

    @pytest.mark.asyncio
    async def test_retries_until_success(self, monkeypatch):
        # Fails the first two write attempts, lands on the third.
        transport = WriteableFakeTransport(
            readback_values={REG_MAX_SELL_POWER: 200},
            write_fail_times=2,
        )
        coordinator = self._coordinator(transport, monkeypatch)

        await coordinator.async_write(REG_MAX_SELL_POWER, 200)

        assert transport.write_attempts == 3          # two failures + one success
        assert transport.writes == [(REG_MAX_SELL_POWER, 200)]
        assert coordinator._tracked_values[REG_MAX_SELL_POWER] == 200

    @pytest.mark.asyncio
    async def test_gives_up_after_max_attempts(self, monkeypatch):
        # Every write attempt times out — surfaces after exhausting the budget.
        transport = WriteableFakeTransport(
            readback_values={REG_MAX_SELL_POWER: 200},
            write_fail_times=99,
        )
        coordinator = self._coordinator(transport, monkeypatch, attempts=3)

        with pytest.raises(TimeoutError):
            await coordinator.async_write(REG_MAX_SELL_POWER, 200)

        assert transport.write_attempts == 3

    @pytest.mark.asyncio
    async def test_readback_mismatch_not_retried(self, monkeypatch):
        # The write succeeds but reads back wrong — likely an inverter clamp, not
        # a flaky link. Fail fast so a retry can't mask it.
        transport = WriteableFakeTransport(
            readback_values={REG_MAX_SELL_POWER: 50},
        )
        coordinator = self._coordinator(transport, monkeypatch)

        with pytest.raises(ValueError, match="readback"):
            await coordinator.async_write(REG_MAX_SELL_POWER, 200)

        assert transport.write_attempts == 1

    @pytest.mark.asyncio
    async def test_device_not_found_not_retried(self, monkeypatch):
        from homeassistant.exceptions import HomeAssistantError

        transport = WriteableFakeTransport()
        coordinator = self._coordinator(transport, monkeypatch)
        _stub_ble_device(monkeypatch, present=False)  # device absent

        with pytest.raises(HomeAssistantError, match="not found"):
            await coordinator.async_write(REG_MAX_SELL_POWER, 200)

        assert transport.write_attempts == 0

    @pytest.mark.asyncio
    async def test_write_many_retries_until_success(self, monkeypatch):
        transport = WriteableFakeTransport(
            readback_values={reg: 20 for reg in DISCHARGE_SOC_REGS},
            write_fail_times=2,
        )
        coordinator = self._coordinator(transport, monkeypatch)

        await coordinator.async_write_many({reg: 20 for reg in DISCHARGE_SOC_REGS})

        assert transport.writes == [(reg, 20) for reg in DISCHARGE_SOC_REGS]
        for reg in DISCHARGE_SOC_REGS:
            assert coordinator._tracked_values[reg] == 20

    @pytest.mark.asyncio
    async def test_happy_path_single_attempt(self, monkeypatch):
        transport = WriteableFakeTransport(
            readback_values={REG_MAX_SELL_POWER: 200},
        )
        coordinator = self._coordinator(transport, monkeypatch)

        await coordinator.async_write(REG_MAX_SELL_POWER, 200)

        assert transport.write_attempts == 1


# --- Persistent connection (opt-in keepalive) --------------------------------

class CountingTransport(WriteableFakeTransport):
    """Fake transport that counts explicit connect()/disconnect() calls.

    The keepalive path connects/disconnects the transport directly; the per-poll
    path drives the same via the async-context-manager protocol. Both funnel
    through connect()/disconnect() here so a test can assert how many BLE
    sessions actually happened.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.connects = 0
        self.disconnects = 0

    async def connect(self) -> None:
        self.connects += 1

    async def disconnect(self) -> None:
        self.disconnects += 1

    async def __aenter__(self) -> "CountingTransport":
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> bool:
        await self.disconnect()
        return False


def _counting_factory(behaviors=None):
    """Return (factory, created) where factory mints a CountingTransport per call.

    ``behaviors`` is an optional list of kwarg dicts applied to successive
    transports (the last entry repeats), so a test can make the first session
    fail and later ones succeed.
    """
    created: list[CountingTransport] = []
    behaviors = behaviors or [{}]

    def factory(_dev):
        kw = behaviors[min(len(created), len(behaviors) - 1)]
        t = CountingTransport(**kw)
        created.append(t)
        return t

    return factory, created


class TestKeepalive:
    """Opt-in persistent connection: reuse one BLE link across polls/writes,
    drop it on error, and release it the instant keepalive is turned off."""

    def _coordinator(self, factory, *, keepalive):
        from custom_components.deye_ble.coordinator import DeyeBleCoordinator

        coordinator = DeyeBleCoordinator(
            hass=None,
            address="AA:BB:CC:DD:EE:FF",
            transport_factory=factory,
            keepalive=keepalive,
        )
        coordinator._resolve_device = lambda: object()
        return coordinator

    @pytest.mark.asyncio
    async def test_keepalive_reuses_one_connection_across_polls(self):
        factory, created = _counting_factory()
        coordinator = self._coordinator(factory, keepalive=True)

        await coordinator._async_update_data()
        await coordinator._async_update_data()

        assert len(created) == 1          # one transport, reused
        assert created[0].connects == 1   # connected once
        assert created[0].disconnects == 0  # never torn down between polls

    @pytest.mark.asyncio
    async def test_keepalive_drops_link_on_failure_then_reconnects(self):
        # First session fails mid-poll; the held link must be dropped so the
        # next poll builds a fresh one.
        factory, created = _counting_factory(
            behaviors=[{"fail_on_block": 0x024A}, {}]
        )
        coordinator = self._coordinator(factory, keepalive=True)
        coordinator.async_set_updated_data({"solar_power": 1})  # tolerate 1 failure

        await coordinator._async_update_data()  # fails -> drops T1
        await coordinator._async_update_data()  # reconnects -> T2

        assert len(created) == 2
        assert created[0].disconnects == 1     # failed link torn down
        assert created[1].connects == 1        # fresh reconnect

    @pytest.mark.asyncio
    async def test_turning_keepalive_off_disconnects_held_link(self):
        factory, created = _counting_factory()
        coordinator = self._coordinator(factory, keepalive=True)

        await coordinator._async_update_data()  # establishes the held link
        assert created[0].connects == 1

        await coordinator.async_set_keepalive(False)

        assert created[0].disconnects == 1
        assert coordinator._persistent is None

    @pytest.mark.asyncio
    async def test_keepalive_off_reconnects_each_poll(self):
        # Default behaviour must be unchanged: a fresh session per poll.
        factory, created = _counting_factory()
        coordinator = self._coordinator(factory, keepalive=False)

        await coordinator._async_update_data()
        await coordinator._async_update_data()

        assert len(created) == 2
        assert all(t.connects == 1 and t.disconnects == 1 for t in created)
