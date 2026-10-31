"""Polling tiers, connection modes, options migration, profile-aware writes.

Drives the real coordinator against fake transports; no bleak, no live HA loop.
"""
from __future__ import annotations

import pytest

pytest.importorskip("homeassistant")

from homeassistant.exceptions import HomeAssistantError

from custom_components.deye_ble import coordinator as coord_mod
from custom_components.deye_ble import registers as r
from custom_components.deye_ble.const import (
    CONF_CONNECTION_MODE,
    CONF_LIVE_INTERVAL,
    CONF_READ_ONLY,
    CONF_SCAN_INTERVAL,
    MODE_LIVE_CLOUD,
    MODE_LIVE_LOCAL,
    MODE_STANDARD,
)
from custom_components.deye_ble import protocol as p
from custom_components.deye_ble.helpers import migrate_options
from tests.test_registers import FRAMES
from tests.test_safety_coordinator import _counting_factory, _stub_ble_device
from tests.test_single_phase import CONFIG, DEVICE_TYPE, TELEMETRY


def _frame(count: int) -> str:
    body = bytes([0x01, 0x03, 2 * count]) + bytes(2 * count)
    return "+ok=" + (body + p.crc16(body)).hex().upper()


# Captured telemetry plus an all-zero frame for every slow block, so a slow
# read decodes (and is acknowledged) like it would on the device.
FULL_FRAMES = {**{start: _frame(n) for start, n in r.SLOW_BLOCKS}, **FRAMES}


def _factory():
    return _counting_factory(behaviors=[{"frames": FULL_FRAMES}])


class _Clock:
    """monotonic() that only moves when told to."""

    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(coord_mod.time, "monotonic", c)
    return c


def _coordinator(factory, **kwargs):
    coordinator = coord_mod.DeyeBleCoordinator(
        hass=None, address="AA:BB:CC:DD:EE:FF", transport_factory=factory, **kwargs,
    )
    coordinator._resolve_device = lambda: object()
    return coordinator


async def _poll(coordinator):
    coordinator.data = await coordinator._async_update_data()
    return coordinator.data


def _read_starts(created):
    return {start for t in created for start, _count in t.reads}


def _starts(blocks):
    return {start for start, _count in blocks}


# --- Tiers -------------------------------------------------------------------

@pytest.mark.asyncio
async def test_live_mode_reads_fast_and_slow_tiers_only_when_due(clock):
    factory, created = _factory()
    coordinator = _coordinator(factory, connection_mode=MODE_LIVE_LOCAL, live_interval=5)

    await _poll(coordinator)                       # first cycle: everything
    assert _read_starts(created) >= _starts(r.LIVE_BLOCKS + r.FAST_BLOCKS + r.SLOW_BLOCKS)

    created[0].reads.clear()
    clock.t += 5
    await _poll(coordinator)                       # 5 s later: live only
    assert _read_starts(created) == _starts(r.LIVE_BLOCKS)

    created[0].reads.clear()
    clock.t += 60
    await _poll(coordinator)                       # a minute on: live + fast
    assert _read_starts(created) == _starts(r.LIVE_BLOCKS + r.FAST_BLOCKS)


@pytest.mark.asyncio
async def test_values_from_tiers_not_read_carry_forward(clock):
    factory, _created = _factory()
    coordinator = _coordinator(factory, connection_mode=MODE_LIVE_LOCAL, live_interval=5)
    first = dict(await _poll(coordinator))

    clock.t += 5
    data = await _poll(coordinator)                # live only this cycle

    for key in ("total_solar", "bms_charge_voltage", "work_mode", "max_sell_power"):
        assert data[key] == first[key], key


@pytest.mark.asyncio
async def test_standard_mode_reads_fast_tier_every_minute_poll_despite_jitter(clock):
    # The fast interval equals the standard poll interval: timer jitter that
    # fires a poll slightly early must not skip the energy counters.
    factory, created = _factory()
    coordinator = _coordinator(factory, scan_interval=60)
    await _poll(coordinator)

    created.clear()
    clock.t += 59.5
    await _poll(coordinator)
    assert _starts(r.FAST_BLOCKS) <= _read_starts(created)


@pytest.mark.asyncio
async def test_slow_interval_is_configurable(clock):
    factory, created = _factory()
    coordinator = _coordinator(factory, scan_interval=60, slow_interval=900)
    await _poll(coordinator)

    created.clear()
    clock.t += 300                                 # the default would read now
    await _poll(coordinator)
    assert not _starts(r.SLOW_BLOCKS) & _read_starts(created)

    created.clear()
    clock.t += 600
    await _poll(coordinator)
    assert _starts(r.SLOW_BLOCKS) <= _read_starts(created)


# --- Connection modes ----------------------------------------------------------

def test_mode_selects_the_poll_interval():
    assert _coordinator(None, scan_interval=60).update_interval.total_seconds() == 60
    live = _coordinator(None, connection_mode=MODE_LIVE_CLOUD, live_interval=5)
    assert live.update_interval.total_seconds() == 5


