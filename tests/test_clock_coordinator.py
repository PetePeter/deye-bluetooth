"""Coordinator wiring for the inverter clock: snapshot drift + carry-forward.

Requires homeassistant (guarded by pytest.importorskip) because the drift is
measured with dt_util.now(). Uses the existing FakeTransport — no bleak, no
mocks. Lives here rather than in test_coordinator.py, which is deliberately
HA-free.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("homeassistant")

from homeassistant.exceptions import HomeAssistantError

from custom_components.deye_ble import const
from custom_components.deye_ble import coordinator as coord_mod
from custom_components.deye_ble import protocol as p
from custom_components.deye_ble import registers as r
from tests.test_coordinator import FakeTransport
from custom_components.deye_ble.registers import decode_clock, encode_clock
from tests.test_registers import CLOCK_DATETIME, FRAMES, clock_frame

# Site time a few minutes after the captured clock reading, so a real (nonzero)
# drift is asserted rather than a coincidental zero.
SITE_TZ = timezone(timedelta(hours=10))
NOW = datetime(2026, 8, 9, 9, 0, 0, tzinfo=SITE_TZ)
EXPECTED_DRIFT = -240  # inverter 08:56:00 vs HA 09:00:00 -> 4 minutes behind
EXPECTED_TOD_DRIFT = -240  # same dates here, so time-of-day drift matches


def _frames_with_clock() -> dict[int, str]:
    return {**FRAMES, 0x003E: clock_frame()}


def _freeze_now(monkeypatch, moment: datetime) -> None:
    """Pin the coordinator's notion of local time."""
    monkeypatch.setattr(coord_mod.dt_util, "now", lambda *_a, **_kw: moment)


async def _poll(coordinator) -> dict:
    """Run one update cycle and store the result the way HA's base class does.

    _async_update_data reads self.data for carry-forward; when it is driven
    directly (no running coordinator) nothing writes that back.
    """
    coordinator.data = await coordinator._async_update_data()
    return coordinator.data


def _make_coordinator(transport, **kwargs):
    coordinator = coord_mod.DeyeBleCoordinator(
        hass=None,
        address="AA:BB:CC:DD:EE:FF",
        transport_factory=lambda _dev: transport,
        **kwargs,
    )
    coordinator._resolve_device = lambda: None
    return coordinator


@pytest.mark.asyncio
async def test_config_cycle_publishes_clock_and_drift(monkeypatch):
    _freeze_now(monkeypatch, NOW)
    coordinator = _make_coordinator(FakeTransport(_frames_with_clock()))

    data = await _poll(coordinator)

    assert data["inverter_clock"] == CLOCK_DATETIME
    assert data["inverter_clock_drift"] == EXPECTED_DRIFT
    assert data["inverter_time_of_day_drift"] == EXPECTED_TOD_DRIFT


@pytest.mark.asyncio
async def test_drift_does_not_grow_between_config_reads(monkeypatch):
    """Drift is a snapshot of the last clock read, not a live countdown.

    On telemetry-only cycles the clock is not re-read, so both keys are carried
    forward unchanged. If drift were ever recomputed against the carried clock
    (or derived at render time from `now`), it would climb one second per second
    and this assertion would fail.
    """
    _freeze_now(monkeypatch, NOW)
    coordinator = _make_coordinator(FakeTransport(_frames_with_clock()))
    await _poll(coordinator)

    # Ten minutes later, on a cycle where config is not due.
    coordinator._config_dirty = False
    coordinator._last_config_read = 1e12
    coordinator._slow_interval = 1e12
    _freeze_now(monkeypatch, NOW + timedelta(minutes=10))

    data = await _poll(coordinator)

    assert data["inverter_clock"] == CLOCK_DATETIME
    assert data["inverter_clock_drift"] == EXPECTED_DRIFT
    assert data["inverter_time_of_day_drift"] == EXPECTED_TOD_DRIFT


@pytest.mark.asyncio
async def test_failed_clock_read_keeps_previous_snapshot(monkeypatch):
    # Config-block reads are best-effort; a clock hiccup must not blank the
    # entities or invent a fresh drift for a stale reading.
    _freeze_now(monkeypatch, NOW)
    coordinator = _make_coordinator(FakeTransport(_frames_with_clock()))
    await _poll(coordinator)

    coordinator._transport_factory = lambda _dev: FakeTransport(
        _frames_with_clock(), fail_on_block=0x003E,
    )
    _freeze_now(monkeypatch, NOW + timedelta(minutes=10))
    coordinator.mark_config_dirty()

    data = await _poll(coordinator)

    assert data["inverter_clock"] == CLOCK_DATETIME
    assert data["inverter_clock_drift"] == EXPECTED_DRIFT
    assert data["inverter_time_of_day_drift"] == EXPECTED_TOD_DRIFT


@pytest.mark.asyncio
async def test_time_of_day_drift_survives_a_wrong_year(monkeypatch):
    """The live fault shape: RTC a year behind and minutes off.

    Total drift is dominated by the year error; the time-of-day sensor is the
    one that still shows the minutes-scale offset we are hunting.
    """
    wrong_year = clock_frame(encode_clock(datetime(2025, 8, 9, 10, 54, 43)))
    _freeze_now(monkeypatch, datetime(2026, 8, 9, 9, 25, 19, tzinfo=SITE_TZ))
    coordinator = _make_coordinator(FakeTransport({**FRAMES, 0x003E: wrong_year}))

    data = await _poll(coordinator)

    assert data["inverter_clock_drift"] == -31_530_636      # ~ -365 days
    assert data["inverter_time_of_day_drift"] == 89 * 60 + 24


# --- Clock sync (the write half) --------------------------------------------
# The RTC does not latch when its registers are written. It latches on the
# Time Sync bit (0x00E4 bit 0) falling on->off, so the whole sequence is
#   read 0x00E4 -> set bit0 -> settle -> write 0x3E/0x3F/0x40 -> clear bit0
# and the pre-sequence flag value is restored afterwards, on both paths.
# See local-deye-cloud/docs/inverter-clock-2026-08-09.md.

SYNC_ON = 0x0AEB   # live value with Time Sync enabled
SYNC_OFF = 0x0AEA  # live value with Time Sync disabled

# The settle the fake inverter demands before it will honour staged clock words.
# See FakeInverter.require_settle — this models an UNCONFIRMED hypothesis.
MODELLED_SETTLE = 1.0

# Captured before any monkeypatching, so the fakes below can yield to the event
# loop even while asyncio.sleep is patched out.
_REAL_SLEEP = asyncio.sleep

# A starting RTC that differs from NOW in EVERY field, modelled on the real
# observed fault (a clock left a year behind). The interruption tests need this:
# with a start of 2026-08-09 the first register (year+month) already matches
# now, so a half-committed mixture would be invisible and the assertion would
# pass without proving anything.
STALE_RTC = datetime(2025, 1, 5, 10, 54, 43)

# Corruption that never lets up. The known-bad extension keeps trying past the
# normal budget, so "every attempt is corrupt" has to cover those windows too —
# a fake that only corrupts the first three lets the extension succeed, which is
# correct behaviour and the wrong scenario for these tests.
ALL_WINDOWS = tuple(range(1, 40))


class VirtualClock:
    """Site wall clock that only moves when the code under test sleeps.

    Makes the sequence's timing assertable (which write happened how long after
    which) without a real delay, and lets a retry prove it re-derived its target.
    """

    def __init__(self, start: datetime):
        self.now = start

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def _install_virtual_clock(monkeypatch, clock: VirtualClock) -> None:
    async def _sleep(seconds, *_a, **_kw):
        clock.advance(seconds)
        # Keep a genuine yield point: a competing task must be *able* to
        # interleave here, otherwise the BLE-lock test proves nothing.
        await _REAL_SLEEP(0)

    monkeypatch.setattr(coord_mod.dt_util, "now", lambda *_a, **_kw: clock.now)
    monkeypatch.setattr(coord_mod.asyncio, "sleep", _sleep)
    # monotonic follows the virtual clock too. The known-bad ceiling is measured
    # with time.monotonic() so it bounds real elapsed work rather than a sum of
    # backoffs; without this the ceiling can never bind in a virtual-time test
    # and a mutation removing it stays green.
    monkeypatch.setattr(
        coord_mod.time, "monotonic", lambda: clock.now.timestamp(),
    )


