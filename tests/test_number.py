"""Number-entity tests — Discharge SOC writes the floor to all non-charge slots.

Requires homeassistant (number.py imports it). The coordinator is faked so the
test exercises the real entity setter logic without bleak or a live HA loop.
"""
from __future__ import annotations

import pytest

pytest.importorskip("homeassistant")

from custom_components.deye_ble.const import CONF_LOGGER_SN
from custom_components.deye_ble.number import DeyeDischargeSoc
from custom_components.deye_ble.registers import DISCHARGE_SOC_REGS


class FakeCoordinator:
    def __init__(self):
        self.data: dict = {}
        self.write_many_calls: list[dict[int, int]] = []
        self.updated: dict | None = None
        self.dirty = False

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
