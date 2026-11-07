"""Three-phase string-inverter profile, decoded from a live SUN-6K-G0x capture.

TELEMETRY/CONFIG are raw register words read over BLE through a DYDA_WiBLE_1.7.2
logger (serial number replaced) on 2026-11-07, cross-checked against same-minute
Deye Cloud readings (Solar Today 1.0 kWh, Solar Total 62.4 kWh, Rated Power
6000 W, L1/L2/L3 ~230.0/227.5/230.0 V, 49.96 Hz, 31.0 °C).
"""
from datetime import datetime

import pytest

from custom_components.deye_ble import registers as r
from custom_components.deye_ble.helpers import (
    async_detect_profile,
    async_poll,
    infer_grid_connected,
)

DEVICE_TYPE = 0x0002

TELEMETRY: dict[int, list[int]] = {
    59: [2, 10, 0, 221, 624, 0],
    70: [3983, 3940, 3983, 2300, 2275, 2300, 7, 10, 10, 4996, 3733, 0, 3993, 0, 6480, 0],
    86: [3914, 0, 65400, 65535, 1310, 1310],
    101: [0, 0, 0, 0, 0, 0, 0, 0, 3694, 11, 10, 0],
}

CONFIG: dict[int, list[int]] = {
    2: [528, 12594, 13108, 13622, 14136, 14640, 0, 4, 51747, 46141, 22050, 835, 787, 2, 60000, 0],
    18: [515, 1, 255, 120, 6667, 1803, 1055],
}


def _decode(config: bool = True):
    return r.decode_string({**TELEMETRY, **CONFIG} if config else TELEMETRY)


def test_device_type_selects_string():
    assert r.profile_from_device_type(0x0002) == r.PROFILE_STRING
    assert r.profile_from_device_type(0x0200) == r.PROFILE_STRING
    assert r.profile_from_device_type(0x0003) == r.PROFILE_SINGLE_PHASE
    assert r.profile_from_device_type(0x0005) == r.PROFILE_THREE_PHASE


@pytest.mark.parametrize("blocks", [r.STR_LIVE_BLOCKS + r.STR_FAST_BLOCKS + r.STR_SLOW_BLOCKS])
def test_blocks_fit_ble_reads_and_do_not_overlap(blocks):
    covered = set()
    for start, count in blocks:
        assert 1 <= count <= 16
        span = set(range(start, start + count))
        assert not covered & span
        covered |= span


@pytest.mark.parametrize("key,expected", [
    ("solar_power", 391.4),
    ("pv_input_power", 399.3),
    ("output_apparent_power", 648.0),
    ("output_reactive_power", -13.6),
    ("daily_solar", 1.0),
    ("total_solar", 62.4),
    ("pv1_voltage", 369.4),
    ("pv1_current", 1.1),
    ("pv1_power", 406.34),
    ("pv2_voltage", 1.0),
    ("pv2_current", 0.0),
    ("pv2_power", 0.0),
    ("grid_line_voltage_l1_l2", 398.3),
    ("grid_line_voltage_l2_l3", 394.0),
    ("grid_line_voltage_l3_l1", 398.3),
    ("grid_voltage_l1", 230.0),
    ("grid_voltage_l2", 227.5),
    ("grid_voltage_l3", 230.0),
    ("grid_current_l1", 0.7),
    ("grid_current_l2", 1.0),
    ("grid_current_l3", 1.0),
    ("grid_frequency", 49.96),
    ("inverter_temp", 31.0),
    ("inverter_ac_temp", 31.0),
    ("device_state", "Normal"),
    ("alarm", "OK"),
    ("fault", "OK"),
    ("alarm_code", 0),
    ("fault_code", 0),
    ("inverter_problem", False),
    ("inverter_serial", "1234567890"),
    ("firmware_control", "0343.5622.B43D"),
    ("firmware_comms", "0313.CA23"),
    ("protocol_version", 528),
    ("rated_power", 6000.0),
    ("mppt_count", 2),
    ("phase_count", 3),
    ("inverter_clock", datetime(2026, 11, 7, 11, 4, 31)),
])
def test_decode(key, expected):
    assert _decode()[key] == expected