class FakeInverter:
    """Real fake of the logger + inverter RTC over the AT/Modbus path.

    Models the one behaviour the design turns on: the staged clock registers are
    committed ONLY on the Time Sync bit0 on->off edge. Telemetry reads fall
    through to the captured frames so a real poll can run against it.

    MODELLING RULE, LEARNED THE HARD WAY. Model only what the hardware evidence
    establishes, and where its behaviour is unknown make the fake PERMISSIVE —
    an unknown must let a bug through and fail the test, never quietly absorb
    it. The first version of this fake cleared staging on every sync-on write
    and refused to latch unless all three words were present in that window.
    Both were invented; neither appears in
    local-deye-cloud/docs/inverter-clock-2026-08-09.md. Together they made a
    real corruption bug inexpressible — a partial stage followed by a falling
    edge — and every test passed with that bug present. A missing test looks
    missing; a lying fake looks like coverage.

    So: a write lands in the staging register and STAYS there, and a falling
    edge commits whatever each of the three registers currently holds — the
    newly written word where one was written, the RTC's own value where none
    was. A mixture of new date and stale time is therefore reachable here,
    because it is reachable on the device.

    *require_settle* additionally ignores words staged less than MODELLED_SETTLE
    after the sync-on write. That is the rehearsal hypothesis (one failure
    without a gap, one success with one, 2026-08-13) encoded as hardware
    behaviour — it is NOT confirmed, and this fake must never be cited as
    evidence that the hardware needs the gap. It is kept because it makes the
    fake stricter than reality might be, which is the safe direction, and it
    stops the delay being deleted as dead weight.

    *corrupt_attempts* reproduces the observed bad write: the year byte 0x1A
    landing as 0x7A (year 2122) in the numbered commit window(s).

    *fail_write_reg* raises on every write to that register — used to interrupt
    the sequence mid-stage. *fail_enable_after_commit* raises on a flag write
    that would set bit 0 once a commit has already happened, i.e. it fails the
    final re-enable only.
    """

    def __init__(
        self,
        clock: VirtualClock,
        *,
        flags: int = SYNC_ON,
        rtc: datetime | None = None,
        require_settle: bool = False,
        corrupt_attempts: tuple[int, ...] = (),
        frames: dict[int, str] | None = None,
        fail_write_reg: int | None = None,
        fail_enable_after_commit: bool = False,
        cache_window: float = 0.0,
        fail_read_reg: int | None = None,
        undecodable_clock: bool = False,
        cancel_after_commit: bool = False,
        never_commits: bool = False,
        cache_survives_write: bool = False,
        io_delay: float = 0.0,
    ):
        # Set by the test after construction, since it needs the task object.
        self.cancel_on_enable = None
        self._clock = clock
        self.flags = flags
        # The RTC TICKS. Modelling it as a frozen value would make the
        # freshness rule untestable, because consecutive honest reads would be
        # identical and indistinguishable from cache.
        self._rtc_base = rtc or CLOCK_DATETIME
        self._rtc_set_at = clock.now
        self.latched: datetime | None = None  # what the last commit wrote
        self.ops: list[tuple] = []
        self._require_settle = require_settle
        self._corrupt_attempts = set(corrupt_attempts)
        self._frames = frames or FRAMES
        self._fail_write_reg = fail_write_reg
        self._fail_read_reg = fail_read_reg
        self._undecodable_clock = undecodable_clock
        self._fail_enable_after_commit = fail_enable_after_commit
        self._cache_window = cache_window
        self._cancel_after_commit = cancel_after_commit
        self._never_commits = never_commits
        # Hardware (2026-08-15, 32 post-commit frames across two runs) shows the
        # read cache invalidating on a write. This flag models the adversarial
        # world where it does not, so the guard against it can be tested.
        self._cache_survives_write = cache_survives_write
        self._io_delay = io_delay
        self._cached: list[int] | None = None
        self._cached_at: datetime | None = None
        # Staging persists across windows — see the modelling rule above.
        self._staged: dict[int, int] = {}
        self._staged_at: dict[int, datetime] = {}
        self._sync_on_at: datetime | None = None
        self.windows = 0  # commit windows opened (i.e. sync-on writes seen)
        # The logger accepts ONE BLE central. The fake used to model the
        # device's registers but say nothing about its connection model, which
        # made an entire class of concurrency bug INEXPRESSIBLE — a background
        # write escaping the coordinator's lock could collide with a poll and no
        # test could see it. Opening a second session now raises, the way the
        # radio does.
        self.sessions_open = 0
        self.max_concurrent_sessions = 0
        self.commits = 0  # falling edges seen

    # -- transport surface ---------------------------------------------------

    async def __aenter__(self) -> "FakeInverter":
        self._open_session()
        return self

    async def __aexit__(self, *exc) -> bool:
        self._close_session()
        return False

    async def connect(self) -> None:
        self.ops.append(("connect",))
        self._open_session()

    async def disconnect(self) -> None:
        self.ops.append(("disconnect",))
        self._close_session()

    def _open_session(self) -> None:
        self.sessions_open += 1
        self.max_concurrent_sessions = max(
            self.max_concurrent_sessions, self.sessions_open
        )
        if self.sessions_open > 1:
            raise AssertionError(
                "second BLE session opened while one was live — the logger "
                "accepts a single central, so this is br-connection-canceled"
            )

    def _close_session(self) -> None:
        self.sessions_open = max(0, self.sessions_open - 1)

    async def handshake(self) -> None:
        await self._io()
        self.ops.append(("handshake",))
        if self.cancel_on_enable is not None and self.commits:
            # The re-enable session has just opened, after the falling edge.
            task, self.cancel_on_enable = self.cancel_on_enable, None
            task.cancel()

    async def read(self, address: int, count: int) -> list[int]:
        await self._io()
        self.ops.append(("read", address, count))
        if address == self._fail_read_reg:
            raise RuntimeError(f"BLE read of 0x{address:04X} failed")
        if address == r.REG_TIME_SYNC:
            return [self.flags]
        if address == r.REG_CLOCK:
            return self._read_clock()
        return p.parse_read(self._frames[address])

    # -- the logger's response cache -----------------------------------------

    def _read_clock(self) -> list[int]:
        if self._undecodable_clock:
            return [0x1A0D, 0x0908, 0x3800]   # month 13: decodes to nothing
        """Serve the RTC through a cache, the way the logger actually does.

        Measured 2026-08-14: byte-identical frames in runs of 6, 2, 8, 2 and 7+
        at 1 Hz with no writes in between. A caller inside that window learns
        nothing about a write it just made — including that the frame may
        pre-date the write entirely and still decode to a plausible time.
        """
        now = self._clock.now
        age = (
            None if self._cached_at is None
            else (now - self._cached_at).total_seconds()
        )
        # A cache entry stamped in the FUTURE is not a valid cache entry — the
        # wall clock stepped backwards under it. Without this the entry never
        # ages out and the fake serves one frame forever, which is a bug in the
        # model rather than a behaviour of the logger.
        cached = self._cached is not None and age is not None and 0 <= age < self._cache_window
        if not cached:
            self._cached = encode_clock(self.rtc)
            self._cached_at = now
        return list(self._cached)

    @property
    def rtc(self) -> datetime:
        """The live, ticking RTC value."""
        elapsed = (self._clock.now - self._rtc_set_at).total_seconds()
        return self._rtc_base + timedelta(seconds=int(elapsed))

    async def write_block(self, address: int, values: list[int]) -> None:
        """One contiguous 0x10 frame — the form the clock must use.

        Measured 2026-08-15: three single-register writes corrupt the year byte
        4 times in 5 on this hardware; one block frame, 0 in 5. The fake records
        it as a single op so a test can assert the clock commit is ONE frame.
        """
        await self._io()
        self.ops.append(("write_block", address, list(values)))
        if address == self._fail_write_reg:
            raise RuntimeError(f"BLE block write to 0x{address:04X} failed")
        for offset, value in enumerate(values):
            self._stage(address + offset, value)

    async def write(self, address: int, value: int) -> None:
        await self._io()
        self.ops.append(("write", address, value))
        if address == self._fail_write_reg:
            raise RuntimeError(f"BLE write to 0x{address:04X} failed")
        if address == r.REG_TIME_SYNC:
            if (
                self._fail_enable_after_commit
                and value & r.TIME_SYNC_MASK
                and self.commits
            ):
                raise RuntimeError("BLE write of the Time Sync re-enable failed")
            self._flag_write(value)
        elif r.REG_CLOCK <= address < r.REG_CLOCK + r.CLOCK_WORD_COUNT:
            self._stage(address, value)
        else:
            raise AssertionError(f"unexpected write to 0x{address:04X}")

    async def _io(self) -> None:
        """Yield to the loop the way real BLE I/O does. DO NOT REMOVE.

        Without this, every operation completes inside one scheduler slice, so a
        competing task can never interleave and
        test_sync_holds_the_ble_lock_across_the_whole_sequence passes whether or
        not the lock is held — it did exactly that until a mutation check caught
        it. The await looks like dead weight and is the only thing making that
        test capable of failing.
        """
        await _REAL_SLEEP(self._io_delay)

    # -- modelled inverter behaviour -----------------------------------------

    def _flag_write(self, value: int) -> None:
        was_on = bool(self.flags & r.TIME_SYNC_MASK)
        now_on = bool(value & r.TIME_SYNC_MASK)
        self.flags = value
        if now_on:
            # A sync-on write opens a commit window, whether or not the bit was
            # already set (it usually is — 0x0AEB is the live value). Staging is
            # deliberately NOT cleared: nothing establishes that the device
            # discards previously written words, so the permissive model keeps
            # them and lets a stale word reach a commit.
            self.windows += 1
            self._sync_on_at = self._clock.now
        elif was_on:
            self._latch()
            if self._cancel_after_commit:
                # Cancel the task mid-sequence, in the gap between the commit
                # and the re-enable. Real sources: HA shutdown, integration
                # reload, config entry unload.
                self._cancel_after_commit = False
                task = asyncio.current_task()
                if task is not None:
                    task.cancel()

    def _stage(self, address: int, value: int) -> None:
        index = address - r.REG_CLOCK
        if not 0 <= index < r.CLOCK_WORD_COUNT:
            raise AssertionError(f"staged a non-clock register 0x{address:04X}")
        if index == 0 and self.windows in self._corrupt_attempts:
            # Observed twice on real hardware, same byte and same family — the
            # year byte gains high bits: 0x1A -> 0x7A (year 2122, 2026-08-09)
            # and 0x1A -> 0x4A (year 2074, 2026-08-14). Everything else in the
            # frame arrives correct, which is what makes it survive a
            # field-by-field check.
            value = (value & 0x00FF) | (0x7A << 8)
        self._staged[index] = value
        self._staged_at[index] = self._clock.now
        if not self._cache_survives_write:
            self._cached = None      # observed behaviour: a write drops the cache
            self._cached_at = None

    def _latch(self) -> None:  # noqa: C901 - modelled behaviour, kept explicit
        """Commit whatever the three registers hold — written word or old value.

        No minimum is required. A partial stage commits a MIXTURE: the words
        that were written, plus the RTC's own value for the ones that were not.
        That is the corruption shape the previous fake could not express.
        """
        self.commits += 1
        if self._never_commits:
            # Observed on hardware 2026-08-15: a full sequence whose commit
            # simply did not take, while the RTC went on ticking.
            return
        current = encode_clock(self.rtc)
        committed = []
        for index in range(r.CLOCK_WORD_COUNT):
            word = self._staged.get(index)
            committed.append(
                word if word is not None and self._settled(index) else current[index]
            )
        decoded = r.decode_clock(committed)
        if decoded is None:
            return  # an unrepresentable frame changes nothing
        self.latched = decoded
        self._rtc_base = decoded
        self._rtc_set_at = self._clock.now

    def _settled(self, index: int) -> bool:
        """Whether a staged word waited out the modelled (unconfirmed) settle."""
        if not self._require_settle or self._sync_on_at is None:
            return True
        gap = (self._staged_at[index] - self._sync_on_at).total_seconds()
        return gap >= MODELLED_SETTLE

    # -- assertion helpers ---------------------------------------------------

    @property
    def writes(self) -> list[tuple[int, int]]:
        """Every register write, with block frames expanded to (reg, value)."""
        out: list[tuple[int, int]] = []
        for op in self.ops:
            if op[0] == "write":
                out.append((op[1], op[2]))
            elif op[0] == "write_block":
                out.extend((op[1] + i, v) for i, v in enumerate(op[2]))
        return out

    @property
    def clock_frames(self) -> list[tuple]:
        """Frames that carried clock registers, however they were shaped."""
        return [
            op for op in self.ops
            if (op[0] == "write_block" and op[1] == r.REG_CLOCK)
            or (op[0] == "write"
                and r.REG_CLOCK <= op[1] < r.REG_CLOCK + r.CLOCK_WORD_COUNT)
        ]