@pytest.mark.asyncio
async def test_standard_reconnects_every_poll(clock):
    factory, created = _factory()
    coordinator = _coordinator(factory)
    await _poll(coordinator)
    await _poll(coordinator)
    assert len(created) == 2 and all(t.disconnects == 1 for t in created)


@pytest.mark.asyncio
async def test_live_cloud_goes_silent_for_the_quiet_window_then_reconnects(clock):
    factory, created = _factory()
    coordinator = _coordinator(
        factory, connection_mode=MODE_LIVE_CLOUD, live_interval=5,
        cloud_sync_interval=300, cloud_window=30,
    )
    await _poll(coordinator)
    held = created[0]
    assert held.disconnects == 0

    clock.t += 300                                 # window opens
    before = list(held.reads)
    data = await _poll(coordinator)
    assert held.disconnects == 1                   # link released for the logger
    assert held.reads == before and len(created) == 1   # no BLE traffic at all
    assert "battery_soc" in data                   # entities keep their values

    clock.t += 20                                  # still inside the window
    await _poll(coordinator)
    assert len(created) == 1

    clock.t += 15                                  # window over
    await _poll(coordinator)
    assert len(created) == 2 and created[1].disconnects == 0   # held again


@pytest.mark.asyncio
async def test_live_local_never_releases_the_link(clock):
    factory, created = _factory()
    coordinator = _coordinator(factory, connection_mode=MODE_LIVE_LOCAL, live_interval=5)
    for _ in range(5):
        await _poll(coordinator)
        clock.t += 300
    assert len(created) == 1 and created[0].disconnects == 0


@pytest.mark.asyncio
async def test_apply_options_switches_mode_and_interval():
    factory, created = _factory()
    coordinator = _coordinator(factory)
    await coordinator.async_apply_options({
        CONF_CONNECTION_MODE: MODE_LIVE_LOCAL, CONF_LIVE_INTERVAL: 3, CONF_READ_ONLY: False,
    })
    assert coordinator.connection_mode == MODE_LIVE_LOCAL
    assert coordinator.update_interval.total_seconds() == 3
    assert coordinator.read_only is False


# --- Options migration ---------------------------------------------------------

def test_migration_keeps_an_unlocked_standard_install_unchanged():
    old = {"dry_run": False, "keepalive": False, CONF_SCAN_INTERVAL: 60, "reassert": True}
    assert migrate_options(old) == {
        CONF_READ_ONLY: False, CONF_CONNECTION_MODE: MODE_STANDARD,
        CONF_SCAN_INTERVAL: 60, "reassert": True,
    }


def test_migration_maps_keepalive_to_live_local_at_the_old_interval():
    new = migrate_options({"keepalive": True, CONF_SCAN_INTERVAL: 45})
    assert new[CONF_CONNECTION_MODE] == MODE_LIVE_LOCAL
    assert new[CONF_LIVE_INTERVAL] == 45


def test_migration_is_a_no_op_once_done():
    assert migrate_options({CONF_READ_ONLY: True}) is None


# --- Profile detection + single-phase writes -------------------------------------

