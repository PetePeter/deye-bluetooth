"""Model switch entities preserve unrelated bits when changing a shared word."""
from __future__ import annotations

import pytest

pytest.importorskip("homeassistant")

from homeassistant.exceptions import HomeAssistantError

from custom_components.deye_ble.const import CONF_LOGGER_SN
from custom_components.deye_ble.models import get
from custom_components.deye_ble.switch import DeyeModelSwitch
from custom_components.deye_ble.registers import REG_PEAK_SHAVING_FLAGS


class FakeCoordinator:
    def __init__(self, data=None):
        self.model = get("three_phase")
        self.data = data or {}
        self.write_calls = []
        self.updated = None
        self.dirty = False

    async def async_write(self, reg, value):
        self.write_calls.append((reg, value))

    async def async_write_many(self, values):
        self.write_calls.extend(values.items())

    def async_set_updated_data(self, data):
        self.updated = data
        self.data = data

    def mark_config_dirty(self):
        self.dirty = True


class FakeEntry:
    def __init__(self, sn):
        self.data = {CONF_LOGGER_SN: sn}


def _entity(key, raw):
    coordinator = FakeCoordinator({"peak_shaving_flags_raw": raw})
    point = next(point for point in coordinator.model.points if point.key == key)
    entity = DeyeModelSwitch(coordinator, FakeEntry("DEYE00000001"), point)
    return entity, coordinator


@pytest.mark.asyncio
async def test_grid_enable_sets_only_bit4_from_baseline():
    entity, coordinator = _entity("grid_peak_shaving", 0x2AAA)

    await entity.async_turn_on()

    assert coordinator.write_calls == [(REG_PEAK_SHAVING_FLAGS, 0x2ABA)]
    assert coordinator.updated["grid_peak_shaving"] is True
    assert coordinator.updated["peak_shaving_flags_raw"] == 0x2ABA
    assert coordinator.dirty


@pytest.mark.asyncio
async def test_gen_enable_preserves_grid_bit():
    entity, coordinator = _entity("gen_peak_shaving", 0x2ABA)

    await entity.async_turn_on()

    assert coordinator.write_calls == [(REG_PEAK_SHAVING_FLAGS, 0x2ABE)]
    assert coordinator.updated["gen_peak_shaving"] is True
    assert coordinator.updated["grid_peak_shaving"] is True


@pytest.mark.asyncio
async def test_grid_disable_clears_only_bit4():
    entity, coordinator = _entity("grid_peak_shaving", 0x2ABE)

    await entity.async_turn_off()

    assert coordinator.write_calls == [(REG_PEAK_SHAVING_FLAGS, 0x2AAE)]
    assert coordinator.updated["grid_peak_shaving"] is False
    assert coordinator.updated["gen_peak_shaving"] is True


def test_is_on_reflects_coordinator_bool():
    entity, _ = _entity("grid_peak_shaving", 0x2ABA)
    entity.coordinator.data["grid_peak_shaving"] = True
    assert entity.is_on is True
    entity.coordinator.data["grid_peak_shaving"] = False
    assert entity.is_on is False


@pytest.mark.asyncio
async def test_toggle_without_source_word_refuses_to_write():
    coordinator = FakeCoordinator()
    point = next(point for point in coordinator.model.points if point.key == "grid_peak_shaving")
    entity = DeyeModelSwitch(coordinator, FakeEntry("DEYE00000001"), point)

    with pytest.raises(HomeAssistantError):
        await entity.async_turn_on()

    assert coordinator.write_calls == []
