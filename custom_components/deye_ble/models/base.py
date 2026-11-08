"""Pure model descriptors shared by the Deye register maps and HA platforms."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from ..codec import WordBlocks, lookup, s16, s32, scaled, u32

TIER_LIVE = "live"
TIER_FAST = "fast"
TIER_SLOW = "slow"
TIERS = (TIER_LIVE, TIER_FAST, TIER_SLOW)
MAX_READ_REGISTERS = 16

Decoder = Callable[[WordBlocks], Any]
Computer = Callable[[Mapping[str, Any]], Any]
Encoder = Callable[[Any, Mapping[str, Any]], int | Mapping[int, int]]


@dataclass(frozen=True)
class Read:
    """Registers required for one value and its pure decoder."""

    registers: tuple[int, ...]
    decode: Decoder

    def __post_init__(self) -> None:
        if not self.registers:
            raise ValueError("a read descriptor needs at least one register")
        if any(register < 0 for register in self.registers):
            raise ValueError("register addresses must be non-negative")


@dataclass(frozen=True)
class Write:
    """Verified write operation for a point, including multi-register writes."""

    registers: tuple[int, ...]
    verified_on: str
    encode: Encoder
    reassertable: bool = True

    def __post_init__(self) -> None:
        if not self.registers:
            raise ValueError("a write descriptor needs at least one register")
        if not self.verified_on.strip():
            raise ValueError("verified_on must name a confirmed inverter model")

    def values(self, value: Any, current: Mapping[str, Any]) -> dict[int, int]:
        """Encode a value to raw register writes using the current decoded state."""
        encoded = self.encode(value, current)
        if isinstance(encoded, Mapping):
            result = {int(register): int(raw) for register, raw in encoded.items()}
        elif len(self.registers) == 1:
            result = {self.registers[0]: int(encoded)}
        else:
            raise ValueError("multi-register writes must encode to a register mapping")
        if set(result).difference(self.registers):
            raise ValueError("write encoder returned a register outside its descriptor")
        return result


@dataclass(frozen=True)
class Sensor:
    """Sensor presentation metadata with no Home Assistant dependency."""

    name: str
    unit: str | None = None
    device_class: str | None = None
    state_class: str | None = None
    icon: str | None = None
    precision: int | None = None
    entity_category: str | None = None


@dataclass(frozen=True)
class Number:
    """Number entity presentation metadata."""

    name: str
    unit: str | None
    minimum: float
    maximum: float
    step: float = 1
    icon: str | None = None
    device_class: str | None = None
    mode: str = "box"


@dataclass(frozen=True)
class Switch:
    """Switch entity presentation metadata."""

    name: str
    icon: str | None = None
    device_class: str | None = None
    entity_category: str | None = None


@dataclass(frozen=True)
class BinarySensor:
    """Binary-sensor presentation metadata with no HA dependency."""

    name: str
    device_class: str | None = None
    icon: str | None = None
    entity_category: str | None = None


@dataclass(frozen=True)
class Select:
    """Select entity presentation metadata."""

    name: str
    options: Mapping[int, str]
    icon: str | None = None


@dataclass(frozen=True)
class Time:
    """Time entity presentation metadata."""

    name: str
    icon: str | None = None


@dataclass(frozen=True)
class Capabilities:
    """Hardware features that gate special core entities and write paths."""

    has_battery: bool = False
    has_tou: bool = False
    clock_writable: bool = False
    cloud_time_sync: bool = False


@dataclass(frozen=True)
class Point:
    """One decoded value, optionally shown and/or writable in Home Assistant."""

    key: str
    read: Read | None = None
    write: Write | None = None
    show: Sensor | Number | Switch | BinarySensor | Select | Time | tuple[Sensor | Number | Switch | BinarySensor | Select | Time, ...] | None = None
    tier: str = TIER_SLOW
    compute: Computer | None = None

    def __post_init__(self) -> None:
        if not self.key:
            raise ValueError("point key must not be empty")
        if self.tier not in TIERS:
            raise ValueError(f"unknown read tier: {self.tier}")
        if self.read is None and self.compute is None:
            raise ValueError(f"point {self.key!r} needs a read or compute function")

    @property
    def shows(self) -> tuple[Sensor | Number | Switch | BinarySensor | Select | Time, ...]:
        if self.show is None:
            return ()
        return self.show if isinstance(self.show, tuple) else (self.show,)


@dataclass(frozen=True)
class Model:
    """Register map and user-facing point declarations for one inverter model."""

    id: str
    name: str
    device_types: tuple[int, ...]
    points: tuple[Point, ...]
    capabilities: Capabilities = field(default_factory=Capabilities)

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("model id must not be empty")
        if not self.name:
            raise ValueError("model name must not be empty")
        keys = [point.key for point in self.points]
        if len(keys) != len(set(keys)):
            raise ValueError(f"duplicate point key in model {self.id!r}")

    def decode(self, words_by_reg: WordBlocks) -> dict[str, Any]:
        """Decode available reads, then compute derived points in declaration order."""
        values: dict[str, Any] = {}
        for point in self.points:
            if point.read is None:
                continue
            value = point.read.decode(words_by_reg)
            if value is not None:
                values[point.key] = value
        for point in self.points:
            if point.compute is None:
                continue
            value = point.compute(values)
            if value is not None:
                values[point.key] = value
        return values

    def read_blocks(self, tiers: Sequence[str] | None = None) -> dict[str, list[tuple[int, int]]]:
        """Build contiguous, tiered BLE reads from the addresses used by points."""
        return derive_blocks(self.points, tiers=tiers)

    @property
    def write_points(self) -> tuple[Point, ...]:
        """Points that declare one or more supported write operations."""
        return tuple(point for point in self.points if point.write is not None)

    @property
    def reassert_points(self) -> dict[int, Point]:
        """Register-to-point map for writes that have a stable drift value."""
        registers: dict[int, Point] = {}
        for point in self.points:
            if point.write is None or not point.write.reassertable:
                continue
            for register in point.write.registers:
                previous = registers.get(register)
                if previous is not None and previous.key != point.key:
                    raise ValueError(
                        f"models {self.id!r} maps register 0x{register:04X} "
                        f"to both {previous.key!r} and {point.key!r}"
                    )
                registers[register] = point
        return registers

    @property
    def controls(self) -> dict[str, int]:
        """Compatibility view for callers needing the first register per control."""
        return {
            point.key: point.write.registers[0]
            for point in self.points
            if point.write is not None
        }


class ModelRegistry:
    """Validated model index with device detection and the three-phase fallback."""

    def __init__(
        self,
        models: Sequence[Model],
        *,
        fallback_id: str = "three_phase",
    ) -> None:
        self.models = tuple(models)
        self._by_id: dict[str, Model] = {}
        self._by_device_type: dict[int, Model] = {}
        for model in self.models:
            if model.id in self._by_id:
                raise ValueError(f"duplicate model id {model.id!r}")
            self._by_id[model.id] = model
            for device_type in model.device_types:
                if device_type in self._by_device_type:
                    other = self._by_device_type[device_type]
                    raise ValueError(
                        f"duplicate device type 0x{device_type:04X}: "
                        f"{other.id!r} and {model.id!r}"
                    )
                self._by_device_type[device_type] = model
        if fallback_id not in self._by_id:
            raise ValueError(f"fallback model {fallback_id!r} is not registered")
        self.fallback = self._by_id[fallback_id]

    def get(self, model_id: str) -> Model:
        return self._by_id[model_id]

    def for_device_type(self, raw: int) -> Model:
        return self._by_device_type.get(raw, self.fallback)

    def choices(self) -> list[tuple[str, str]]:
        return [(model.id, model.name) for model in self.models]


def derive_blocks(
    points: Sequence[Point],
    *,
    tiers: Sequence[str] | None = None,
) -> dict[str, list[tuple[int, int]]]:
    """Group adjacent point registers per tier into blocks of at most 16 words."""
    selected = set(TIERS if tiers is None else tiers)
    unknown = selected.difference(TIERS)
    if unknown:
        raise ValueError(f"unknown read tiers: {sorted(unknown)}")

    blocks: dict[str, list[tuple[int, int]]] = {tier: [] for tier in TIERS}
    for tier in TIERS:
        if tier not in selected:
            continue
        registers = sorted({
            register
            for point in points
            if point.tier == tier and point.read is not None
            for register in point.read.registers
        })
        if not registers:
            continue
        start = previous = registers[0]
        for register in registers[1:]:
            if register - start < MAX_READ_REGISTERS:
                previous = register
                continue
            blocks[tier].append((start, previous - start + 1))
            start = previous = register
        blocks[tier].append((start, previous - start + 1))
    return blocks


def scalar_read(
    register: int,
    *,
    factor: float = 1,
    signed: bool = False,
    offset: int = 0,
) -> Read:
    """Describe a scaled one-register value using the shared codec rules."""
    def decode(words: WordBlocks):
        raw = lookup(words, register)
        if raw is None:
            return None
        return scaled(s16(raw) if signed else raw, factor, offset=offset)

    return Read((register,), decode)


def pair_read(
    low_register: int,
    high_register: int,
    *,
    factor: float = 1,
    signed: bool = False,
) -> Read:
    """Describe low-word-first 32-bit values, including sparse word pairs."""
    def decode(words: WordBlocks):
        low, high = lookup(words, low_register), lookup(words, high_register)
        if low is None or high is None:
            return None
        raw = s32(low, high) if signed else u32(low, high)
        return round(raw * factor, 2)

    return Read((low_register, high_register), decode)


__all__ = [
    "TIER_LIVE", "TIER_FAST", "TIER_SLOW", "TIERS", "MAX_READ_REGISTERS",
    "Read", "Write", "Sensor", "Number", "Switch", "BinarySensor", "Select", "Time",
    "Capabilities", "Point", "Model", "ModelRegistry", "derive_blocks",
    "scalar_read", "pair_read",
]