def test_no_battery_or_bms_keys():
    data = _decode()
    forbidden = {
        "battery_soc", "battery_voltage", "battery_power", "battery_temp",
        "total_battery_charge", "total_battery_discharge",
        "bms_soc", "bms_voltage", "bms_charge_voltage", "tou_slots",
    }
    assert not (forbidden & data.keys())


def test_string_temperature_keys_match_register_meanings():
    words = dict(TELEMETRY)
    temperatures = list(words[86])
    temperatures[90 - 86] = 1310
    temperatures[91 - 86] = 1420
    words[86] = temperatures

    decoded = r.decode_string(words)

    assert decoded["inverter_temp"] == 31.0
    assert decoded["inverter_ac_temp"] == 42.0
    assert "inverter_dc_temp" not in decoded


def test_string_profile_has_no_writable_controls():
    profile = r.PROFILES[r.PROFILE_STRING]
    assert profile.controls == {}
    assert profile.time_sync_reg is None
    assert profile.clock_reg == r.STR_REG_CLOCK == 22
    assert profile.clock_writable is False
    assert r.PROFILES[r.PROFILE_THREE_PHASE].clock_writable is True
    assert r.PROFILES[r.PROFILE_SINGLE_PHASE].clock_writable is True


def test_alarm_and_fault_drive_inverter_problem():
    faulted = dict(TELEMETRY)
    block = list(faulted[101])
    block[0] = 1 << 2          # alarm bit 2: Grid phase failure
    block[3] = 1 << (17 - 16)  # fault word 1 bit 1 -> overall bit 17: AC over-current
    faulted[101] = block
    data = r.decode_string(faulted)
    assert data["alarm"] == "Grid phase failure"
    assert data["fault"] == "AC over-current"
    assert data["inverter_problem"] is True


def test_grid_connected_inferred_from_phase_voltages_and_frequency():
    assert infer_grid_connected(_decode()) is True

    freq_lost = dict(TELEMETRY)
    b70 = list(freq_lost[70])
    b70[79 - 70] = 0
    freq_lost[70] = b70
    assert infer_grid_connected(r.decode_string(freq_lost)) is False

    volts_lost = dict(TELEMETRY)
    b70 = list(volts_lost[70])
    b70[73 - 70 : 76 - 70] = [0, 0, 0]
    volts_lost[70] = b70
    assert infer_grid_connected(r.decode_string(volts_lost)) is False


class _Transport:
    def __init__(self):
        self.reads: list[tuple[int, int]] = []

    async def handshake(self):
        pass

    async def read(self, address, count):
        self.reads.append((address, count))
        if address == r.REG_DEVICE_TYPE:
            return [DEVICE_TYPE]
        return {**TELEMETRY, **CONFIG}[address][:count]


@pytest.mark.asyncio
async def test_detect_profile_reads_only_the_device_type():
    t = _Transport()
    assert await async_detect_profile(t) == r.PROFILE_STRING
    assert t.reads == [(r.REG_DEVICE_TYPE, 1)]


@pytest.mark.asyncio
async def test_full_poll_reads_every_tier_and_never_the_device_type():
    t = _Transport()
    data = await async_poll(t, profile=r.PROFILE_STRING)
    assert t.reads == r.STR_LIVE_BLOCKS + r.STR_FAST_BLOCKS + r.STR_SLOW_BLOCKS
    assert data["rated_power"] == 6000.0


@pytest.mark.asyncio
async def test_live_only_poll_reads_live_blocks():
    t = _Transport()
    data = await async_poll(t, profile=r.PROFILE_STRING, tiers={r.TIER_LIVE})
    assert t.reads == r.STR_LIVE_BLOCKS
    assert {
        "solar_power", "pv_input_power", "grid_voltage_l1", "grid_voltage_l2",
        "grid_voltage_l3", "grid_current_l1", "grid_frequency",
        "inverter_temp", "inverter_ac_temp",
    } <= data.keys()
    assert "pv1_power" not in data and "rated_power" not in data


@pytest.mark.asyncio
async def test_slow_read_failure_keeps_telemetry():
    class Flaky(_Transport):
        async def read(self, address, count):
            if address in (2, 18):
                raise TimeoutError
            return await super().read(address, count)

    data = await async_poll(Flaky(), profile=r.PROFILE_STRING)
    assert data["solar_power"] == 391.4 and "rated_power" not in data