class _SinglePhaseTransport:
    """Single-phase logger: capture words, writable registers, block writes."""

    def __init__(self):
        self.words = {}
        for start, block in {**TELEMETRY, **CONFIG}.items():
            for i, w in enumerate(block):
                self.words[start + i] = w
        self.words[r.REG_DEVICE_TYPE] = DEVICE_TYPE
        self.reads: list[int] = []
        self.writes: list[tuple[int, object]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def handshake(self):
        pass

    async def read(self, address, count):
        self.reads.append(address)
        return [self.words.get(address + i, 0) for i in range(count)]

    async def write(self, address, value):
        self.writes.append((address, value))
        self.words[address] = value

    async def write_block(self, address, values):
        self.writes.append((address, list(values)))
        for i, w in enumerate(values):
            self.words[address + i] = w


@pytest.mark.asyncio
async def test_detect_profile_then_polls_never_read_the_device_type():
    t = _SinglePhaseTransport()
    coordinator = _coordinator(lambda _d: t)
    assert await coordinator.async_detect_profile() == r.PROFILE_SINGLE_PHASE

    t.reads.clear()
    data = await _poll(coordinator)
    assert r.REG_DEVICE_TYPE not in t.reads
    assert data["battery_soc"] == 52


@pytest.mark.asyncio
async def test_single_phase_write_lands_on_the_single_phase_register(monkeypatch):
    _stub_ble_device(monkeypatch)
    t = _SinglePhaseTransport()
    coordinator = _coordinator(lambda _d: t, profile=r.PROFILE_SINGLE_PHASE, read_only=False)
    coordinator._write_backoff = 0

    reg = coordinator.register_profile.controls["max_sell_power"]
    await coordinator.async_write(reg, 4000)
    assert t.writes == [(245, 4000)]


@pytest.mark.asyncio
async def test_single_phase_clock_sync_writes_only_the_clock_block(monkeypatch):
    from tests.test_clock_coordinator import NOW, VirtualClock, _install_virtual_clock

    clock = VirtualClock(NOW)
    _install_virtual_clock(monkeypatch, clock)
    t = _SinglePhaseTransport()
    coordinator = _coordinator(lambda _d: t, profile=r.PROFILE_SINGLE_PHASE, read_only=False)

    await coordinator.async_sync_clock()

    # One block write to 22-24 and nothing else: the three-phase Time Sync
    # address (0x00E4) is unverified on this map and must never be touched.
    assert [reg for reg, _v in t.writes] == [r.SP_REG_CLOCK]
    assert r.REG_TIME_SYNC not in t.reads
    assert coordinator.data["clock_sync_result"] == "ok"


# --- Review regressions ----------------------------------------------------------

@pytest.mark.asyncio
async def test_quiet_window_longer_than_its_interval_still_leaves_polling_time(clock):
    factory, created = _factory()
    coordinator = _coordinator(
        factory, connection_mode=MODE_LIVE_CLOUD, live_interval=5,
        cloud_sync_interval=60, cloud_window=120,
    )
    await _poll(coordinator)
    clock.t += 60
    await _poll(coordinator)                       # window opens
    clock.t += 121                                 # window over
    await _poll(coordinator)
    assert len(created) == 2                       # polled again, not silenced forever


def test_single_phase_reassert_sees_drift_on_its_own_registers():
    from custom_components.deye_ble.helpers import detect_drift

    controls = r.PROFILES[r.PROFILE_SINGLE_PHASE].controls
    reg_to_key = {reg: key for key, reg in controls.items()}
    drifted = detect_drift(
        {245: 4000, 244: 0, 232: 1},
        {"max_sell_power": 3000, "work_mode": "Selling First", "grid_charge_enabled": True},
        reg_to_key,
    )
    assert drifted == [(245, 4000)]


@pytest.mark.asyncio
async def test_single_phase_slow_tier_is_acknowledged_with_an_unset_tou_slot(clock):
    t = _SinglePhaseTransport()
    t.words[250] = 0xFFFF                          # unset slot time: no tou_slots
    coordinator = _coordinator(
        lambda _d: t, profile=r.PROFILE_SINGLE_PHASE, connection_mode=MODE_LIVE_LOCAL,
    )
    data = await _poll(coordinator)
    assert "tou_slots" not in data

    t.reads.clear()
    clock.t += 5
    await _poll(coordinator)
    assert not set(t.reads) & _starts(r.SP_SLOW_BLOCKS)


@pytest.mark.asyncio
async def test_read_only_switch_locks_writes_before_the_options_listener_runs():
    from custom_components.deye_ble.switch import DeyeReadOnlySwitch

    class _Entry:
        data = {"logger_sn": "SN"}
        options = {CONF_READ_ONLY: False}

    class _ConfigEntries:
        def async_update_entry(self, entry, *, options):
            entry.options = options                # listener NOT run

    coordinator = _coordinator(None, read_only=False)
    switch = DeyeReadOnlySwitch(coordinator, _Entry())
    switch.hass = type("Hass", (), {"config_entries": _ConfigEntries()})()

    await switch.async_turn_on()

    assert coordinator.read_only is True


@pytest.mark.asyncio
async def test_write_queued_behind_a_poll_is_refused_if_read_only_turns_on(monkeypatch):
    import asyncio

    _stub_ble_device(monkeypatch)
    t = _SinglePhaseTransport()
    coordinator = _coordinator(lambda _d: t, profile=r.PROFILE_SINGLE_PHASE, read_only=False)

    await coordinator._ble_lock.acquire()          # a poll holds the link
    write = asyncio.ensure_future(coordinator.async_write(232, 1))
    await asyncio.sleep(0)                         # write passes the first gate, queues
    coordinator.read_only = True
    coordinator._ble_lock.release()

    with pytest.raises(HomeAssistantError, match="Read Only"):
        await write
    assert t.writes == []


@pytest.mark.asyncio
async def test_failed_slow_block_is_retried_once_then_acknowledged(clock):
    class _ClockBlockFails(_SinglePhaseTransport):
        async def read(self, address, count):
            if address == 18:
                self.reads.append(address)
                raise TimeoutError
            return await super().read(address, count)

    t = _ClockBlockFails()
    coordinator = _coordinator(
        lambda _d: t, profile=r.PROFILE_SINGLE_PHASE, connection_mode=MODE_LIVE_LOCAL,
    )
    await _poll(coordinator)                       # slow block 18 fails

    t.reads.clear()
    clock.t += 5
    await _poll(coordinator)                       # retried once
    assert 18 in t.reads

    t.reads.clear()
    clock.t += 5
    await _poll(coordinator)                       # still failing: not every poll
    assert 18 not in t.reads
