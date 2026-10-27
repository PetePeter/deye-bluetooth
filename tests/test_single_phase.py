"""Regression fixtures from the user's actual BLE capture, 2026-10-17."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from custom_components.deye_ble import platforms_for_profile
from custom_components.deye_ble import protocol as p
from custom_components.deye_ble import registers as legacy
from custom_components.deye_ble import single_phase as s
from custom_components.deye_ble.helpers import async_poll


@pytest.fixture
def captured():
    frames = json.loads((Path(__file__).parent / "fixtures" / "sun6k_protocol21.json").read_text())
    return {int(address): p.parse_read(frame) for address, frame in frames.items()}


def test_captured_single_phase_values(captured):
    data = s.decode(captured)
    assert data["battery_voltage"] == 58.02
    assert data["battery_soc"] == 100
    assert data["battery_temp"] == 25.0
    assert data["battery_power"] == -60
    assert data["solar_power"] == 535
    assert data["house_load"] == 435
    assert data["grid_power"] == 36
    assert data["inverter_power_l1"] == 399
    assert data["grid_voltage_l1"] == 251.5
    assert data["grid_frequency"] == 50.03
    assert data["daily_solar"] == 7.1
    assert data["daily_grid_import"] == 7.0
    assert data["_register_profile"] == "single_phase"
    assert "inverter_power_l2" not in data
    assert "inverter_power_l3" not in data
    assert "bms_charge_voltage" not in data
    assert "work_mode" not in data
    assert "time_sync" not in data


def test_capture_discriminates_from_original_map(captured):
    # With only the handshake fix the original mapping reports false zeros.
    old, new = legacy.decode(captured), s.decode(captured)
    assert old["battery_soc"] == 0
    assert old["battery_voltage"] == 0
    assert new["battery_soc"] != old["battery_soc"]
    assert new["battery_voltage"] != old["battery_voltage"]


def test_32_bit_totals_preserve_high_word_and_noncontiguous_import():
    data = s.decode({78: [0x1234, 5000, 2], 96: [0xFFFF, 1], 85: [10, 3]})
    assert data["total_grid_import"] == 13573.2
    assert data["total_solar"] == 13107.1
    assert data["total_consumption"] == 19661.8


def test_missing_word_omits_total_and_missing_pv_omits_solar():
    data = s.decode({96: [0xFFFF], 186: [123]})
    assert "total_solar" not in data
    assert "solar_power" not in data
    assert "battery_soc" not in data


def test_clock_is_read_from_22_not_legacy_62():
    data = s.decode({22: [0x1A0A, 0x110D, 0x1C39]})
    assert data["inverter_clock"] == datetime(2026, 10, 17, 13, 28, 57)
    assert "inverter_clock" not in s.decode({62: [0x1A0A, 0x110D, 0x1C39]})


def test_poll_plan_covers_all_fields_and_limits_ble_frame_size():
    addresses = {start + i for start, count in s.READ_BLOCKS for i in range(count)}
    assert all(1 <= count <= 16 for _, count in s.READ_BLOCKS)
    assert {item[0] for item in s.SCALARS.values()} <= addresses
    assert {address for pair in s.TOTALS.values() for address in pair} <= addresses
    assert {22, 23, 24, 186, 187} <= addresses
    assert max(addresses) < 0x0200
    # Read both words together so counter rollover cannot split a 32-bit value.
    for low, high in s.TOTALS.values():
        assert any(start <= min(low, high) and max(low, high) < start + count
                   for start, count in s.READ_BLOCKS)


def test_monitoring_platforms_exclude_legacy_controls():
    assert platforms_for_profile("single_phase") == ["sensor", "binary_sensor"]
    assert "number" in platforms_for_profile("legacy")
    assert "switch" in platforms_for_profile("legacy")


@pytest.mark.asyncio
async def test_single_phase_poll_never_reads_legacy_control_blocks(captured):
    words = {start + i: word for start, values in captured.items()
             for i, word in enumerate(values)}
    reads = []

    class Transport:
        async def handshake(self):
            pass

        async def get_register_profile(self):
            return "single_phase"

        async def read(self, start, count):
            reads.append((start, count))
            return [words.get(start + i, 0) for i in range(count)]

        async def write(self, *args):
            pytest.fail("a monitoring poll attempted a control write")

    data = await async_poll(Transport(), with_config=True)
    assert reads == s.READ_BLOCKS
    assert data["battery_voltage"] == 58.02
    assert data["solar_power"] == 535
    assert "max_sell_power" not in data


@pytest.mark.asyncio
async def test_single_phase_short_read_fails_instead_of_filling_zeros():
    class Transport:
        async def handshake(self):
            pass

        async def get_register_profile(self):
            return "single_phase"

        async def read(self, start, count):
            return [0] * (count - 1)

    with pytest.raises(ValueError, match="short read"):
        await async_poll(Transport())


def test_second_live_capture_decodes_complete_sensor_and_energy_set():
    frames = json.loads((Path(__file__).parent / "fixtures" / "sun6k_protocol21_single_phase.json").read_text())
    blocks = {int(address): p.parse_read(frame) for address, frame in frames.items()}
    data = s.decode(blocks)
    assert data["battery_voltage"] == 57.81
    assert data["battery_soc"] == 100
    assert data["battery_power"] == -60
    assert data["solar_power"] == 597
    assert data["house_load"] == 443
    assert data["grid_power"] == 0
    assert data["inverter_temp"] == 50.8
    assert data["grid_frequency"] == 49.98
    assert data["daily_consumption"] == 13.2
    assert data["total_battery_charge"] == 614.3
    assert data["total_battery_discharge"] == 381.8
    assert data["total_grid_import"] == 3903.6
    assert data["total_grid_export"] == 2.0
    assert data["total_consumption"] == 5119.2
    assert data["total_solar"] == 1621.6
    assert data["inverter_clock"] == datetime(2026, 10, 17, 13, 42, 13)