@pytest.mark.asyncio
async def test_config_cycle_publishes_time_sync(monkeypatch):
    # The flag has to be on the poll plan, not just decodable: an inverter with
    # Time Sync off accepts no cloud calibration, and that was invisible for
    # days. Dropping 0x00E4 from CONTROL_BLOCKS fails here.
    clock = VirtualClock(NOW)
    _install_virtual_clock(monkeypatch, clock)

    data = await _poll(_make_coordinator(FakeInverter(clock, flags=SYNC_ON)))

    assert data["time_sync"] is True


# --- Characterisation of the fake itself ------------------------------------
# Testing a test double is normally an antipattern. These two earn their place:
# the fake encodes a model of the hardware, and its PERMISSIVENESS is the only
# reason the critical regression test above is capable of failing. An earlier
# version quietly refused to commit partial stages and cleared staging on every
# arm write; both were invented, and together they made a real corruption bug
# unreachable while every test stayed green. These pin the model so it cannot
# drift back to flattering the code.

@pytest.mark.asyncio
async def test_fake_commits_whatever_the_registers_hold():
    clock = VirtualClock(NOW)
    fake = FakeInverter(clock, flags=SYNC_ON, rtc=STALE_RTC)

    # Only the first word (year+month) lands, then the flag falls.
    await fake.write(r.REG_CLOCK, encode_clock(NOW)[0])
    await fake.write(r.REG_TIME_SYNC, SYNC_OFF)

    # New year and month against the old day and time — the corruption shape.
    assert fake.latched == datetime(2026, 8, 5, 10, 54, 43)


@pytest.mark.asyncio
async def test_fake_keeps_staged_words_across_an_arm_write():
    # Nothing in the hardware evidence says the device discards previously
    # written words when Time Sync is re-armed, so the fake must not either.
    clock = VirtualClock(NOW)
    fake = FakeInverter(clock, flags=SYNC_ON, rtc=STALE_RTC)

    await fake.write(r.REG_CLOCK, encode_clock(NOW)[0])
    await fake.write(r.REG_TIME_SYNC, SYNC_ON)   # re-arm, opens a new window
    await fake.write(r.REG_TIME_SYNC, SYNC_OFF)  # falling edge

    assert fake.latched == datetime(2026, 8, 5, 10, 54, 43)


def _sync_coordinator(fake, **kwargs):
    return _make_coordinator(fake, read_only=False, **kwargs)


def _target_after(*delays: float) -> datetime:
    """The naive site time the sequence should have written, after *delays*."""
    return (NOW + timedelta(seconds=sum(delays))).replace(tzinfo=None)


@pytest.mark.asyncio
async def test_sync_writes_the_proven_sequence_and_latches_now(monkeypatch):
    clock = VirtualClock(NOW)
    _install_virtual_clock(monkeypatch, clock)
    fake = FakeInverter(clock, flags=SYNC_ON)

    await _sync_coordinator(fake).async_sync_clock()

    target = _target_after(const.CLOCK_SYNC_SETTLE)
    words = encode_clock(target)
    # Order is the whole mechanism: flag on, settle, clock words, flag off.
    assert fake.writes == [
        (r.REG_TIME_SYNC, SYNC_ON),
        (r.REG_CLOCK + 0, words[0]),
        (r.REG_CLOCK + 1, words[1]),
        (r.REG_CLOCK + 2, words[2]),
        (r.REG_TIME_SYNC, SYNC_OFF),
        (r.REG_TIME_SYNC, SYNC_ON),   # restore
    ]
    assert fake.latched == target


@pytest.mark.asyncio
async def test_sync_restores_time_sync_on_after_success(monkeypatch):
    # The point of the whole design: the commit edge ends with Time Sync OFF,
    # which is exactly how the phone app silently disabled cloud calibration.
    # A daily sync that did the same would become the bug it is fixing.
    clock = VirtualClock(NOW)
    _install_virtual_clock(monkeypatch, clock)
    fake = FakeInverter(clock, flags=SYNC_ON)

    await _sync_coordinator(fake).async_sync_clock()

    assert fake.flags == SYNC_ON


@pytest.mark.asyncio
async def test_sync_enables_time_sync_even_when_it_was_off(monkeypatch):
    # Policy is "always end ON", not "restore what was there". Finding the flag
    # OFF means cloud calibration is disabled — a fault, not a preference — so
    # we repair it rather than faithfully preserving it. The other bits of the
    # register are still handed back exactly as read.
    clock = VirtualClock(NOW)
    _install_virtual_clock(monkeypatch, clock)
    fake = FakeInverter(clock, flags=SYNC_OFF)

    await _sync_coordinator(fake).async_sync_clock()

    assert fake.flags == SYNC_ON
    assert fake.latched == _target_after(const.CLOCK_SYNC_SETTLE)


@pytest.mark.asyncio
async def test_interrupted_stage_with_sync_off_latches_nothing(monkeypatch):
    """The critical regression: an interrupted write must never reach a commit.

    Originally this guarded a PARTIAL stage — one clock word landing before an
    error, then a falling edge committing the mixture. The block write removes
    that by construction (see test_the_clock_is_written_as_one_frame), so what
    is guarded now is the remaining shape: a write that fails must leave the RTC
    untouched rather than half-changed.

    With Time Sync pre-OFF, a final write of the pre-sequence value is itself a
    FALLING EDGE. If a BLE error interrupted the three clock writes, that edge
    commits a mixture — the words that landed, plus the RTC's own value for the
    ones that did not — i.e. a new date against a stale time, wrong by hours or
    days. Pre-OFF is the state this feature exists to repair, so it is the
    first-run case, not a corner.

    Ending on bit0=1 makes the last write a rising edge, which cannot commit.
    """
    clock = VirtualClock(NOW)
    _install_virtual_clock(monkeypatch, clock)
    fake = FakeInverter(
        clock, flags=SYNC_OFF, rtc=STALE_RTC, fail_write_reg=r.REG_CLOCK,
    )

    with pytest.raises(HomeAssistantError):
        await _sync_coordinator(fake).async_sync_clock()

    assert fake.latched is None                # nothing committed at all
    assert fake.flags & r.TIME_SYNC_MASK       # and calibration left enabled


