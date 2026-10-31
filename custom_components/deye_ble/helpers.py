"""Pure helpers — no HA imports, so they stay unit-testable without HA.

Holds the BLE poll orchestration (async_poll), logger-SN validation, and the
daily-baseline calculation. The HA-coupled DeyeBleCoordinator (coordinator.py)
imports async_poll from here.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Protocol

from . import registers as r

_LOGGER = logging.getLogger(__name__)


# --- Transport protocol (duck-typed, not bleak-dependent) -------------------

class Transport(Protocol):
    async def handshake(self) -> None: ...
    async def read(self, address: int, count: int) -> list[int]: ...
    async def write(self, address: int, value: int) -> None: ...


# --- Pure async poll (unit-testable with a fake transport) ------------------

async def async_poll(
    transport: Transport,
    *,
    with_config: bool = False,
    profile: str | None = r.PROFILE_THREE_PHASE,
) -> dict[str, Any]:
    """Connect, handshake, read all register blocks, decode and return.

    Telemetry-block read failures propagate (the poll has failed). Control-block
    reads (only when *with_config*) are non-fatal: a failure is logged and the
    affected config keys are simply omitted, so a config hiccup never discards a
    good telemetry cycle. The caller carries forward the previous config values.

    *profile* selects the register map; ``None`` detects it from the device-type
    register first. The result carries it as ``device_profile`` so the caller
    can pass it back and skip the detection read on later polls.
    """
    await transport.handshake()

    if profile is None:
        (device_type,) = await transport.read(r.REG_DEVICE_TYPE, 1)
        profile = r.profile_from_device_type(device_type)

    words_by_reg: dict[int, list[int]] = {}
    if profile == r.PROFILE_SINGLE_PHASE:
        # Monitoring-only profile: settings are read, never the control blocks.
        for start, count in r.SP_READ_BLOCKS:
            words_by_reg[start] = await transport.read(start, count)
        if with_config:
            for start, count in r.SP_CONFIG_BLOCKS:
                try:
                    words_by_reg[start] = await transport.read(start, count)
                except Exception as e:  # noqa: BLE001 — config read is best-effort
                    _LOGGER.debug("config block %d read failed (non-fatal): %s", start, e)
        data = r.decode_single_phase(words_by_reg)
        data["device_profile"] = profile
        return data

    for start, count in r.READ_BLOCKS:
        words_by_reg[start] = await transport.read(start, count)

    if with_config:
        for start, count in r.CONTROL_BLOCKS:
            try:
                words_by_reg[start] = await transport.read(start, count)
            except Exception as e:  # noqa: BLE001 — config read is best-effort
                _LOGGER.debug("config block 0x%04X read failed (non-fatal): %s", start, e)

    data = r.decode(words_by_reg)
    data["device_profile"] = profile
    return data


# --- Grid-connection inference ----------------------------------------------

# Nominal grid is ~230 V/phase; off-grid the sensed grid voltage collapses to
# near zero (the inverter's own output/load voltage stays up, but that is a
# different register). 100 V cleanly separates "energised" from "collapsed".
GRID_PRESENT_VOLTAGE = 100.0

_GRID_VOLTAGE_KEYS = ("grid_voltage_l1", "grid_voltage_l2", "grid_voltage_l3")


def infer_grid_connected(data: dict[str, Any]) -> bool | None:
    """Infer grid-connection state from the per-phase grid voltages.

    Returns ``True`` if any phase is energised above ``GRID_PRESENT_VOLTAGE``,
    ``False`` if all reported phases are below it, and ``None`` if no grid
    voltage was reported (unknown — never a false "disconnected").
    """
    if data.get("grid_connected") is not None:
        # Single-phase profile reads the grid relay (register 194) directly.
        return data["grid_connected"]
    voltages = [data[k] for k in _GRID_VOLTAGE_KEYS if data.get(k) is not None]
    if not voltages:
        return None
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
    clock = r.decode_clock(words)
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

# Register -> entity key for every writable control register. The five
# non-charge TOU slots all map to "discharge_soc" so reassert can re-apply the
# floor if the cloud/app drifts any of them.
_REG_TO_KEY: dict[int, str] = {
    r.REG_WORK_MODE: "work_mode",
    r.REG_MAX_SELL_POWER: "max_sell_power",
    r.REG_ZERO_EXPORT_POWER: "zero_export_power",
    r.REG_TOU_SLOT2_START: "charge_start",
    r.REG_TOU_SLOT3_START: "charge_end",
    r.REG_CHARGE_SOC: "charge_soc",
    **{reg: "discharge_soc" for reg in r.DISCHARGE_SOC_REGS},
}
# Reverse map for the single-register controls (discharge_soc spans many slots
# and is written explicitly, so it is intentionally not reversible here).
_KEY_TO_REG: dict[str, int] = {
    v: k for k, v in _REG_TO_KEY.items() if v != "discharge_soc"
}


def register_to_key(reg: int) -> str | None:
    """Map a control register address to its entity key, or None."""
    return _REG_TO_KEY.get(reg)


def key_to_register(key: str) -> int | None:
    """Map an entity key to its control register address, or None."""
    return _KEY_TO_REG.get(key)


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
) -> list[tuple[int, int | str]]:
    """Compare last HA-set register values against current device values.

    Tracked values are the raw ints passed to ``async_write``.  For comparison,
    work-mode ints are converted to their decoded string label (matching what
    ``registers.decode`` returns).  Other registers stay as ints.

    Returns a list of ``(register, raw_tracked_value)`` pairs that have drifted.
    Registers not in the control set are ignored.  Missing keys in *current*
    are silently skipped (can't compare what isn't reported).
    """
    drifted: list[tuple[int, int | str]] = []
    for reg, expected in tracked.items():
        key = register_to_key(reg)
        if key is None:
            continue
        if key not in current:
            continue
        # Work-mode: tracked is a raw int (0, 1, 2), current is a string label.
        compare_val = expected
        if reg == r.REG_WORK_MODE and isinstance(expected, int):
            compare_val = r.WORK_MODE_LABELS.get(
                expected, f"Unknown ({expected})"
            )
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
