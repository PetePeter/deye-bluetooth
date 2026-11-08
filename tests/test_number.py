"""Generic model-number entities use real point and write descriptors."""
from __future__ import annotations

import pytest

pytest.importorskip("homeassistant")

from custom_components.deye_ble.const import CONF_LOGGER_SN
from custom_components.deye_ble.models import get
from custom_components.deye_ble.number import DeyeModelNumber
from custom_components.deye_ble.registers import (
    DISCHARGE_SOC_REGS,
    PROFILE_SINGLE_PHASE,
    REG_BATT_LOW_SOC,
    REG_BATT_RESTART_SOC,
    REG_BATT_SHUTDOWN_SOC,
    REG_GEN_PEAK_POWER,
    REG_GRID_PEAK_POWER,
    REG_MAX_CHARGE_CURRENT,
    REG_MAX_DISCHARGE_CURRENT,
    REG_MAX_SELL_POWER,
    REG_ZERO_EXPORT_POWER,
)


class FakeCoordinator:
    def __init__(self, profile="three_phase"):
        self.model = get(profile)
        self.data = {}
        self.write_calls = []
        self.write_many_calls = []
        self.updated = None
        self.dirty = False

    async def async_write(self, reg, value):
        self.write_calls.append((reg, value))

    async def async_write_many(self, regs):
        self.write_many_calls.append(regs)

    def async_set_updated_data(self, data):
        self.updated = data
        self.data = data

    def mark_config_dirty(self):
        self.dirty = True


class FakeEntry:
    def __init__(self, sn):
        self.data = {CONF_LOGGER_SN: sn}


def _number(coordinator, key):
    point = next(point for point in coordinator.model.points if point.key == key)
    return DeyeModelNumber(coordinator, FakeEntry("DEYE00000001"), point)


@pytest.mark.asyncio
async def test_discharge_soc_writes_all_declared_non_charge_slots():
    coordinator = FakeCoordinator()
    entity = _number(coordinator, "discharge_soc")

    await entity.async_set_native_value(25)

    assert coordinator.write_many_calls == [{reg: 25 for reg in DISCHARGE_SOC_REGS}]
    assert coordinator.updated["discharge_soc"] == 25
    assert coordinator.dirty


@pytest.mark.parametrize(("profile", "key", "reg"), [
    ("three_phase", "max_sell_power", REG_MAX_SELL_POWER),
    ("three_phase", "max_charge_current", REG_MAX_CHARGE_CURRENT),
    ("three_phase", "max_discharge_current", REG_MAX_DISCHARGE_CURRENT),
    ("three_phase", "batt_shutdown_soc", REG_BATT_SHUTDOWN_SOC),
    ("three_phase", "batt_low_soc", REG_BATT_LOW_SOC),
    ("three_phase", "batt_restart_soc", REG_BATT_RESTART_SOC),
    ("three_phase", "grid_peak_power", REG_GRID_PEAK_POWER),
    ("three_phase", "gen_peak_power", REG_GEN_PEAK_POWER),
    (PROFILE_SINGLE_PHASE, "max_sell_power", 245),
    (PROFILE_SINGLE_PHASE, "zero_export_power", 206),
    (PROFILE_SINGLE_PHASE, "grid_peak_power", 293),
    (PROFILE_SINGLE_PHASE, "gen_peak_power", 292),
    (PROFILE_SINGLE_PHASE, "max_charge_current", 210),
    (PROFILE_SINGLE_PHASE, "max_discharge_current", 211),
    (PROFILE_SINGLE_PHASE, "batt_shutdown_soc", 217),
    (PROFILE_SINGLE_PHASE, "batt_restart_soc", 218),
    (PROFILE_SINGLE_PHASE, "batt_low_soc", 219),
    (PROFILE_SINGLE_PHASE, "grid_charge_current", 230),
    (PROFILE_SINGLE_PHASE, "smartload_off_soc", 237),
    (PROFILE_SINGLE_PHASE, "smartload_on_soc", 239),
    (PROFILE_SINGLE_PHASE, "smartload_min_solar_power", 241),
])
@pytest.mark.asyncio
async def test_model_number_writes_its_declared_register(profile, key, reg):
    coordinator = FakeCoordinator(profile)
    entity = _number(coordinator, key)

    await entity.async_set_native_value(50)

    assert coordinator.write_calls == [(reg, 50)]
    assert coordinator.updated[key] == 50
    assert coordinator.dirty


def test_zero_export_range_comes_from_the_model_point():
    three_phase = _number(FakeCoordinator(), "zero_export_power")
    single_phase = _number(FakeCoordinator(PROFILE_SINGLE_PHASE), "zero_export_power")

    assert (three_phase.native_min_value, three_phase.native_max_value) == (-15000, 100)
    assert (single_phase.native_min_value, single_phase.native_max_value) == (0, 100)