@pytest.mark.asyncio
async def test_interrupted_stage_with_sync_on_latches_nothing(monkeypatch):
    # The same interruption from the safe starting state. Pinned so the pre-ON
    # path can't regress into issuing a falling edge of its own.
    clock = VirtualClock(NOW)
    _install_virtual_clock(monkeypatch, clock)
    fake = FakeInverter(
        clock, flags=SYNC_ON, rtc=STALE_RTC, fail_write_reg=r.REG_CLOCK,
    )

    with pytest.raises(HomeAssistantError):
        await _sync_coordinator(fake).async_sync_clock()

    assert fake.latched is None
    assert fake.flags == SYNC_ON


@pytest.mark.asyncio
async def test_failed_time_sync_reenable_fails_the_sync(monkeypatch):
    # The commit lands but the re-enable write does not, so cloud calibration is
    # genuinely left disabled. Reporting success there is the same silent-failure
    # shape as the original app bug — the service must fail loudly.
    clock = VirtualClock(NOW)
    _install_virtual_clock(monkeypatch, clock)
    fake = FakeInverter(clock, flags=SYNC_ON, fail_enable_after_commit=True)

    with pytest.raises(HomeAssistantError):
        await _sync_coordinator(fake).async_sync_clock()

    # The clock itself did land — the failure is the flag, and the message has
    # to be able to say so.
    assert fake.latched == _target_after(const.CLOCK_SYNC_SETTLE)


@pytest.mark.asyncio
async def test_a_failed_attempt_gets_a_fresh_link(monkeypatch):
    # A link that just errored must not carry the next attempt. Catching the
    # error INSIDE the session context skips keepalive's drop-on-error, so all
    # three attempts run against the same dead transport and the retry budget
    # is theatre.
    clock = VirtualClock(NOW)
    _install_virtual_clock(monkeypatch, clock)
    built: list[FakeInverter] = []

    def factory(_device):
        # The first link fails every flag write; later ones are healthy.
        broken = not built
        fake = FakeInverter(
            clock, flags=SYNC_ON,
            fail_write_reg=r.REG_TIME_SYNC if broken else None,
        )
        built.append(fake)
        return fake

    coordinator = coord_mod.DeyeBleCoordinator(
        hass=None, address="AA:BB:CC:DD:EE:FF", transport_factory=factory,
        read_only=False, connection_mode="live_local",
    )
    coordinator._resolve_device = lambda: None

    await coordinator.async_sync_clock()

    assert len(built) > 1, "the failed attempt reused its broken link"
    assert built[-1].flags == SYNC_ON


@pytest.mark.asyncio
async def test_sync_publishes_its_outcome(monkeypatch):
    # The 08:00 automation needs a fact, not an inference: drift is a snapshot
    # that is carried forward on a partial poll failure, so it cannot tell a
    # verified sync from a stale reading.
    clock = VirtualClock(NOW)
    _install_virtual_clock(monkeypatch, clock)
    coordinator = _sync_coordinator(FakeInverter(clock, flags=SYNC_ON))

    await coordinator.async_sync_clock()
    data = await _poll(coordinator)

    assert data["clock_sync_result"] == const.SYNC_OK
    assert data["clock_sync_attempts"] == 1
    assert data["clock_sync_at"] is not None


@pytest.mark.asyncio
async def test_outcome_survives_a_failed_poll(monkeypatch):
    """A verified sync followed by a failed poll must still report as verified.

    The outcome is coordinator state, not a device reading. If it were only
    stamped on the success path, one BLE hiccup after a good sync would leave
    the checker seeing no outcome at all — and report a successful sync as a
    failure, which is the alarm-that-cries-wolf this contract exists to avoid.
    """
    clock = VirtualClock(NOW)
    _install_virtual_clock(monkeypatch, clock)
    coordinator = _sync_coordinator(FakeInverter(clock, flags=SYNC_ON))

    # A normal cycle BEFORE the sync, so the carried-forward data predates it.
    await _poll(coordinator)
    assert "clock_sync_result" not in coordinator.data

    await coordinator.async_sync_clock()

    # The refresh the service triggers then fails outright — the exact path the
    # first version of this test missed by polling successfully first, which
    # left the outcome already stamped and the failure path unexercised.
    coordinator._resolve_device = _raise_no_device
    data = await _poll(coordinator)

    assert data["clock_sync_result"] == const.SYNC_OK
    assert data["clock_sync_attempts"] == 1


def _raise_no_device():
    raise RuntimeError("BLE device not found")


@pytest.mark.asyncio
async def test_outcome_survives_the_escalation_failure(monkeypatch):
    """Even the path that gives up entirely must not bury a verified sync.

    Past _max_failures, _handle_poll_failure raises UpdateFailed and returns no
    data at all, so nothing downstream can stamp the outcome. Reachable for
    real: the coordinator is already one failure short of the limit, the sync
    itself succeeds, and its own refresh is the failure that tips it over.
    """
    from homeassistant.helpers.update_coordinator import UpdateFailed

    clock = VirtualClock(NOW)
    _install_virtual_clock(monkeypatch, clock)
    coordinator = _sync_coordinator(FakeInverter(clock, flags=SYNC_ON))
    await _poll(coordinator)
    assert "clock_sync_result" not in coordinator.data

    coordinator._consecutive_failures = coordinator._max_failures - 1
    await coordinator.async_sync_clock()

    coordinator._resolve_device = _raise_no_device
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()

    assert coordinator.data["clock_sync_result"] == const.SYNC_OK


@pytest.mark.asyncio
async def test_sync_notifies_listeners_with_the_outcome_already_applied(monkeypatch):
    """Publishing means entities can SEE it, not just that data was mutated.

    Recording the outcome without telling anyone leaves the entity showing the
    previous value until the next successful poll — which is the same "visible
    only if a later read succeeds" hole the escalation path had. The listener
    also asserts ordering: by the time it runs, the new outcome must already be
    in coordinator.data, or a listener reading it gets the stale one.
    """
    clock = VirtualClock(NOW)
    _install_virtual_clock(monkeypatch, clock)
    coordinator = _sync_coordinator(FakeInverter(clock, flags=SYNC_ON))
    seen: list = []

    def _on_update():
        seen.append(coordinator.data.get("clock_sync_id"))

    coordinator._listeners[_on_update] = (_on_update, None)

    await coordinator.async_sync_clock()

    assert seen == [1]


@pytest.mark.asyncio
async def test_each_sync_publishes_a_new_identity(monkeypatch):
    """The verifier compares identity, never timestamps.

    Both sides of a timestamp comparison would come from the wall clock, and an
    NTP or manual step during the call can make a genuinely new result look
    older than the run that produced it. A counter cannot be stepped backwards
    by the very fault this feature exists to correct.
    """
    clock = VirtualClock(NOW)
    _install_virtual_clock(monkeypatch, clock)
    coordinator = _sync_coordinator(FakeInverter(clock, flags=SYNC_ON))

    await coordinator.async_sync_clock()
    first = coordinator.data["clock_sync_id"]

    # Time runs BACKWARDS between the two syncs — the wall clock is corrected
    # while we are the thing correcting clocks.
    clock.now = NOW - timedelta(hours=1)
    await coordinator.async_sync_clock()
    second = coordinator.data["clock_sync_id"]

    assert second != first
    assert second > first


@pytest.mark.asyncio
async def test_a_mismatched_attempt_keeps_its_link(monkeypatch):
    """The narrow contract, stated precisely: errors drop the link, mismatches don't.

    A verification mismatch does not implicate the link — the read-back came
    back through it. Reconnecting anyway would add churn to a logger with a
    history of wedging on reconnects. Errors are the case that drops (see
    test_a_failed_attempt_gets_a_fresh_link).
    """
    clock = VirtualClock(NOW)
    _install_virtual_clock(monkeypatch, clock)
    built: list[FakeInverter] = []

    def factory(_device):
        # Corrupts the year on the first window only: attempt 1 verifies false
        # without raising, attempt 2 succeeds.
        fake = FakeInverter(clock, flags=SYNC_ON, corrupt_attempts=(1,))
        built.append(fake)
        return fake

    coordinator = coord_mod.DeyeBleCoordinator(
        hass=None, address="AA:BB:CC:DD:EE:FF", transport_factory=factory,
        read_only=False, connection_mode="live_local",
    )
    coordinator._resolve_device = lambda: None

    await coordinator.async_sync_clock()

    assert len(built) == 1, "a mismatch reconnected when it did not need to"
    assert built[0].windows == const.CLOCK_SYNC_ATTEMPTS  # 2 attempts + re-enable


class _FakeEntry:
    """The two fields the entity constructors actually read."""
    data = {"logger_sn": "TESTSN123456"}
    options: dict = {}


@pytest.mark.asyncio
async def test_sensor_attributes_expose_the_sync_outcome(monkeypatch):
    # The automation reads this entity, so the contract has to survive at the
    # entity boundary, not just in coordinator data.
    from custom_components.deye_ble.sensor import DeyeClockSyncResultSensor

    clock = VirtualClock(NOW)
    _install_virtual_clock(monkeypatch, clock)
    coordinator = _sync_coordinator(FakeInverter(clock, flags=SYNC_ON))
    await coordinator.async_sync_clock()
    await _poll(coordinator)

    sensor = DeyeClockSyncResultSensor(coordinator, _FakeEntry())

    assert sensor.native_value == const.SYNC_OK
    assert sensor.extra_state_attributes["sync_attempts"] == 1
    # The identity a verifier compares against what it saw before the call.
    assert sensor.extra_state_attributes["sync_id"] == 1


