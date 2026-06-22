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
