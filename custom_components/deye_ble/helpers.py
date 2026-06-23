"""Pure helpers — no HA imports, so they stay unit-testable without HA.

Holds the BLE poll orchestration (async_poll), logger-SN validation, and the
daily-baseline calculation. The HA-coupled DeyeBleCoordinator (coordinator.py)
imports async_poll from here.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any, Protocol

from . import registers as r

_LOGGER = logging.getLogger(__name__)


# --- Transport protocol (duck-typed, not bleak-dependent) -------------------

class Transport(Protocol):
    async def handshake(self) -> None: ...
    async def read(self, address: int, count: int) -> list[int]: ...
    async def write(self, address: int, value: int) -> None: ...


# --- Pure async poll (unit-testable with a fake transport) ------------------

async def async_poll(transport: Transport, *, with_config: bool = False) -> dict[str, Any]:
    """Connect, handshake, read all register blocks, decode and return.

    Telemetry-block read failures propagate (the poll has failed). Control-block
    reads (only when *with_config*) are non-fatal: a failure is logged and the
    affected config keys are simply omitted, so a config hiccup never discards a
    good telemetry cycle. The caller carries forward the previous config values.
    """
    await transport.handshake()

    words_by_reg: dict[int, list[int]] = {}
    for start, count in r.READ_BLOCKS:
        words_by_reg[start] = await transport.read(start, count)

    if with_config:
        for start, count in r.CONTROL_BLOCKS:
            try:
                words_by_reg[start] = await transport.read(start, count)
            except Exception as e:  # noqa: BLE001 — config read is best-effort
                _LOGGER.debug("config block 0x%04X read failed (non-fatal): %s", start, e)

    return r.decode(words_by_reg)


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

    Returns ``None`` on match.  Raises :class:`ReadbackError` on mismatch.
    """
    if expected != actual:
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