@pytest.mark.asyncio
async def test_sensor_reports_a_failed_sync_as_failed(monkeypatch):
    # The half nobody looks at: an outcome entity that can only ever say "ok"
    # would satisfy every success test and quietly never raise the alarm.
    from custom_components.deye_ble.sensor import DeyeClockSyncResultSensor

    clock = VirtualClock(NOW)
    _install_virtual_clock(monkeypatch, clock)
    coordinator = _sync_coordinator(
        FakeInverter(clock, flags=SYNC_ON, corrupt_attempts=ALL_WINDOWS)
    )

    with pytest.raises(HomeAssistantError):
        await coordinator.async_sync_clock()

    sensor = DeyeClockSyncResultSensor(coordinator, _FakeEntry())

    assert sensor.native_value in (const.SYNC_FAILED, const.SYNC_CLOCK_WRONG)
    assert sensor.extra_state_attributes["sync_attempts"] == (
        const.CLOCK_SYNC_ATTEMPTS + const.CLOCK_KNOWN_BAD_ATTEMPTS
    )


def test_sensor_attributes_absent_before_any_sync():
    # Unknown, not a fabricated "ok" — a verifier must be able to tell "no sync
    # has run" from "a sync succeeded".
    from custom_components.deye_ble.sensor import DeyeClockSyncResultSensor

    coordinator = _make_coordinator(None)
    coordinator.data = {"inverter_clock": CLOCK_DATETIME}

    sensor = DeyeClockSyncResultSensor(coordinator, _FakeEntry())

    assert sensor.native_value is None
    assert sensor.extra_state_attributes is None


@pytest.mark.asyncio
async def test_sync_outcome_outlives_the_telemetry_going_unavailable(monkeypatch):
    """A verified sync must stay readable when the poll that follows it fails.

    CoordinatorEntity.available follows coordinator.last_update_success, and HA
    strips extra_state_attributes from an unavailable entity — so hanging the
    outcome off a telemetry entity means one failed refresh erases the evidence
    of a sync that actually worked, and the verifier alerts on a success.

    last_update_success is set here exactly as DataUpdateCoordinator sets it
    when a refresh raises UpdateFailed.
    """
    from custom_components.deye_ble.sensor import (
        DeyeClockSyncResultSensor, DeyeInverterClockSensor,
    )

    clock = VirtualClock(NOW)
    _install_virtual_clock(monkeypatch, clock)
    coordinator = _sync_coordinator(FakeInverter(clock, flags=SYNC_ON))
    await coordinator.async_sync_clock()
    await _poll(coordinator)

    coordinator.last_update_success = False  # the refresh after the sync failed

    clock_sensor = DeyeInverterClockSensor(coordinator, _FakeEntry())
    result_sensor = DeyeClockSyncResultSensor(coordinator, _FakeEntry())

    # The telemetry entity is rightly unavailable — it reports what the device
    # currently says, and we can no longer ask.
    assert clock_sensor.available is False
    # The sync result is not a device reading. It stays true, and readable.
    assert result_sensor.available is True
    assert result_sensor.native_value == "ok"
    assert result_sensor.extra_state_attributes["sync_id"] == 1


@pytest.mark.asyncio
async def test_failed_sync_publishes_a_failed_outcome(monkeypatch):
    clock = VirtualClock(NOW)
    _install_virtual_clock(monkeypatch, clock)
    coordinator = _sync_coordinator(
        FakeInverter(clock, flags=SYNC_ON, corrupt_attempts=ALL_WINDOWS)
    )

    with pytest.raises(HomeAssistantError):
        await coordinator.async_sync_clock()
    data = await _poll(coordinator)

    assert data["clock_sync_result"] != const.SYNC_OK
    assert data["clock_sync_attempts"] == (
        const.CLOCK_SYNC_ATTEMPTS + const.CLOCK_KNOWN_BAD_ATTEMPTS
    )


@pytest.mark.asyncio
async def test_sync_waits_before_writing_the_clock_words(monkeypatch):
    # Fake refuses to commit words staged with no gap after sync-on. See
    # FakeInverter.require_settle: this models an UNCONFIRMED hypothesis and
    # exists to keep the delay from being deleted, not to prove the hardware
    # needs it. Setting CLOCK_SYNC_SETTLE to 0 fails here.
    clock = VirtualClock(NOW)
    _install_virtual_clock(monkeypatch, clock)
    fake = FakeInverter(clock, flags=SYNC_ON, require_settle=True)

    await _sync_coordinator(fake).async_sync_clock()

    assert fake.latched == _target_after(const.CLOCK_SYNC_SETTLE)


@pytest.mark.asyncio
async def test_a_cached_readback_cannot_approve_a_corrupt_write(monkeypatch):
    """The false PASS. This is the one that matters.

    The logger serves cached read responses for a variable window of at least
    8 s (measured 2026-08-14). On a normal day the clock is already near-correct
    when a sync starts, so a stale PRE-write frame decodes to a plausible time
    and sails through the tolerance — reporting success over an RTC that the
    write just corrupted to year 2074. Silent, and in the direction nobody
    checks.

    Verifying on elapsed time alone cannot tell that frame from a real one.
    """
    clock = VirtualClock(NOW)
    _install_virtual_clock(monkeypatch, clock)
    fake = FakeInverter(
        clock, flags=SYNC_ON,
        rtc=NOW.replace(tzinfo=None),          # already correct, as on any normal day
        cache_window=8.0,                      # the measured worst case
        corrupt_attempts=ALL_WINDOWS,            # every write lands as year 2074
    )

    with pytest.raises(HomeAssistantError):
        await _sync_coordinator(fake).async_sync_clock()

    # The corruption must have been SEEN, not smoothed over by a stale frame.
    # (The fake models the 0x1A -> 0x7A instance; tonight's was 0x1A -> 0x4A.)
    assert fake.latched is not None
    assert fake.latched.year != NOW.year, "a stale frame approved a corrupt RTC"


@pytest.mark.asyncio
async def test_a_good_sync_still_passes_through_the_cache(monkeypatch):
    # The defence must not cost a correct sync: with the same 8 s window and a
    # clean write, the fresh frames arrive and the sync verifies.
    clock = VirtualClock(NOW)
    _install_virtual_clock(monkeypatch, clock)
    fake = FakeInverter(clock, flags=SYNC_ON, cache_window=8.0)

    await _sync_coordinator(fake).async_sync_clock()

    assert fake.latched == _target_after(const.CLOCK_SYNC_SETTLE)
    assert fake.flags == SYNC_ON


@pytest.mark.asyncio
async def test_sync_retries_a_corrupted_write_with_a_fresh_target(monkeypatch):
    # The observed failure: the year byte landed as 0x7A (2122). The retry must
    # re-derive its target — replaying the first attempt's intent would write a
    # time already stale by a whole attempt.
    clock = VirtualClock(NOW)
    _install_virtual_clock(monkeypatch, clock)
    fake = FakeInverter(clock, flags=SYNC_ON, corrupt_attempts=(1,))

    await _sync_coordinator(fake).async_sync_clock()

    first_target = _target_after(const.CLOCK_SYNC_SETTLE)
    second_target = _target_after(
        const.CLOCK_SYNC_SETTLE, const.CLOCK_COMMIT_SETTLE,
        const.CLOCK_SYNC_SETTLE,
    )
    assert fake.latched == second_target
    assert fake.latched != first_target
    assert fake.flags == SYNC_ON


@pytest.mark.asyncio
async def test_sync_raises_when_every_attempt_fails_and_still_restores(monkeypatch):
    clock = VirtualClock(NOW)
    _install_virtual_clock(monkeypatch, clock)
    fake = FakeInverter(clock, flags=SYNC_ON, corrupt_attempts=ALL_WINDOWS)

    with pytest.raises(HomeAssistantError):
        await _sync_coordinator(fake).async_sync_clock()

    assert fake.flags == SYNC_ON
    # normal budget + bounded known-bad extension + the final enable write
    assert fake.windows == (
        const.CLOCK_SYNC_ATTEMPTS + const.CLOCK_KNOWN_BAD_ATTEMPTS + 1
    )


@pytest.mark.asyncio
async def test_a_known_bad_clock_is_pushed_past_the_normal_budget(monkeypatch):
    """Never walk away from a clock we have just watched read a bad date.

    Nothing else corrects this RTC, so giving up tidily leaves the inverter on
    2074 until the next scheduled run. "Verified wrong" therefore earns a slower
    bounded extension that "could not verify" does not.
    """
    clock = VirtualClock(NOW)
    _install_virtual_clock(monkeypatch, clock)
    # Corrupt only through the normal budget: the extension then succeeds, which
    # is the entire point of having one.
    fake = FakeInverter(
        clock, flags=SYNC_ON,
        corrupt_attempts=tuple(range(1, const.CLOCK_SYNC_ATTEMPTS + 1)),
    )
    coordinator = _sync_coordinator(fake)

    await coordinator.async_sync_clock()

    assert coordinator.data["clock_sync_result"] == const.SYNC_OK
    assert coordinator.data["clock_sync_attempts"] > const.CLOCK_SYNC_ATTEMPTS
    assert fake.latched is not None and fake.latched.year == NOW.year


