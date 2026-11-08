"""Pure helpers — no HA imports, so they stay unit-testable without HA.

Holds the BLE poll orchestration (async_poll), logger-SN validation, and the
daily-baseline calculation. The HA-coupled DeyeBleCoordinator (coordinator.py)
imports async_poll from here.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Mapping, Protocol

from . import codec
from . import models as r
from .models.base import Point, Select
from .const import (
    CONF_CONNECTION_MODE,
    CONF_LIVE_INTERVAL,
    CONF_PROFILE,
    CONF_READ_ONLY,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    LEGACY_CONF_DRY_RUN,
    LEGACY_CONF_KEEPALIVE,
    MAX_LIVE_INTERVAL,
    MIN_LIVE_INTERVAL,
    MODE_LIVE_LOCAL,
    MODE_STANDARD,
    PROFILE_AUTO,
)

_LOGGER = logging.getLogger(__name__)


# --- Transport protocol (duck-typed, not bleak-dependent) -------------------

class Transport(Protocol):
    async def handshake(self) -> None: ...
    async def read(self, address: int, count: int) -> list[int]: ...
    async def write(self, address: int, value: int) -> None: ...


# --- Pure async poll (unit-testable with a fake transport) ------------------

async def async_detect_profile(transport: Transport) -> str:
    """Read the register-0 device type and map it to a register profile."""
    await transport.handshake()
    (device_type,) = await transport.read(r.REG_DEVICE_TYPE, 1)
    return r.profile_from_device_type(device_type)


async def async_read_tiers(
    transport: Transport,
    *,
    profile: str = r.PROFILE_THREE_PHASE,
    tiers=r.ALL_TIERS,
) -> tuple[dict[str, Any], bool]:
    """Handshake, read the blocks of *tiers* for *profile* and decode them.

    LIVE and FAST read failures propagate (the poll has failed). SLOW reads are
    non-fatal: a failure is logged and the affected keys are simply omitted, so
    a settings hiccup never discards a good telemetry cycle. The caller carries
    forward values from tiers not read this cycle.

    Returns ``(data, slow_complete)``: False when any slow block failed, so the
    caller retries the slow tier instead of acknowledging it.
    """
    await transport.handshake()

    model = r.get(profile)
    blocks = model.read_blocks(tiers)
    words_by_reg: dict[int, list[int]] = {}
    slow_complete = True
    for tier in (r.TIER_LIVE, r.TIER_FAST, r.TIER_SLOW):
        for start, count in blocks[tier]:
            try:
                words_by_reg[start] = await transport.read(start, count)
            except Exception as e:  # noqa: BLE001 — slow read is best-effort
                if tier != r.TIER_SLOW:
                    raise
                slow_complete = False
                _LOGGER.debug("slow block 0x%04X read failed (non-fatal): %s", start, e)

    return model.decode(words_by_reg), slow_complete


async def async_poll(transport: Transport, **kwargs) -> dict[str, Any]:
    """:func:`async_read_tiers` without the slow-completion flag."""
    data, _slow_complete = await async_read_tiers(transport, **kwargs)
    return data


def apply_profile_choice(data: Mapping[str, Any], choice: str) -> dict[str, Any] | None:
    """New entry.data for a profile picked in Options, or None if unchanged.

    A pinned profile overrides auto-detection (a safety net if the device type
    misreads at setup). PROFILE_AUTO drops the cached profile so the next setup
    re-detects. Any change needs a reload: the entity set differs per profile.
    """
    if choice == data.get(CONF_PROFILE, PROFILE_AUTO):
        return None
    new = dict(data)
    if choice == PROFILE_AUTO:
        new.pop(CONF_PROFILE, None)
    else:
        new[CONF_PROFILE] = choice
    return new


def migrate_options(options: Mapping[str, Any]) -> dict[str, Any] | None:
    """Upgrade pre-0.2 options, or None when there is nothing to migrate.

    ``dry_run`` becomes ``read_only`` unchanged. ``keepalive`` becomes a
    connection mode: off is STANDARD; on is LIVE_LOCAL (a held link with no
    quiet window, as before) polling at the old scan interval, so nothing about
    an upgraded install's behaviour changes.
    """
    if LEGACY_CONF_DRY_RUN not in options and LEGACY_CONF_KEEPALIVE not in options:
        return None
    new = dict(options)
    if LEGACY_CONF_DRY_RUN in new:
        new.setdefault(CONF_READ_ONLY, new.pop(LEGACY_CONF_DRY_RUN))
    if LEGACY_CONF_KEEPALIVE in new:
        if new.pop(LEGACY_CONF_KEEPALIVE):
            new.setdefault(CONF_CONNECTION_MODE, MODE_LIVE_LOCAL)
            interval = new.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
            new.setdefault(
                CONF_LIVE_INTERVAL, max(MIN_LIVE_INTERVAL, min(MAX_LIVE_INTERVAL, interval))
            )
        else:
            new.setdefault(CONF_CONNECTION_MODE, MODE_STANDARD)
    return new


# --- Grid-connection inference ----------------------------------------------

# Nominal grid is ~230 V/phase; off-grid the sensed grid voltage collapses to
# near zero (the inverter's own output/load voltage stays up, but that is a
# different register). 100 V cleanly separates "energised" from "collapsed".
GRID_PRESENT_VOLTAGE = 100.0
# Some outages leave the grid voltage registers up (2026-11-01 07:44 UTC: all
# phases held ~229.6 V) while grid frequency and power read 0. Nominal is 50/60 Hz.
GRID_PRESENT_FREQUENCY = 40.0

# Three-phase L1-L3, single-phase "grid_voltage".
_GRID_VOLTAGE_KEYS = ("grid_voltage_l1", "grid_voltage_l2", "grid_voltage_l3", "grid_voltage")


def infer_grid_connected(data: dict[str, Any]) -> bool | None:
    """Infer grid-connection state; every reported signal must look healthy.

    ``False`` if the grid relay is open (single-phase register 194), grid
    frequency is below ``GRID_PRESENT_FREQUENCY``, or no phase is energised
    above ``GRID_PRESENT_VOLTAGE``. ``True`` if the voltage (or, without one,
    the relay) says so. ``None`` if nothing was reported (unknown — never a
    false "disconnected").
    """
    relay = data.get("grid_connected")
    if relay is False:
        return False
    frequency = data.get("grid_frequency")
    if frequency is not None and frequency < GRID_PRESENT_FREQUENCY:
        return False
    voltages = [data[k] for k in _GRID_VOLTAGE_KEYS if data.get(k) is not None]
    if not voltages:
        return relay
    return max(voltages) > GRID_PRESENT_VOLTAGE


# --- Inverter clock drift ----------------------------------------------------

def clock_drift_seconds(
    inverter_clock: datetime | None,
    now: datetime,
) -> int | None:
    """Seconds the inverter RTC is ahead of *now* (negative = running behind).

    Both sides are site wall clock: the inverter reports a bare local time, and
    *now* is HA's local time. A timezone on *now* is dropped rather than used to
    convert, since there is nothing on the other side to convert against.

    Returns ``None`` when the clock could not be read, so a missing reading is
    never reported as zero drift.

    Callers must compute this at the moment a *fresh* clock is decoded and carry
    the result forward with it. Recomputing against a carried-forward clock would
    make drift climb one second per second between config reads — a permanently
    alarming number that says nothing about the inverter.
    """
    if inverter_clock is None:
        return None
    if now.tzinfo is not None:
        now = now.replace(tzinfo=None)
    return round((inverter_clock - now).total_seconds())


_SECONDS_PER_DAY = 24 * 60 * 60


def time_of_day_drift_seconds(
    inverter_clock: datetime | None,
    now: datetime,
) -> int | None:
    """Wall-clock-of-day drift in seconds, ignoring the date entirely.

    Same sign convention and same snapshot rules as :func:`clock_drift_seconds`
    (positive = inverter ahead, ``None`` = unread, measure it once against a
    fresh reading), but the date is discarded.

    This exists because the two faults are independent: an RTC that is a year
    out AND ~90 minutes fast reports a total drift of about -365 days, in which
    the minutes are invisible. A repeating time-of-day offset is the fingerprint
    of something actively rewriting the clock, so it needs its own number.

    The result is wrapped to the shortest way round the 24h circle, i.e. always
    within +/-12h: 00:01 against 23:59 is +120 s, never -86280. Without that, a
    tiny offset straddling midnight would read as a near-24h jump — exactly what
    a rogue writer looks like. Exactly 12h apart resolves to +43200.
    """
    if inverter_clock is None:
        return None
    if now.tzinfo is not None:
        now = now.replace(tzinfo=None)

    def _seconds_into_day(value: datetime) -> int:
        return value.hour * 3600 + value.minute * 60 + value.second

    delta = (_seconds_into_day(inverter_clock) - _seconds_into_day(now)) % _SECONDS_PER_DAY
    if delta > _SECONDS_PER_DAY // 2:
        delta -= _SECONDS_PER_DAY
    return delta


def clock_within_tolerance(
    words: list[int],
    now: datetime,
    tolerance: int,
) -> bool:
    """Whether an RTC readback is close enough to *now* to call the write good.

    The clock is the one control that cannot use the strict
    :func:`verify_readback` every other register uses: it ticks throughout the
    commit sequence, so the readback is *expected* to differ from what was
    written. It is judged on drift instead — accepted when within *tolerance*
    seconds of a freshly taken *now*.

    An undecodable frame is a failure, never a pass: the observed bad write
    (year byte 0x1A landing as 0x7A, giving 2122) decodes fine and is caught by
    the tolerance, but a frame that decodes to nothing tells us the write cannot
    be confirmed, and unconfirmed must not read as confirmed.
    """
    clock = codec.decode_clock(words)
    if clock is None:
        return False
    drift = clock_drift_seconds(clock, now)
    return abs(drift) <= tolerance


# --- Logger SN validation ----------------------------------------------------

def validate_logger_sn(sn: str) -> str:
    """Normalise and validate a Deye logger serial number.

    Returns the stripped uppercased string on success, raises ValueError
    otherwise. Valid SNs are alphanumeric, 8-20 characters after stripping.
    """
    sn = sn.strip().upper()
    if not 8 <= len(sn) <= 20:
        raise ValueError("Logger serial must be 8-20 characters")
    if not sn.isalnum():
        raise ValueError("Logger serial must be alphanumeric")
    return sn


# --- Control register ↔ entity key mapping --------------------------------

def register_to_key(reg: int) -> str | None:
    """Map a control register address to its entity key, or None."""
    return next((
        point.key
        for point in r.get(r.PROFILE_THREE_PHASE).points
        if point.write is not None and reg in point.write.registers
    ), None)


def key_to_register(key: str) -> int | None:
    """Map an entity key to its control register address, or None."""
    return r.get(r.PROFILE_THREE_PHASE).controls.get(key)


# --- Read-back verification ------------------------------------------------

class ReadbackError(ValueError):
    """Raised when a write was acked but the device read-back differs."""


def verify_readback(reg: int, expected: int, actual: int) -> None:
    """Compare a write's expected value against the device read-back.

    Registers are 16-bit, and ``parse_read`` returns the raw *unsigned* word.
    Comparing on the 16-bit wire value (``& 0xFFFF``) means a signed write such
    as ``-30`` (wire ``0xFFE2``) verifies against its unsigned read-back
    (``65506``) without any per-register signedness plumbing.

    Returns ``None`` on match.  Raises :class:`ReadbackError` on mismatch.
    """
    if (expected & 0xFFFF) != (actual & 0xFFFF):
        raise ReadbackError(
            f"readback mismatch at 0x{reg:04X}: expected {expected}, got {actual}"
        )


# --- Drift detection (for local-wins reassert) ----------------------------

def detect_drift(
    tracked: dict[int, int | str],
    current: dict[str, float | int | str],
    reg_to_key: Mapping[int, str | Point] | None = None,
) -> list[tuple[int, int | str]]:
    """Compare last HA-set register values against current device values.

    Tracked values are the raw ints passed to ``async_write``.  For comparison,
    enum ints (work mode etc.) are converted to their decoded string label.
    Other registers stay as ints. *reg_to_key* maps registers to their model
    points or data keys; the default is the three-phase model's writable points.

    Returns a list of ``(register, raw_tracked_value)`` pairs that have drifted.
    Registers not in the control set are ignored.  Missing keys in *current*
    are silently skipped (can't compare what isn't reported).
    """
    drifted: list[tuple[int, int | str]] = []
    default_points = r.get(r.PROFILE_THREE_PHASE).reassert_points
    for reg, expected in tracked.items():
        descriptor = (
            reg_to_key.get(reg) if reg_to_key is not None else default_points.get(reg)
        )
        if descriptor is None:
            continue
        key = descriptor if isinstance(descriptor, str) else descriptor.key
        if key not in current:
            continue
        compare_val = expected
        if isinstance(descriptor, Point):
            options = next((
                show.options for show in descriptor.shows if isinstance(show, Select)
            ), None)
        else:
            options = next((
                show.options
                for model in r.MODELS
                for point in model.points
                if point.key == key
                for show in point.shows
                if isinstance(show, Select)
            ), None)
        if options is not None and isinstance(expected, int):
            compare_val = options.get(expected, f"Unknown ({expected})")
        if current[key] != compare_val:
            drifted.append((reg, expected))
    return drifted


# --- Spurious lifetime-total reset guard ------------------------------------

# Lifetime cumulative energy registers (kWh) that only ever climb. A BLE frame
# occasionally decodes one (or several) as 0; feeding that 0 to a
# total_increasing sensor makes HA statistics treat it as a meter reset and then
# count the recovery back up as a phantom spike (one 0-blip cost ~2.6 MWh of
# fake grid import overnight). These keys are held at their last good value
# until the device reports a real number again.
_MONOTONIC_TOTAL_KEYS = (
    "total_solar",
    "total_grid_import",
    "total_grid_export",
    "total_battery_charge",
    "total_battery_discharge",
    "total_consumption",
)


def hold_spurious_total_resets(
    prev: dict[str, Any] | None,
    current: dict[str, Any],
) -> dict[str, Any]:
    """Hold lifetime totals that spuriously read 0 at their last good value.

    A lifetime total dropping to ``0`` (or below) while the previous cycle had a
    positive reading is a bad BLE frame, not a real meter reset. Such keys are
    overwritten in *current* with the previous value so the downstream
    total_increasing sensors never see the false reset. All other keys — and any
    genuine positive reading — pass through untouched. Mutates and returns
    *current*.
    """
    if not prev:
        return current
    for key in _MONOTONIC_TOTAL_KEYS:
        new = current.get(key)
        old = prev.get(key)
        if new is not None and old is not None and new <= 0 < old:
            _LOGGER.warning(
                "deye: ignoring spurious %s=%s (holding last good %s)",
                key, new, old,
            )
            current[key] = old
    return current


# --- Daily baseline calculation --------------------------------------------

def daily_calc(
    baseline: float | None,
    day: date | None,
    total: float | None,
    today: date,
) -> tuple[float | None, date | None, float | None]:
    """Compute today's consumption from a lifetime cumulative total.

    Returns ``(new_baseline, new_day, daily_value)`` — callers persist
    *baseline* and *day* across coordinator updates and HA restarts.

    Rules (mirrors DeyeDailyGridImportSensor logic in the cloud bridge):
    - If *total* is ``None`` → return unchanged baseline/day, value ``None``.
    - New day, first run, or the meter counter went backwards → rebaseline.
    - Otherwise value = ``total - baseline``.
    """
    if total is None:
        return baseline, day, None

    if day != today or baseline is None or total < baseline:
        baseline = total
        day = today

    value = round(total - baseline, 2)
    return baseline, day, value
