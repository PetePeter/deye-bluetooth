"""Switch-entity tests — peak-shaving enables read-modify-write the 0x00B2 word.

Requires homeassistant (switch.py imports it). The coordinator is faked so the
test exercises the real entity setter logic without bleak or a live HA loop.

The confirmed baseline (live MITM 2026-07-09) is 0x2AAA (both off); grid enable
is bit 4 (-> 0x2ABA), gen enable is bit 2. The critical property under test is
that toggling one enable preserves every other bit in the packed register.
"""
from __future__ import annotations

import pytest

pytest.importorskip("homeassistant")

from homeassistant.exceptions import HomeAssistantError

from custom_components.deye_ble.const import CONF_LOGGER_SN
from custom_components.deye_ble.registers import REG_PEAK_SHAVING_FLAGS
from custom_components.deye_ble.switch import DeyeGenPeakShaving, DeyeGridPeakShaving


class FakeCoordinator:
    def __init__(self, data: dict | None = None):
        self.data: dict = data or {}
        self.write_calls: list[tuple[int, int]] = []
        self.updated: dict | None = None
        self.dirty = False

    async def async_write(self, reg: int, value: int) -> None:
        self.write_calls.append((reg, value))

    def async_set_updated_data(self, data: dict) -> None:
        self.updated = data
        self.data = data

    def mark_config_dirty(self) -> None:
        self.dirty = True


class FakeEntry:
    def __init__(self, sn: str):
        self.data = {CONF_LOGGER_SN: sn}


def _entity(cls, raw: int):
    coordinator = FakeCoordinator({"peak_shaving_flags_raw": raw})
    return cls(coordinator, FakeEntry("DEYE00000001"), "DEYE00000001"), coordinator


@pytest.mark.asyncio
async def test_grid_enable_sets_only_bit4_from_baseline():
    entity, coordinator = _entity(DeyeGridPeakShaving, 0x2AAA)

    await entity.async_turn_on()

    assert coordinator.write_calls == [(REG_PEAK_SHAVING_FLAGS, 0x2ABA)]
    assert coordinator.updated["grid_peak_shaving"] is True
    assert coordinator.updated["peak_shaving_flags_raw"] == 0x2ABA
    assert coordinator.dirty


@pytest.mark.asyncio
async def test_gen_enable_preserves_grid_bit():
    # Grid already on (0x2ABA); enabling gen must reach 0x2ABE, not clobber grid.
    entity, coordinator = _entity(DeyeGenPeakShaving, 0x2ABA)

    await entity.async_turn_on()

    assert coordinator.write_calls == [(REG_PEAK_SHAVING_FLAGS, 0x2ABE)]
    assert coordinator.updated["gen_peak_shaving"] is True
    assert coordinator.updated["grid_peak_shaving"] is True


@pytest.mark.asyncio
async def test_grid_disable_clears_only_bit4():
    # Both on (0x2ABE); disabling grid must leave gen set (0x2AAE).
    entity, coordinator = _entity(DeyeGridPeakShaving, 0x2ABE)

    await entity.async_turn_off()

    assert coordinator.write_calls == [(REG_PEAK_SHAVING_FLAGS, 0x2AAE)]
    assert coordinator.updated["grid_peak_shaving"] is False
    assert coordinator.updated["gen_peak_shaving"] is True


@pytest.mark.asyncio
async def test_is_on_reflects_coordinator_bool():
    entity, _ = _entity(DeyeGridPeakShaving, 0x2ABA)
    entity.coordinator.data["grid_peak_shaving"] = True
    assert entity.is_on is True
    entity.coordinator.data["grid_peak_shaving"] = False
    assert entity.is_on is False


@pytest.mark.asyncio
async def test_toggle_without_raw_word_refuses_to_write():
    # No 0x00B2 read yet -> a blind write could clear other function bits. Refuse.
    coordinator = FakeCoordinator({})  # no peak_shaving_flags_raw
    entity = DeyeGridPeakShaving(coordinator, FakeEntry("DEYE00000001"), "DEYE00000001")

    with pytest.raises(HomeAssistantError):
        await entity.async_turn_on()

    assert coordinator.write_calls == []