@pytest.mark.asyncio
async def test_a_clock_left_wrong_is_reported_as_its_own_outcome(monkeypatch):
    # A clock stuck on a bad date must not be reported as a generic failure —
    # that is the silent-failure shape again, one level up.
    clock = VirtualClock(NOW)
    _install_virtual_clock(monkeypatch, clock)
    coordinator = _sync_coordinator(
        FakeInverter(clock, flags=SYNC_ON, corrupt_attempts=ALL_WINDOWS)
    )

    with pytest.raises(HomeAssistantError, match="LEFT WRONG"):
        await coordinator.async_sync_clock()

    assert coordinator.data["clock_sync_result"] == const.SYNC_CLOCK_WRONG


@pytest.mark.asyncio
async def test_an_unverified_sync_does_not_get_the_extension(monkeypatch):
    # Absence of evidence is not evidence of a bad clock. Hammering the inverter
    # when we cannot even read it is the wrong response.
    clock = VirtualClock(NOW)
    _install_virtual_clock(monkeypatch, clock)
    fake = FakeInverter(
        clock, flags=SYNC_ON, fail_read_reg=r.REG_CLOCK,
    )
    coordinator = _sync_coordinator(fake)

    with pytest.raises(HomeAssistantError):
        await coordinator.async_sync_clock()

    assert coordinator.data["clock_sync_result"] == const.SYNC_FAILED
    assert coordinator.data["clock_sync_attempts"] == const.CLOCK_SYNC_ATTEMPTS


@pytest.mark.asyncio
async def test_the_extension_stops_after_its_attempt_budget(monkeypatch):
    # Unbounded retrying against a live inverter is its own hazard.
    clock = VirtualClock(NOW)
    _install_virtual_clock(monkeypatch, clock)
    coordinator = _sync_coordinator(
        FakeInverter(clock, flags=SYNC_ON, corrupt_attempts=ALL_WINDOWS)
    )

    with pytest.raises(HomeAssistantError):
        await coordinator.async_sync_clock()

    assert coordinator.data["clock_sync_attempts"] == (
        const.CLOCK_SYNC_ATTEMPTS + const.CLOCK_KNOWN_BAD_ATTEMPTS
    )


@pytest.mark.asyncio
async def test_the_extension_stops_at_the_time_ceiling(monkeypatch):
    """The ceiling is the SECOND bound, and it needs its own test.

    With the shipped constants the attempt budget always bites first
    (3 x 20 s against a 120 s ceiling), so the ceiling is unreachable and a test
    using them cannot tell whether it exists — a mutation removing it stayed
    green. Raising the attempt budget here makes the ceiling the binding
    constraint, which is the only way to prove it binds at all.
    """
    clock = VirtualClock(NOW)
    _install_virtual_clock(monkeypatch, clock)
    monkeypatch.setattr(coord_mod, "CLOCK_KNOWN_BAD_ATTEMPTS", 100)
    coordinator = _sync_coordinator(
        FakeInverter(clock, flags=SYNC_ON, corrupt_attempts=ALL_WINDOWS)
    )

    with pytest.raises(HomeAssistantError):
        await coordinator.async_sync_clock()

    extension_attempts = (
        coordinator.data["clock_sync_attempts"] - const.CLOCK_SYNC_ATTEMPTS
    )
    # LITERALS, not the constants: a 180 s ceiling with 20 s backoffs and ~3 s
    # of work per attempt admits 7. Comparing against CLOCK_KNOWN_BAD_CEILING /
    # CLOCK_KNOWN_BAD_BACKOFF would move with any mutation of those constants,
    # so the test could never fail — it would be pinning nothing.
    assert extension_attempts == 7, extension_attempts
    assert extension_attempts < 100, "the ceiling did not stop the extension"


@pytest.mark.asyncio
async def test_an_unreadable_drift_syncs_rather_than_skips(monkeypatch):
    # Unknown drift must not be read as "nothing to do". A needless write costs
    # far less than leaving a wrong clock uncorrected because we could not see it.
    clock = VirtualClock(NOW)
    _install_virtual_clock(monkeypatch, clock)
    fake = FakeInverter(
        clock, flags=SYNC_ON, fail_read_reg=r.REG_CLOCK,
    )
    coordinator = _sync_coordinator(fake)

    with pytest.raises(HomeAssistantError):
        await coordinator.async_sync_clock(min_drift=30)

    assert coordinator.data["clock_sync_result"] != const.SYNC_SKIPPED
    assert any(w[0] != r.REG_TIME_SYNC for w in fake.writes), "no sync was attempted"


# --- Drift threshold (the automation's "is this write worth it?") ------------

@pytest.mark.asyncio
async def test_a_small_drift_is_not_worth_a_write(monkeypatch):
    """Every sync is an opportunity to corrupt, so only write when it matters.

    At ~3.1 s/day a 30 s threshold means a real write roughly every ten days
    instead of daily — an order of magnitude less exposure for a clock that is
    still comfortably better than the minute-resolution TOU slots need.
    """
    clock = VirtualClock(NOW)
    _install_virtual_clock(monkeypatch, clock)
    fake = FakeInverter(clock, flags=SYNC_ON, rtc=NOW.replace(tzinfo=None))
    coordinator = _sync_coordinator(fake)

    await coordinator.async_sync_clock(min_drift=30)

    assert coordinator.data["clock_sync_result"] == const.SYNC_SKIPPED
    clock_writes = [w for w in fake.writes if w[0] != r.REG_TIME_SYNC]
    assert clock_writes == [], "wrote the clock despite the drift being tiny"
    assert fake.flags == SYNC_ON  # still left enabled


@pytest.mark.asyncio
async def test_a_skip_that_cannot_re_enable_is_not_a_success(monkeypatch):
    """The silent path: a skipped check still writes Time Sync ON.

    If that write fails, cloud calibration is left disabled — the exact fault
    this feature exists to repair — while the run reports "skipped", which every
    caller treats as fine. Classification must consider the re-enable BEFORE the
    skip, or the failure is invisible.
    """
    clock = VirtualClock(NOW)
    _install_virtual_clock(monkeypatch, clock)
    fake = FakeInverter(
        clock, flags=SYNC_ON,
        rtc=NOW.replace(tzinfo=None),      # inside the threshold, so it skips
        fail_write_reg=r.REG_TIME_SYNC,    # ...and the re-enable cannot land
    )
    coordinator = _sync_coordinator(fake)

    with pytest.raises(HomeAssistantError, match="DISABLED"):
        await coordinator.async_sync_clock(min_drift=30)

    assert coordinator.data["clock_sync_result"] == const.SYNC_FAILED


@pytest.mark.asyncio
async def test_a_transport_error_cannot_erase_a_known_bad_clock(monkeypatch):
    """Sticky known-wrong.

    Attempt 1 reads back a bad date; attempt 2 dies on the link. verdict is
    per-attempt, so without a sticky flag the transport error downgrades a clock
    we have SEEN to be wrong into a generic failure — and skips the extension
    that exists to correct it.
    """
    clock = VirtualClock(NOW)
    _install_virtual_clock(monkeypatch, clock)
    built: list[FakeInverter] = []

    def factory(_device):
        first = not built
        fake = FakeInverter(
            clock, flags=SYNC_ON,
            # First link: corrupts, so attempt 1 verifies WRONG. Later links
            # fail outright, so every later attempt is UNVERIFIED.
            corrupt_attempts=ALL_WINDOWS if first else (),
            fail_write_reg=None if first else r.REG_CLOCK,
        )
        built.append(fake)
        return fake

    coordinator = coord_mod.DeyeBleCoordinator(
        hass=None, address="AA:BB:CC:DD:EE:FF", transport_factory=factory,
        read_only=False,
    )
    coordinator._resolve_device = lambda: None

    with pytest.raises(HomeAssistantError, match="LEFT WRONG"):
        await coordinator.async_sync_clock()

    assert coordinator.data["clock_sync_result"] == const.SYNC_CLOCK_WRONG
    assert coordinator.data["clock_sync_attempts"] > const.CLOCK_SYNC_ATTEMPTS, (
        "the extension was skipped for a clock known to be wrong"
    )


@pytest.mark.asyncio
async def test_cancellation_still_re_enables_time_sync(monkeypatch):
    """HA cancels service calls on shutdown, reload and entry unload.

    This sequence can hold the link for minutes, so being cancelled between the
    commit's falling edge and the re-enable is ordinary. Unshielded, that leaves
    Time Sync OFF — the feature causing the fault it exists to repair — and
    bypasses the outcome record, leaving a verifier with nothing at all.
    """
    clock = VirtualClock(NOW)
    _install_virtual_clock(monkeypatch, clock)
    fake = FakeInverter(clock, flags=SYNC_OFF, cancel_after_commit=True)
    coordinator = _sync_coordinator(fake)

    task = asyncio.ensure_future(coordinator.async_sync_clock())
    with pytest.raises(asyncio.CancelledError):
        await task

    # Let the shielded re-enable finish after the cancellation.
    for _ in range(20):
        await _REAL_SLEEP(0)

    assert fake.flags & r.TIME_SYNC_MASK, "cancelled with Time Sync left OFF"
    assert coordinator.data["clock_sync_result"] == const.SYNC_FAILED, (
        "cancellation left no outcome for a verifier to read"
    )


