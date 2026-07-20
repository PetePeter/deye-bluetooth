"""Number-entity tests — Discharge SOC writes the floor to all non-charge slots.

Requires homeassistant (number.py imports it). The coordinator is faked so the
test exercises the real entity setter logic without bleak or a live HA loop.
"""
from __future__ import annotations

import pytest

pytest.importorskip("homeassistant")

from custom_components.deye_ble.const import CONF_LOGGER_SN
from custom_components.deye_ble.number import (
    DeyeBattLowSoc,
    DeyeBattRestartSoc,
    DeyeBattShutdownSoc,
    DeyeDischargeSoc,
    DeyeGenPeakPower,
    DeyeGridPeakPower,
    DeyeMaxChargeCurrent,
    DeyeMaxDischargeCurrent,
    DeyeZeroExportPower,
)
from custom_components.deye_ble.registers import (
    DISCHARGE_SOC_REGS,
    REG_BATT_LOW_SOC,
    REG_BATT_RESTART_SOC,
    REG_BATT_SHUTDOWN_SOC,
    REG_GEN_PEAK_POWER,
    REG_GRID_PEAK_POWER,
    REG_MAX_CHARGE_CURRENT,
    REG_MAX_DISCHARGE_CURRENT,
    REG_ZERO_EXPORT_POWER,
)


class FakeCoordinator:
    def __init__(self):
        self.data: dict = {}
        self.write_calls: list[tuple[int, int]] = []
        self.write_many_calls: list[dict[int, int]] = []
        self.updated: dict | None = None
        self.dirty = False

    async def async_write(self, reg: int, value: int) -> None:
        self.write_calls.append((reg, value))

    async def async_write_many(self, regs: dict[int, int]) -> None:
        self.write_many_calls.append(regs)

    def async_set_updated_data(self, data: dict) -> None:
        self.updated = data

    def mark_config_dirty(self) -> None:
        self.dirty = True


class FakeEntry:
    def __init__(self, sn: str):
        self.data = {CONF_LOGGER_SN: sn}


@pytest.mark.asyncio
async def test_discharge_soc_writes_value_to_all_non_charge_slots():
    coordinator = FakeCoordinator()
    entity = DeyeDischargeSoc(coordinator, FakeEntry("DEYE00000001"), "DEYE00000001")

    await entity.async_set_native_value(25)

    assert coordinator.write_many_calls == [{reg: 25 for reg in DISCHARGE_SOC_REGS}]
    assert coordinator.updated["discharge_soc"] == 25
    assert coordinator.dirty


@pytest.mark.asyncio
async def test_max_charge_current_writes_to_register_0x006c():
    coordinator = FakeCoordinator()
    entity = DeyeMaxChargeCurrent(coordinator, FakeEntry("DEYE00000001"), "DEYE00000001")

    await entity.async_set_native_value(210)

    assert coordinator.write_calls == [(REG_MAX_CHARGE_CURRENT, 210)]
    assert coordinator.updated["max_charge_current"] == 210
    assert coordinator.dirty


@pytest.mark.asyncio
async def test_max_discharge_current_writes_to_register_0x006d():
    coordinator = FakeCoordinator()
    entity = DeyeMaxDischargeCurrent(coordinator, FakeEntry("DEYE00000001"), "DEYE00000001")

    await entity.async_set_native_value(200)

    assert coordinator.write_calls == [(REG_MAX_DISCHARGE_CURRENT, 200)]
    assert coordinator.updated["max_discharge_current"] == 200
    assert coordinator.dirty


@pytest.mark.parametrize("entity_cls,reg,data_key,value", [
    (DeyeBattShutdownSoc, REG_BATT_SHUTDOWN_SOC, "batt_shutdown_soc", 4),
    (DeyeBattLowSoc, REG_BATT_LOW_SOC, "batt_low_soc", 5),
    (DeyeBattRestartSoc, REG_BATT_RESTART_SOC, "batt_restart_soc", 6),
    # Peak-shave caps stay editable and write through on every change.
    (DeyeGridPeakPower, REG_GRID_PEAK_POWER, "grid_peak_power", 16500),
    (DeyeGenPeakPower, REG_GEN_PEAK_POWER, "gen_peak_power", 8000),
    # Zero-export power is signed — a negative must pass through unchanged.
    (DeyeZeroExportPower, REG_ZERO_EXPORT_POWER, "zero_export_power", -30),
])
@pytest.mark.asyncio
async def test_single_register_number_writes_to_expected_register(
    entity_cls, reg, data_key, value,
):
    coordinator = FakeCoordinator()
    entity = entity_cls(coordinator, FakeEntry("DEYE00000001"), "DEYE00000001")

    await entity.async_set_native_value(value)

    assert coordinator.write_calls == [(reg, value)]
    assert coordinator.updated[data_key] == value
    assert coordinator.dirty


def test_zero_export_power_allows_negative_range():
    # The phone app blocks negatives; the entity must expose the signed range
    # (live-probed: -15000..100) so a grid-import/feed-in bias can be set from HA.
    entity = DeyeZeroExportPower(FakeCoordinator(), FakeEntry("DEYE00000001"), "DEYE00000001")
    assert entity._attr_native_min_value == -15000
    assert entity._attr_native_max_value == 100
