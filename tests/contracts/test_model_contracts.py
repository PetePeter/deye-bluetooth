"""Contracts exercised against every registered model and broken real models."""
from __future__ import annotations

import pytest

from custom_components.deye_ble import models
from custom_components.deye_ble.codec import lookup
from custom_components.deye_ble.models.base import (
    Capabilities,
    Model,
    ModelRegistry,
    Point,
    Read,
    Sensor,
    Write,
)
from tests.contracts.captures import MODEL_CAPTURES
from tests.contracts.key_glossary import meaning_for


_TIERS = (models.TIER_LIVE, models.TIER_FAST, models.TIER_SLOW)
_CLOCK_KEYS = {"inverter_clock", "time_sync"}


def _assert_valid_blocks(model: Model) -> None:
    """Every derived block is a legal BLE read and blocks do not overlap."""
    blocks = model.read_blocks()
    occupied: set[int] = set()
    for tier in _TIERS:
        for start, count in blocks[tier]:
            assert 1 <= count <= 16, f"{model.id} {tier} block {start:#06x} has {count} words"
            span = set(range(start, start + count))
            assert not occupied.intersection(span), f"{model.id} has overlapping read blocks at {start:#06x}"
            occupied.update(span)


def _assert_shown_keys_decode(model: Model, capture: dict[int, list[int]]) -> None:
    shown = {point.key for point in model.points if point.shows}
    decoded = set(model.decode(capture))
    missing = shown - decoded
    assert not missing, f"{model.id} capture does not decode shown keys: {sorted(missing)}"


def _assert_glossary_complete(model: Model) -> None:
    for point in model.points:
        meaning = meaning_for(model.id, point.key)
        assert meaning is not None and meaning.description.strip(), (
            f"{model.id}.{point.key} needs a semantic key glossary entry"
        )
        if meaning.registers is not None:
            assert point.read is not None and point.read.registers == meaning.registers, (
                f"{model.id}.{point.key} reads {point.read.registers if point.read else None}; "
                f"glossary says {meaning.registers}: {meaning.description}"
            )


def _assert_writes_verified(model: Model) -> None:
    for point in model.points:
        if point.write is not None:
            assert point.write.verified_on.strip(), f"{model.id}.{point.key} write needs verified_on"


def _assert_clock_writes_supported(model: Model) -> None:
    for point in model.points:
        if point.key in _CLOCK_KEYS and point.write is not None:
            assert model.capabilities.clock_writable, (
                f"{model.id}.{point.key} writes the clock without clock_writable"
            )


class _BrokenBlocksModel(Model):
    def read_blocks(self, tiers=None):
        if self.id == "oversized":
            return {"live": [(10, 17)], "fast": [], "slow": []}
        return {"live": [(10, 2), (11, 2)], "fast": [], "slow": []}


def _fake_model(
    model_id: str,
    *,
    points: tuple[Point, ...] = (),
    device_types: tuple[int, ...] = (),
    capabilities: Capabilities | None = None,
) -> Model:
    return Model(
        id=model_id,
        name=model_id,
        device_types=device_types,
        points=points,
        capabilities=capabilities or Capabilities(),
    )


@pytest.mark.parametrize("model", models.MODELS, ids=lambda model: model.id)
def test_each_registered_model_satisfies_its_contract(model: Model):
    _assert_valid_blocks(model)
    _assert_shown_keys_decode(model, MODEL_CAPTURES[model.id])
    _assert_glossary_complete(model)
    _assert_writes_verified(model)
    _assert_clock_writes_supported(model)


def test_registered_device_type_claims_are_unique():
    ModelRegistry(models.MODELS, fallback_id=models.PROFILE_THREE_PHASE)


def test_duplicate_device_type_is_rejected_for_broken_models():
    first = _fake_model("first", device_types=(7,))
    second = _fake_model("second", device_types=(7,))

    with pytest.raises(ValueError, match="duplicate device type"):
        ModelRegistry((first, second), fallback_id="first")


@pytest.mark.parametrize("model_id", ["oversized", "overlap"])
def test_invalid_blocks_are_rejected_for_broken_models(model_id: str):
    broken = _BrokenBlocksModel(id=model_id, name=model_id, device_types=(), points=())

    with pytest.raises(AssertionError):
        _assert_valid_blocks(broken)


def test_missing_shown_decode_is_rejected_for_broken_model():
    point = Point(
        "missing_value",
        read=Read((99,), lambda words: lookup(words, 99)),
        show=Sensor("Missing Value"),
    )
    broken = _fake_model("missing_capture", points=(point,))

    with pytest.raises(AssertionError, match="missing_value"):
        _assert_shown_keys_decode(broken, {})


def test_missing_glossary_meaning_is_rejected_for_broken_model():
    point = Point(
        "unlisted_value",
        read=Read((99,), lambda words: lookup(words, 99)),
        show=Sensor("Unlisted Value"),
    )
    broken = _fake_model("unlisted_model", points=(point,))

    with pytest.raises(AssertionError, match="semantic key glossary"):
        _assert_glossary_complete(broken)


def test_register_meaning_rejects_temperature_drift_for_broken_model():
    point = Point(
        "inverter_temp",
        read=Read((91,), lambda words: lookup(words, 91)),
        show=Sensor("Inverter Temperature"),
    )
    broken = _fake_model("string", points=(point,))

    with pytest.raises(AssertionError, match=r"glossary says \(90,\)"):
        _assert_glossary_complete(broken)


def test_unverified_write_is_rejected_for_broken_model():
    write = Write((1,), "SUN-TEST", lambda value, _data: int(value))
    object.__setattr__(write, "verified_on", "")
    point = Point("control", read=Read((1,), lambda words: lookup(words, 1)), write=write)
    broken = _fake_model("unverified", points=(point,))

    with pytest.raises(AssertionError, match="needs verified_on"):
        _assert_writes_verified(broken)


def test_clock_write_requires_capability_for_broken_model():
    point = Point(
        "inverter_clock",
        read=Read((22,), lambda words: lookup(words, 22)),
        write=Write((22,), "SUN-TEST", lambda value, _data: int(value)),
    )
    broken = _fake_model("clock_read_only", points=(point,))

    with pytest.raises(AssertionError, match="clock_writable"):
        _assert_clock_writes_supported(broken)
