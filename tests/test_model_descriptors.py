"""Contracts for pure model descriptors and the dynamic registry."""
from __future__ import annotations

import pytest

from custom_components.deye_ble.codec import lookup
from custom_components.deye_ble.models.base import (
    Capabilities,
    Model,
    ModelRegistry,
    Number,
    Point,
    Read,
    Sensor,
    Write,
    derive_blocks,
)
from custom_components.deye_ble import models


def _word_read(register: int) -> Read:
    return Read((register,), lambda words: lookup(words, register))


def _model(
    model_id: str,
    device_types: tuple[int, ...],
    points: tuple[Point, ...] = (),
) -> Model:
    return Model(
        id=model_id,
        name=model_id.replace("_", " ").title(),
        device_types=device_types,
        points=points,
        capabilities=Capabilities(),
    )


def test_read_blocks_pack_nearby_registers_by_tier_with_sixteen_register_limit():
    points = tuple(
        Point(f"live_{register}", _word_read(register), tier="live")
        for register in range(0x10, 0x22)
    ) + (
        Point("slow_same_address", _word_read(0x10), tier="slow"),
        Point("live_after_gap", _word_read(0x30), tier="live"),
        Point("live_gapped_but_packable", _word_read(0x40), tier="live"),
        Point("live_at_block_end", _word_read(0x4F), tier="live"),
        Point("live_after_block_limit", _word_read(0x50), tier="live"),
    )

    assert derive_blocks(points) == {
        "live": [(0x10, 16), (0x20, 2), (0x30, 1), (0x40, 16), (0x50, 1)],
        "fast": [],
        "slow": [(0x10, 1)],
    }


def test_model_decodes_read_points_then_computed_points_and_keeps_show_data():
    points = (
        Point("grid_power", _word_read(0x20), tier="live", show=Sensor("Grid Power", "W")),
        Point(
            "house_load",
            _word_read(0x21),
            tier="live",
            show=Number("House Load", "W", 0, 1000, 10),
            write=Write((0x21,), "SUN-TEST", lambda value, _data: int(value)),
        ),
        Point(
            "combined_power",
            compute=lambda data: data["grid_power"] + data["house_load"],
        ),
    )
    model = _model("example", (7,), points)

    assert model.decode({0x20: [10, 4]}) == {
        "grid_power": 10,
        "house_load": 4,
        "combined_power": 14,
    }
    assert model.points[0].show == Sensor("Grid Power", "W")
    assert model.reassert_points[0x21].key == "house_load"


def test_registry_rejects_duplicate_device_types():
    with pytest.raises(ValueError, match="device type 0x0007"):
        ModelRegistry([_model("first", (7,)), _model("second", (7,))])


def test_registry_preserves_choices_and_falls_back_to_three_phase():
    three_phase = _model("three_phase", (5,))
    single_phase = _model("single_phase", (3,))
    registry = ModelRegistry([three_phase, single_phase])

    assert registry.choices() == [("three_phase", "Three Phase"), ("single_phase", "Single Phase")]
    assert registry.get("single_phase") is single_phase
    assert registry.for_device_type(0x9999) is three_phase


def test_default_registry_discovers_models_and_keeps_persisted_ids():
    assert [model.id for model in models.MODELS] == [
        "three_phase", "single_phase", "string",
    ]
    assert models.for_device_type(0x0002).id == "string"
    assert models.for_device_type(0x0003).id == "single_phase"
    assert models.for_device_type(0x9999).id == "three_phase"
    assert models.choices() == [
        ("three_phase", "Three-phase"),
        ("single_phase", "Single-phase"),
        ("string", "String (on-grid)"),
    ]
