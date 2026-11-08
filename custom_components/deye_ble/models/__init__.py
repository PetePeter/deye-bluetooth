"""Register maps and decoders grouped by inverter model."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .single_phase_lp1 import *
from .string_g05 import *
from .three_phase_hybrid import *
from .single_phase_lp1 import __all__ as _single_phase_exports
from .string_g05 import __all__ as _string_exports
from .three_phase_hybrid import __all__ as _three_phase_exports


REG_DEVICE_TYPE = 0x0000
DEVICE_TYPE_SINGLE_PHASE = 0x0003

PROFILE_THREE_PHASE = "three_phase"
PROFILE_SINGLE_PHASE = "single_phase"
PROFILE_STRING = "string"

# On-grid string inverters report 2, or byte-swapped 0x0200.
DEVICE_TYPES_STRING = frozenset((0x0002, 0x0200))


def profile_from_device_type(raw: int) -> str:
    """Map the register-0 device type to a decode profile."""
    if raw == DEVICE_TYPE_SINGLE_PHASE:
        return PROFILE_SINGLE_PHASE
    if raw in DEVICE_TYPES_STRING:
        return PROFILE_STRING
    return PROFILE_THREE_PHASE


TIER_LIVE = "live"
TIER_FAST = "fast"
TIER_SLOW = "slow"
ALL_TIERS = frozenset((TIER_LIVE, TIER_FAST, TIER_SLOW))

SP_IO_MODE_IDS = {value: key for key, value in SP_IO_MODES.items()}
SP_ENERGY_PATTERN_IDS = {value: key for key, value in SP_ENERGY_PATTERNS.items()}


@dataclass(frozen=True)
class RegisterProfile:
    """Polling and control metadata for a register profile."""

    live: list[tuple[int, int]]
    fast: list[tuple[int, int]]
    slow: list[tuple[int, int]]
    decode: Callable[[dict[int, list[int]]], dict]
    controls: dict[str, int]
    clock_reg: int
    clock_writable: bool
    time_sync_reg: int | None


PROFILES: dict[str, RegisterProfile] = {
    PROFILE_THREE_PHASE: RegisterProfile(
        live=LIVE_BLOCKS,
        fast=FAST_BLOCKS,
        slow=SLOW_BLOCKS,
        decode=decode,
        controls={
            "work_mode": REG_WORK_MODE,
            "max_sell_power": REG_MAX_SELL_POWER,
            "zero_export_power": REG_ZERO_EXPORT_POWER,
            "charge_soc": REG_CHARGE_SOC,
            "max_charge_current": REG_MAX_CHARGE_CURRENT,
            "max_discharge_current": REG_MAX_DISCHARGE_CURRENT,
            "batt_shutdown_soc": REG_BATT_SHUTDOWN_SOC,
            "batt_restart_soc": REG_BATT_RESTART_SOC,
            "batt_low_soc": REG_BATT_LOW_SOC,
            "grid_peak_power": REG_GRID_PEAK_POWER,
            "gen_peak_power": REG_GEN_PEAK_POWER,
        },
        clock_reg=REG_CLOCK,
        clock_writable=True,
        time_sync_reg=REG_TIME_SYNC,
    ),
    PROFILE_SINGLE_PHASE: RegisterProfile(
        live=SP_LIVE_BLOCKS,
        fast=SP_FAST_BLOCKS,
        slow=SP_SLOW_BLOCKS,
        decode=decode_single_phase,
        controls={
            "zero_export_power": 206,
            "max_charge_current": 210,
            "max_discharge_current": 211,
            "batt_shutdown_soc": 217,
            "batt_restart_soc": 218,
            "batt_low_soc": 219,
            "grid_charge_current": 230,
            "grid_charge_enabled": 232,
            "io_mode": 235,
            "smartload_off_soc": 237,
            "smartload_on_soc": 239,
            "smartload_min_solar_power": 241,
            "energy_pattern": 243,
            "work_mode": 244,
            "max_sell_power": 245,
            "solar_sell_enabled": 247,
            "gen_peak_power": 292,
            "grid_peak_power": 293,
        },
        clock_reg=SP_REG_CLOCK,
        clock_writable=True,
        time_sync_reg=None,
    ),
    PROFILE_STRING: RegisterProfile(
        live=STR_LIVE_BLOCKS,
        fast=STR_FAST_BLOCKS,
        slow=STR_SLOW_BLOCKS,
        decode=decode_string,
        controls={},
        clock_reg=STR_REG_CLOCK,
        clock_writable=False,
        time_sync_reg=None,
    ),
}


def tier_blocks(profile: RegisterProfile, tiers) -> list[tuple[tuple[int, int], bool]]:
    """Return ``(block, fatal)`` pairs in LIVE, FAST, SLOW order."""
    out = []
    for tier, blocks in (
        (TIER_LIVE, profile.live),
        (TIER_FAST, profile.fast),
        (TIER_SLOW, profile.slow),
    ):
        if tier in tiers:
            out += [(block, tier != TIER_SLOW) for block in blocks]
    return out


__all__ = list(dict.fromkeys([
    *_three_phase_exports,
    *_single_phase_exports,
    *_string_exports,
    "REG_DEVICE_TYPE", "DEVICE_TYPE_SINGLE_PHASE", "PROFILE_THREE_PHASE",
    "PROFILE_SINGLE_PHASE", "PROFILE_STRING", "DEVICE_TYPES_STRING",
    "profile_from_device_type", "TIER_LIVE", "TIER_FAST", "TIER_SLOW",
    "ALL_TIERS", "SP_IO_MODE_IDS", "SP_ENERGY_PATTERN_IDS", "RegisterProfile",
    "PROFILES", "tier_blocks",
]))
