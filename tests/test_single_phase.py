"""Single-phase profile, decoded from a live SUN-8K-SG05LP1-EU-SM2 capture.

TELEMETRY/CONFIG are raw register words read over BLE through a DL1000B logger
(serial number replaced). Expected values are the same-minute Deye Cloud readings
where the cloud reports them; live AC values drift between reads, so only exact
matches are asserted.
"""
from datetime import datetime

import pytest

from custom_components.deye_ble import registers as r
from custom_components.deye_ble import protocol as p
from custom_components.deye_ble.helpers import (
    async_detect_profile,
    async_poll,
    infer_grid_connected,
)

DEVICE_TYPE = 0x0003

TELEMETRY: dict[int, list[int]] = {
    59: [2, 30, 250, 0, 27, 0, 940, 2947, 220, 940, 0, 0, 35, 1950, 0, 1553],
    75: [0, 6, 0, 3878, 5000, 0, 0, 0, 0, 37, 3914, 0, 3367, 0, 1000, 1332],
    91: [1290, 1, 0, 2000, 0, 999, 0],
    101: [0, 0, 0, 0, 0, 0, 314, 0, 2278, 4, 59, 0],
    150: [2411, 0, 2411, 0, 2410, 0, 2410, 2442, 0, 0, 158, 0, 213, 0, 210, 0],
    166: [0, 19, 0, 19, 0, 0, 0, 466, 0, 466, 485, 0, 485, 485, 0, 932],
    182: [1166, 5223, 52, 485, 101, 0, 0, 0, 389, 745, 5000, 5000, 1, 20752, 5062],
    312: [5520, 0, 160, 170, 52, 5229, 65529, 1166, 0, 0, 0, 0, 0],
}

CONFIG: dict[int, list[int]] = {
    2: [513, 12594, 13108, 13622, 14136, 14640, 16, 33026, 0, 5398, 0, 21384, 50050, 0, 14464, 1],
    18: [513, 0, 255, 0, 6666, 7944, 2858],
    200: [1, 5760, 5760, 5520, 314, 4500, 20, 90, 0, 0, 160, 170, 0, 1, 0, 8],
    216: [990, 15, 35, 25, 4600, 5200, 4750, 240, 1440, 4900, 90, 25, 4900, 30, 35, 1],
    232: [1, 0, 0, 0, 5100, 95, 5400, 100, 0, 0, 2, 1, 1, 500, 0, 0],
    248: [255, 0, 100, 500, 900, 1300, 1700, 2000, 8000, 8000, 8000, 8000, 8000, 8000, 4900, 4900],
    264: [4900, 4900, 4900, 4900, 50, 50, 95, 95, 100, 75, 6, 6, 6, 6, 7, 6],
    280: [273, 0, 60, 0, 0, 0, 0, 2650, 1950, 5150, 4800, 0, 2300, 5000],
    325: [0],
}


def _decode(config: bool = True):
    return r.decode_single_phase({**TELEMETRY, **CONFIG} if config else TELEMETRY)


def test_device_type_selects_single_phase():
    assert r.profile_from_device_type(DEVICE_TYPE) == r.PROFILE_SINGLE_PHASE
    assert r.profile_from_device_type(0x0005) == r.PROFILE_THREE_PHASE


@pytest.mark.parametrize("blocks", [r.SP_LIVE_BLOCKS + r.SP_FAST_BLOCKS + r.SP_SLOW_BLOCKS])
def test_blocks_fit_ble_reads_and_do_not_overlap(blocks):
    covered = set()
    for start, count in blocks:
        assert 1 <= count <= 16  # larger reads get no BLE reply
        span = set(range(start, start + count))
        assert not covered & span
        covered |= span


@pytest.mark.parametrize("key,expected", [
    # matched against Deye Cloud, same minute
    ("battery_soc", 52),
    ("battery_voltage", 52.23),
    ("battery_current", 7.45),
    ("battery_power", 389),
    ("battery_temp", 16.6),
    ("inverter_temp", 33.2),
    ("inverter_ac_temp", 29.0),
    ("pv1_voltage", 227.8),
    ("pv2_voltage", 5.9),
    ("grid_frequency", 50.0),
    ("grid_power", 19),
    ("gen_frequency", 50.62),
    ("total_solar", 99.9),
    ("total_grid_import", 387.8),
    ("total_grid_export", 0.0),
    ("total_consumption", 391.4),
    ("total_battery_charge", 195.0),
    ("total_battery_discharge", 155.3),
    ("daily_grid_import", 0.6),
    ("daily_battery_discharge", 3.5),
    ("bms_charge_voltage", 55.2),
    ("bms_charge_current_limit", 160),
    ("bms_discharge_current_limit", 170),
    ("bms_soc", 52),
    ("bms_current", -7),
    ("bms_temp", 16.6),
    ("max_sell_power", 500),
    ("max_charge_current", 160),
    ("max_discharge_current", 170),
    ("rated_power", 800.0 * 10),
    # inverter identity and settings
    ("inverter_serial", "1234567890"),
    ("protocol_version", 0x0201),
    ("mppt_count", 2),
    ("phase_count", 1),
    ("battery_capacity", 314),
    ("battery_float_voltage", 55.2),
    ("batt_shutdown_soc", 15),
    ("batt_restart_soc", 35),
    ("batt_low_soc", 25),
    ("grid_charge_start_soc", 30),
    ("grid_charge_current", 35),
    ("grid_charge_enabled", True),
    ("energy_pattern", "Load First"),
    ("work_mode", "Zero Export to Load"),
    ("io_mode", "Generator"),
    ("bms_protocol", "PYLON"),
    ("grid_voltage_high_limit", 265.0),
    ("grid_voltage_low_limit", 195.0),
    ("grid_peak_power", 5000),
    ("gen_peak_power", 2300),
    # status words
    ("device_state", "Normal"),
    ("alarm", "OK"),
    ("fault", "OK"),
    ("inverter_problem", False),
    ("bms_problem", False),
    ("grid_connected", True),
    # month / year counters (kellerza single_phase.py)
    ("month_solar", 94.0),
    ("month_load", 294.7),
    ("year_solar", 94.0),
    ("inverter_clock", datetime(2026, 10, 31, 8, 11, 42)),
])
def test_decode(key, expected):
    assert _decode()[key] == expected