@pytest.mark.asyncio
async def test_a_large_drift_is_worth_a_write(monkeypatch):
    clock = VirtualClock(NOW)
    _install_virtual_clock(monkeypatch, clock)
    fake = FakeInverter(
        clock, flags=SYNC_ON, rtc=NOW.replace(tzinfo=None) - timedelta(minutes=5),
    )
    coordinator = _sync_coordinator(fake)

    await coordinator.async_sync_clock(min_drift=30)

    assert coordinator.data["clock_sync_result"] == const.SYNC_OK
    assert fake.latched is not None


@pytest.mark.asyncio
async def test_the_dst_step_always_clears_the_threshold(monkeypatch):
    # The requirement that justified the whole feature: the inverter stores a
    # bare wall clock, so October's +1 h has to arrive as a write. 3600 s is far
    # past any sensible threshold, so it needs no special case.
    clock = VirtualClock(NOW)
    _install_virtual_clock(monkeypatch, clock)
    fake = FakeInverter(
        clock, flags=SYNC_ON, rtc=NOW.replace(tzinfo=None) - timedelta(hours=1),
    )
    coordinator = _sync_coordinator(fake)

    await coordinator.async_sync_clock(min_drift=30)

    assert coordinator.data["clock_sync_result"] == const.SYNC_OK


@pytest.mark.asyncio
async def test_sync_while_read_only_is_refused_and_touches_nothing(monkeypatch):
    clock = VirtualClock(NOW)
    _install_virtual_clock(monkeypatch, clock)
    fake = FakeInverter(clock, flags=SYNC_ON)

    with pytest.raises(HomeAssistantError, match="Read Only"):
        await _make_coordinator(fake, read_only=True).async_sync_clock()

    assert fake.ops == []


@pytest.mark.asyncio
async def test_sync_holds_the_ble_lock_across_the_whole_sequence(monkeypatch):
    # _ble_lock is taken per-write elsewhere and released between calls, so a
    # poll could otherwise land inside an open commit window — between sync-on
    # and sync-off — while the RTC registers are half-written.
    clock = VirtualClock(NOW)
    _install_virtual_clock(monkeypatch, clock)
    fake = FakeInverter(clock, flags=SYNC_ON)
    coordinator = _sync_coordinator(fake)

    sync = asyncio.create_task(coordinator.async_sync_clock())
    await asyncio.sleep(0)  # let the sequence start and take the lock
    poll = asyncio.create_task(coordinator._async_update_data())
    await asyncio.gather(sync, poll)

    ops = fake.ops
    opened = next(i for i, o in enumerate(ops)
                  if o[0] == "write" and o[1] == r.REG_TIME_SYNC and o[2] & 1)
    closed = next(i for i, o in enumerate(ops)
                  if o[0] == "write" and o[1] == r.REG_TIME_SYNC and not o[2] & 1)
    telemetry_starts = {start for start, _count in r.LIVE_BLOCKS + r.FAST_BLOCKS}
    intruders = [o for o in ops[opened:closed]
                 if o[0] == "read" and o[1] in telemetry_starts]
    assert intruders == []


@pytest.mark.asyncio
async def test_sync_never_tracks_the_clock_for_reassert(monkeypatch):
    # The RTC legitimately changes every second. In _tracked_values it would be
    # "drifted" on every poll and reassert would fight the inverter forever.
    clock = VirtualClock(NOW)
    _install_virtual_clock(monkeypatch, clock)
    fake = FakeInverter(clock, flags=SYNC_ON)
    coordinator = _sync_coordinator(fake)

    await coordinator.async_sync_clock()

    assert coordinator._tracked_values == {}


@pytest.mark.asyncio
async def test_sync_marks_config_dirty_so_the_drift_sensors_refresh(monkeypatch):
    # The automation verifies the sync by reading the drift sensor; without this
    # it would be checking a snapshot up to CONFIG_READ_INTERVAL old.
    clock = VirtualClock(NOW)
    _install_virtual_clock(monkeypatch, clock)
    fake = FakeInverter(clock, flags=SYNC_ON)
    coordinator = _sync_coordinator(fake)
    coordinator._config_dirty = False

    await coordinator.async_sync_clock()

    assert coordinator._config_dirty is True


@pytest.mark.asyncio
async def test_a_second_cancellation_cannot_abort_the_re_enable(monkeypatch):
    """Shielding, tested at the moment it matters.

    A cancellation arriving BEFORE the re-enable starts is survivable without
    shielding — the earlier await consumed it. The dangerous case is a second
    cancellation while the re-enable is in flight: unshielded, the write is
    cancelled with it and Time Sync is left OFF after the commit's falling edge.
    """
    clock = VirtualClock(NOW)
    _install_virtual_clock(monkeypatch, clock)
    fake = FakeInverter(clock, flags=SYNC_OFF)
    coordinator = _sync_coordinator(fake)

    task = asyncio.ensure_future(coordinator.async_sync_clock())
    fake.cancel_on_enable = task
    with pytest.raises((asyncio.CancelledError, HomeAssistantError)):
        await task

    for _ in range(20):
        await _REAL_SLEEP(0)

    assert fake.flags & r.TIME_SYNC_MASK, (
        "the re-enable was cancelled with the task, leaving Time Sync OFF"
    )


@pytest.mark.asyncio
async def test_known_wrong_survives_a_later_unverified_attempt(monkeypatch):
    """The sticky flag needs an UNVERIFIED attempt that does not raise.

    A transport error skips the assignment altogether, so it cannot tell a
    sticky flag from a per-attempt one. A clean attempt that merely fails to
    verify does: without stickiness it overwrites the knowledge that an earlier
    attempt read a bad date.
    """
    clock = VirtualClock(NOW)
    _install_virtual_clock(monkeypatch, clock)
    built: list[FakeInverter] = []

    def factory(_device):
        first = not built
        fake = FakeInverter(
            clock, flags=SYNC_ON,
            # First attempt commits a bad year and reports WRONG. Later ones
            # never yield a fresh frame, so they report UNVERIFIED without
            # raising anything.
            corrupt_attempts=ALL_WINDOWS if first else (),
            # Later attempts return an UNDECODABLE frame: unverified, but with
            # no exception, so the sticky assignment is actually reached.
            undecodable_clock=not first,
        )
        built.append(fake)
        return fake

    coordinator = coord_mod.DeyeBleCoordinator(
        hass=None, address="AA:BB:CC:DD:EE:FF", transport_factory=factory,
        read_only=False,
    )
    coordinator._resolve_device = lambda: None

    with pytest.raises(HomeAssistantError, match="LEFT WRONG"):
        await coordinator.async_sync_clock()

    assert coordinator.data["clock_sync_result"] == const.SYNC_CLOCK_WRONG


# --- The lesson from the 2026-08-15 probe -----------------------------------

@pytest.mark.asyncio
async def test_a_ticking_clock_alone_never_verifies_a_commit(monkeypatch):
    """The regression test for the design that was deleted.

    Run 1 of the probe: 14 DISTINCT frames across 68 s, and the commit had never
    landed. The RTC ticks, so frames differ because time passed — a changed
    frame carries no information about our write. Any verification resting on
    "the frame changed" passes here while nothing was written.

    Modelled by a fake that acks the clock writes and never commits them.
    """
    clock = VirtualClock(NOW)
    _install_virtual_clock(monkeypatch, clock)
    fake = FakeInverter(
        clock, flags=SYNC_ON,
        rtc=STALE_RTC,          # a year out, so content-vs-intent can see it
        never_commits=True,     # writes acked, falling edge does nothing
    )
    coordinator = _sync_coordinator(fake)

    with pytest.raises(HomeAssistantError):
        await coordinator.async_sync_clock()

    assert fake.latched is None, "the fake committed when it was told not to"
    assert coordinator.data["clock_sync_result"] == const.SYNC_CLOCK_WRONG


@pytest.mark.asyncio
async def test_a_borderline_drift_is_written_not_skipped(monkeypatch):
    """The staleness margin, at the boundary it exists for.

    A reading can be up to CLOCK_CACHE_MAX_AGE old, so a drift that clears the
    threshold only within that margin has not really cleared it.
    """
    clock = VirtualClock(NOW)
    _install_virtual_clock(monkeypatch, clock)
    borderline = 30 - int(const.CLOCK_CACHE_MAX_AGE) + 1   # 21 s against a 30 s rule
    fake = FakeInverter(
        clock, flags=SYNC_ON,
        rtc=NOW.replace(tzinfo=None) - timedelta(seconds=borderline),
    )
    coordinator = _sync_coordinator(fake)

    await coordinator.async_sync_clock(min_drift=30)

    assert coordinator.data["clock_sync_result"] == const.SYNC_OK, (
        "a drift inside the staleness margin was treated as definitely fine"
    )




