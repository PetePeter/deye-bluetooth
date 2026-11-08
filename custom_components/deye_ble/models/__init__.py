"""Register maps and decoders grouped by inverter model."""
from __future__ import annotations

from importlib import import_module
from pkgutil import iter_modules
from dataclasses import dataclass
from typing import Callable

from .base import Model, ModelRegistry
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
    return for_device_type(raw).id


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


def _discover_models() -> tuple[Model, ...]:
    discovered = []
    for module_info in iter_modules(__path__):
        if module_info.name == "base" or module_info.ispkg:
            continue
        module = import_module(f"{__name__}.{module_info.name}")
        model = getattr(module, "MODEL", None)
        if model is not None:
            discovered.append(model)
    # Preserve the existing Options order, with the fallback model first.
    return tuple(sorted(discovered, key=lambda model: (model.id != PROFILE_THREE_PHASE, model.id)))


MODELS = _discover_models()
REGISTRY = ModelRegistry(MODELS, fallback_id=PROFILE_THREE_PHASE)


def _profile_for_model(model: Model) -> RegisterProfile:
    blocks = model.read_blocks()
    points = {point.key: point for point in model.points}
    clock = points.get("inverter_clock")
    if clock is None or clock.read is None:
        raise ValueError(f"model {model.id!r} must declare its inverter clock read")
    sync = points.get("time_sync")
    return RegisterProfile(
        live=blocks[TIER_LIVE],
        fast=blocks[TIER_FAST],
        slow=blocks[TIER_SLOW],
        decode=model.decode,
        controls=model.controls,
        clock_reg=clock.read.registers[0],
        clock_writable=model.capabilities.clock_writable,
        time_sync_reg=(sync.read.registers[0] if sync is not None and sync.read else None),
    )


PROFILES: dict[str, RegisterProfile] = {
    model.id: _profile_for_model(model) for model in MODELS
}


def get(model_id: str) -> Model:
    """Return a registered model by its persisted config-entry id."""
    return REGISTRY.get(model_id)


def for_device_type(raw: int) -> Model:
    """Find a model from register 0, defaulting to the three-phase model."""
    return REGISTRY.for_device_type(raw)


def choices() -> list[tuple[str, str]]:
    """Dropdown choices as ``(persisted_model_id, display_name)`` pairs."""
    return REGISTRY.choices()


__all__ += ["Model", "ModelRegistry", "MODELS", "REGISTRY", "get", "for_device_type", "choices"]