def test_solar_power_is_pv1_plus_pv2():
    data = _decode()
    assert data["solar_power"] == data["pv1_power"] + data["pv2_power"] == 101


def test_tou_schedule():
    data = _decode()
    assert data["tou_enabled"] is True
    assert data["tou_days"] == ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    slots = data["tou_slots"]
    assert [(s["start"], s["end"], s["soc"]) for s in slots] == [
        ("01:00", "05:00", 50), ("05:00", "09:00", 50), ("09:00", "13:00", 95),
        ("13:00", "17:00", 95), ("17:00", "20:00", 100), ("20:00", "01:00", 75),
    ]
    assert [s["grid_charge"] for s in slots] == [False] * 4 + [True, False]
    assert all(s["gen_charge"] for s in slots)
    assert r.active_tou_slot(slots, "08:11") == 1
    assert r.active_tou_slot(slots, "23:30") == 5
    assert r.active_tou_slot(slots, "00:59") == 5


def test_fault_bits_are_named():
    assert r._bit_texts(0, r.SP_FAULT_BITS) == "OK"
    assert r._bit_texts(1 << 22 | 1 << 40, r.SP_FAULT_BITS) == "AC current leakage, bit 40"


def test_telemetry_alone_has_no_settings():
    data = _decode(config=False)
    assert "battery_capacity" not in data and "tou_slots" not in data
    assert data["battery_soc"] == 52


def test_grid_relay_drives_grid_connected():
    data = _decode()
    assert infer_grid_connected(data) is True
    relay_off = dict(TELEMETRY)
    block = list(relay_off[182])
    block[194 - 182] = 0
    relay_off[182] = block
    assert infer_grid_connected(r.decode_single_phase(relay_off)) is False


def test_no_three_phase_keys():
    assert not any(k.endswith(("_l1", "_l2", "_l3")) for k in _decode())


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
    assert await async_detect_profile(t) == r.PROFILE_SINGLE_PHASE
    assert t.reads == [(r.REG_DEVICE_TYPE, 1)]


@pytest.mark.asyncio
async def test_full_poll_reads_every_tier_and_never_the_device_type():
    t = _Transport()
    data = await async_poll(t, profile=r.PROFILE_SINGLE_PHASE)
    assert t.reads == r.SP_LIVE_BLOCKS + r.SP_FAST_BLOCKS + r.SP_SLOW_BLOCKS
    assert data["battery_capacity"] == 314


@pytest.mark.asyncio
async def test_live_only_poll_reads_live_blocks():
    t = _Transport()
    data = await async_poll(t, profile=r.PROFILE_SINGLE_PHASE, tiers={r.TIER_LIVE})
    assert t.reads == r.SP_LIVE_BLOCKS
    # The live set is what a 5 s dashboard needs.
    assert {"battery_soc", "battery_power", "grid_power", "house_load", "solar_power"} <= data.keys()


def test_every_single_phase_control_reads_back_on_the_slow_tier():
    # A control whose register is never polled would show a value it cannot see.
    for key, reg in r.PROFILES[r.PROFILE_SINGLE_PHASE].controls.items():
        assert any(start <= reg < start + n for start, n in r.SP_SLOW_BLOCKS), key
        assert key in _decode(), key


@pytest.mark.asyncio
async def test_settings_read_failure_keeps_telemetry():
    class Flaky(_Transport):
        async def read(self, address, count):
            if address == 200:
                raise TimeoutError
            return await super().read(address, count)

    data = await async_poll(Flaky(), profile=r.PROFILE_SINGLE_PHASE)
    assert data["battery_soc"] == 52 and "battery_capacity" not in data


# --- SUN-6K-SG04LP1-EU capture (PR #3, logger reply 21511) --------------------
# Raw +ok= frames at the app's block starts, which differ from ours: the decoder
# looks registers up by address, so any covering blocks decode the same.
SUN6K_FRAMES = {
    76: "+ok=01030800460000987C138B11FC",
    108: "+ok=01030A00470B3D0013001500000A32",
    150: "+ok=01030A09D3000009D3000009D4FEE8",
    164: "+ok=01032000B400000008002400000024000000000000018F0000018F01B3000001B301B3D810",
    180: "+ok=01031E000009DA04E216AA006401B30217000000000000FFC4FF97138D138D00018EDC",
}


@pytest.mark.parametrize("key,expected", [
    ("battery_voltage", 58.02),
    ("battery_soc", 100),
    ("battery_temp", 25.0),
    ("battery_power", -60),
    ("solar_power", 535),
    ("house_load", 435),
    ("grid_power", 36),
    ("inverter_output_power", 399),
    ("grid_voltage", 251.5),
    ("grid_frequency", 50.03),
    ("daily_solar", 7.1),
    ("daily_grid_import", 7.0),
])
def test_sun6k_capture_decodes(key, expected):
    words = {start: p.parse_read(frame) for start, frame in SUN6K_FRAMES.items()}
    assert r.decode_single_phase(words)[key] == expected