@pytest.mark.asyncio
async def test_an_undecodable_readback_is_unverified_not_wrong(monkeypatch):
    """Absence of evidence, not evidence of a bad clock.

    A frame that decodes to nothing says we cannot see what the RTC holds. That
    must not earn the bounded extension, which exists for clocks we have
    actually watched read a bad date — hammering an inverter we cannot read is
    the wrong response to not knowing.
    """
    clock = VirtualClock(NOW)
    _install_virtual_clock(monkeypatch, clock)
    coordinator = _sync_coordinator(
        FakeInverter(clock, flags=SYNC_ON, undecodable_clock=True)
    )

    with pytest.raises(HomeAssistantError):
        await coordinator.async_sync_clock()

    assert coordinator.data["clock_sync_result"] == const.SYNC_FAILED
    assert coordinator.data["clock_sync_attempts"] == const.CLOCK_SYNC_ATTEMPTS


@pytest.mark.asyncio
async def test_the_clock_is_written_as_one_frame(monkeypatch):
    """A correctness property with hardware evidence, not a style preference.

    Interleaved trial, 2026-08-15, only the frame shape varying: three separate
    quantity-1 writes to 0x003E-0x0040 corrupted the year byte in 4 of 5
    attempts; one contiguous quantity-3 frame corrupted 0 of 5. Every clean
    block write sat between two failing single writes, so drift in the logger's
    state cannot explain it.

    Writing the clock as three frames would reintroduce an 80% fault rate AND
    the partial-stage hazard that the first critical finding was about.
    """
    clock = VirtualClock(NOW)
    _install_virtual_clock(monkeypatch, clock)
    fake = FakeInverter(clock, flags=SYNC_ON)

    await _sync_coordinator(fake).async_sync_clock()

    frames = fake.clock_frames
    assert len(frames) == 1, f"clock written as {len(frames)} frames, must be 1"
    kind, address, values = frames[0]
    assert kind == "write_block"
    assert address == r.REG_CLOCK
    assert values == encode_clock(_target_after(const.CLOCK_SYNC_SETTLE))


@pytest.mark.asyncio
async def test_a_cancelled_re_enable_does_not_race_a_poll(monkeypatch):
    """Cleanup must stay SERIALIZED, not merely eventual.

    An earlier fix shielded the re-enable so a cancellation could not abort it.
    That kept the write alive but let it run outside the BLE lock — and the
    logger accepts one central, so it could collide with a poll or a later sync,
    with its exception seen by nobody. A write that escapes the lock is not a
    smaller problem than one that dies; it is a less visible one.

    The fake now raises if a second session opens while one is live, which is
    what makes this expressible at all.
    """
    clock = VirtualClock(NOW)
    _install_virtual_clock(monkeypatch, clock)
    fake = FakeInverter(clock, flags=SYNC_OFF)
    coordinator = _sync_coordinator(fake)

    sync = asyncio.ensure_future(coordinator.async_sync_clock())
    fake.cancel_on_enable = sync          # cancel DURING the re-enable
    with pytest.raises((asyncio.CancelledError, HomeAssistantError)):
        await sync

    # A poll starts immediately afterwards, as HA's own timer would — while the
    # deferred re-enable is still pending.
    poll = asyncio.ensure_future(coordinator._async_update_data())
    for _ in range(50):
        await _REAL_SLEEP(0)
    await poll
    if coordinator._pending_enable is not None:
        await coordinator._pending_enable

    assert fake.max_concurrent_sessions == 1, "two BLE centrals at once"
    assert fake.flags & r.TIME_SYNC_MASK, "cancelled with Time Sync left OFF"


@pytest.mark.asyncio
async def test_a_surviving_cache_fails_loud_rather_than_approving(monkeypatch):
    """The adversarial case the hardware has not shown, pinned anyway.

    If the logger's read cache ever survived a write, the read-back would be a
    pre-write frame: plausible content, decoding cleanly, and — on a normal day
    when the clock was already near-right — inside the tolerance. The content
    check alone would report success over an RTC the write had just corrupted.

    The guard is a negative inference: the RTC ticks, so a byte-identical frame
    cannot be a fresh read of a changed clock. It costs one comparison and it
    fails LOUD, as unverified.
    """
    clock = VirtualClock(NOW)
    _install_virtual_clock(monkeypatch, clock)
    fake = FakeInverter(
        clock, flags=SYNC_ON,
        rtc=NOW.replace(tzinfo=None),        # already right, as on any normal day
        # A cache that survives the write AND keeps serving: every read-back is
        # the pre-write frame, so the guard must fire on every attempt. (With a
        # window that expires mid-run the guard fires, a later fresh frame then
        # exposes the corruption and the result is clock_wrong — also correct,
        # but a weaker thing to assert.)
        cache_window=10_000.0,
        cache_survives_write=True,
        corrupt_attempts=ALL_WINDOWS,        # ...while the write corrupts the year
    )
    coordinator = _sync_coordinator(fake)

    with pytest.raises(HomeAssistantError):
        await coordinator.async_sync_clock()

    # Unverified, not "ok" — and not clock_wrong either, since a cached frame is
    # not evidence about the RTC. Without the guard this reports SYNC_OK on the
    # first attempt, over an RTC holding a corrupt year.
    assert coordinator.data["clock_sync_result"] == const.SYNC_FAILED
    assert coordinator.data["clock_sync_attempts"] == const.CLOCK_SYNC_ATTEMPTS, (
        "an unverifiable clock must not earn the known-wrong extension"
    )
    assert fake.latched is not None and fake.latched.year != NOW.year


@pytest.mark.asyncio
async def test_a_primed_cache_still_catches_the_corruption(monkeypatch):
    """The path the old test missed: the automation primes the cache.

    Going through the SAME entry point the automation uses — sync_clock with a
    min_drift — means _read_drift reads the clock before the write, so the cache
    is filled with a pre-write frame. That is the condition under which a
    surviving cache would matter, and the one the previous regression never
    reached because it never read before writing.
    """
    clock = VirtualClock(NOW)
    _install_virtual_clock(monkeypatch, clock)
    fake = FakeInverter(
        clock, flags=SYNC_ON,
        rtc=NOW.replace(tzinfo=None) - timedelta(minutes=5),  # far enough to sync
        cache_window=8.0,
        corrupt_attempts=ALL_WINDOWS,
    )
    coordinator = _sync_coordinator(fake)

    with pytest.raises(HomeAssistantError, match="LEFT WRONG"):
        await coordinator.async_sync_clock(min_drift=30)

    assert coordinator.data["clock_sync_result"] == const.SYNC_CLOCK_WRONG
    assert fake.latched is not None and fake.latched.year != NOW.year


@pytest.mark.asyncio
async def test_fake_rejects_two_concurrent_sessions():
    """Characterises the fake's connection model, which is load-bearing.

    The logger accepts ONE central. Until the fake enforced that, a write
    escaping the coordinator's BLE lock was INEXPRESSIBLE — reviewable by
    reading the code, invisible to the suite. This pins the enforcement so it
    cannot quietly lapse.
    """
    clock = VirtualClock(NOW)
    fake = FakeInverter(clock, flags=SYNC_ON)

    async with fake:
        with pytest.raises(AssertionError, match="single central"):
            async with fake:
                pass


@pytest.mark.asyncio
async def test_a_slow_attempt_cannot_run_past_the_ceiling(monkeypatch):
    """The ceiling must bound the WORK, not just the waiting.

    Checking the deadline only before sleeping lets an attempt that began just
    under it run on through handshake, command and read — which is precisely the
    hang a wall-clock stop exists for. Run in real time with a deliberately slow
    transport, because a virtual clock cannot express an operation that takes
    longer than the code thinks it does.
    """
    for name, value in (
        ("CLOCK_KNOWN_BAD_BACKOFF", 0.05), ("CLOCK_KNOWN_BAD_CEILING", 0.3),
        ("CLOCK_SYNC_SETTLE", 0.0), ("CLOCK_COMMIT_SETTLE", 0.0),
        # One normal attempt, so the measurement is dominated by the EXTENSION
        # rather than by the ordinary budget, which the ceiling does not govern.
        ("CLOCK_SYNC_ATTEMPTS", 1),
    ):
        monkeypatch.setattr(coord_mod, name, value)

    clock = VirtualClock(NOW)
    monkeypatch.setattr(coord_mod.dt_util, "now", lambda *_a, **_kw: clock.now)
    # Each BLE operation takes 0.3 s, so a whole attempt takes far longer than
    # the 0.3 s ceiling — the case where bounding only the sleep does nothing.
    fake = FakeInverter(
        clock, flags=SYNC_ON, corrupt_attempts=ALL_WINDOWS, io_delay=0.3,
    )
    coordinator = _sync_coordinator(fake)

    started = asyncio.get_running_loop().time()
    with pytest.raises(HomeAssistantError):
        await coordinator.async_sync_clock()
    spent = asyncio.get_running_loop().time() - started

    # One normal attempt (~2 s of slow I/O) plus a bounded extension. Without
    # the per-attempt budget the extension adds another full slow attempt.
    assert spent < 3.5, f"the extension ran {spent:.1f}s past a 0.3s ceiling"
