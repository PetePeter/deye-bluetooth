"""P2 — register decode tests.

Fixtures are the literal `+ok=` frames captured from the device:
- the 6 telemetry blocks in local-deye-cloud/captures/app_readmap.txt, and
- one live 0x0210 count-14 read (to reach inverter_temp at 0x021D).

Each frame is parsed by the real protocol.parse_read, so these assert the
decode offsets/scaling against genuine device bytes — not against re-derived
assumptions. Every expected value was confirmed by a same-second BLE-vs-HA
comparison (local-deye-cloud/docs/stats-register-decode.md).
"""
import pytest

from custom_components.deye_ble import protocol as p
from custom_components.deye_ble import registers as r


# --- Captured frames (block_start -> raw +ok= response) ---------------------

FRAMES: dict[int, str] = {
    0x0202: "+ok=01031C00040068349B000031A70000000000043BEC000010B00000006C2DC084C7",
    0x0210: "+ok=01031C0000005100000000000000000C890000000000000000000004E205E665A3",
    0x024A: "+ok=01030C046A14A6002E0000FED8000D9095",
    0x0256: "+ok=01031409700940094F000000000000FFA6FF000130FFD65280",
    0x0270: "+ok=0103200130FFD600000973093B094300A000A000BE01B201A701EE0547054713830000E9C0",
    0x0280: "+ok=010320015800A7031E051D097609400942000000000000015800A7031E051D051D1383E5C8",
    0x02A0: "+ok=01032006F70000000000000A330044001F00000000000000000000000000000000FFFFA4B0",
}


def _words_by_reg(*block_starts: int) -> dict[int, list[int]]:
    """Parse the named captured frames into a {start: words} poll snapshot."""
    starts = block_starts or tuple(FRAMES)
    return {start: p.parse_read(FRAMES[start]) for start in starts}


@pytest.fixture
def poll() -> dict[str, float | int | str]:
    """A full decode of all captured telemetry frames."""
    return r.decode(_words_by_reg())


# --- Per-block decode against real bytes ------------------------------------

def test_energy_totals_block_0x0202(poll):
    assert poll["total_battery_charge"] == 1346.7
    assert poll["total_battery_discharge"] == 1271.1
    assert poll["total_grid_import"] == 1534.0
    assert poll["total_grid_export"] == 427.2
    assert poll["total_consumption"] == 1171.2
    assert poll["daily_grid_import"] == 0.0
    assert poll["daily_grid_export"] == 0.4


def test_solar_and_temps_block_0x0210(poll):
    assert poll["daily_solar"] == 8.1
    assert poll["total_solar"] == 320.9
    assert poll["inverter_temp"] == 51.0  # (1510 - 1000) / 10


def test_battery_block_0x024A(poll):
    assert poll["battery_temp"] == 13.0      # (1130 - 1000) / 10
    assert poll["battery_voltage"] == 52.86  # 5286 * 0.01
    assert poll["battery_soc"] == 46
    assert poll["battery_power"] == -296     # 0xFED8 signed


def test_grid_power_signed_block_0x0256(poll):
    assert poll["grid_power"] == -42  # 0xFFD6 signed, register 0x025F


def test_inverter_phases_block_0x0270(poll):
    assert poll["inverter_power_l1"] == 434
    assert poll["inverter_power_l2"] == 423
    assert poll["inverter_power_l3"] == 494


def test_inverter_phase_sum_matches_total_register(poll):
    # Register 0x027C holds the inverter total (1351 W) — a built-in cross check.
    total = p.parse_read(FRAMES[0x0270])[0x027C - 0x0270]
    assert poll["inverter_power_l1"] + poll["inverter_power_l2"] + poll["inverter_power_l3"] == total


def test_load_block_0x0280(poll):
    assert poll["house_load"] == 1309  # 0x0283
    assert poll["ups_power"] == 1309   # 0x028D (mirrors load while grid-connected)


def test_solar_power_block_0x02A0(poll):
    assert poll["solar_power"] == 1783  # 0x02A0


# --- Full poll coverage ------------------------------------------------------

# The 19 telemetry/control values decode() must produce. daily_consumption is
# derived in P4 from total_consumption (the 20th sensor key); work_mode is the
# separate 21st integration value, decoded below as an enum label.
EXPECTED_KEYS = {
    "solar_power", "house_load", "grid_power", "battery_power", "ups_power",
    "battery_soc", "battery_voltage", "battery_temp", "inverter_temp",
    "daily_solar", "total_solar", "total_grid_import", "total_grid_export",
    "total_battery_charge", "total_battery_discharge", "max_sell_power",
    "inverter_power_l1", "inverter_power_l2", "inverter_power_l3",
}


def test_full_poll_produces_all_telemetry_keys(poll):
    control = r.decode({0x008E: [0, 100]})  # work_mode=0, max_sell=100
    keys = set(poll) | set(control)
    assert EXPECTED_KEYS <= keys


def test_daily_consumption_is_not_decoded(poll):
    # No register exists for it; it is derived later. Must NOT be fabricated.
    assert "daily_consumption" not in poll


def test_partial_poll_omits_missing_keys():
    # Only the battery block present -> only its keys, nothing else invented.
    data = r.decode(_words_by_reg(0x024A))
    assert "battery_soc" in data
    assert "solar_power" not in data
    assert "grid_power" not in data


# --- Control registers -------------------------------------------------------

def test_max_sell_power_from_real_single_register_frame():
    # 0x008F = 100 W, captured write read-back frame.
    data = r.decode({0x008F: p.parse_read("+ok=0103020064B9AF")})
    assert data["max_sell_power"] == 100


@pytest.mark.parametrize("raw,label", [
    (0, "Selling First"),
    (1, "Zero Export to Load"),
    (2, "Zero Export to CT"),
])
def test_work_mode_decode(raw, label):
    assert r.decode({0x008E: [raw]})["work_mode"] == label


def test_tou_charge_window_decode():
    # 0x0095 start, 0x0096 end (HHMM), 0x00A7 target SOC %.
    data = r.decode({0x0095: [1100, 1500], 0x00A7: [100]})
    assert data["charge_start"] == "11:00"
    assert data["charge_end"] == "15:00"
    assert data["charge_soc"] == 100


def test_tou_invalid_hhmm_is_omitted():
    # An unset slot (0xFFFF) is not a valid HHMM -> key omitted, not fabricated.
    data = r.decode({0x0095: [0xFFFF, 0xFFFF]})
    assert "charge_start" not in data
    assert "charge_end" not in data


def test_discharge_soc_decode_from_widened_slot_block():
    # Slot SOCs 0x00A6..0x00AB = [6, 100, 6, 6, 6, 6]: slot 2 (0x00A7) is the
    # charge target (100), every non-charge slot is the discharge floor (6).
    data = r.decode({0x00A6: [6, 100, 6, 6, 6, 6]})
    assert data["discharge_soc"] == 6   # 0x00A6, representative non-charge slot
    assert data["charge_soc"] == 100    # 0x00A7 still decodes from the same block


def test_discharge_soc_regs_are_the_non_charge_slots():
    # The five slots written as the discharge floor — slot 2 (charge) excluded.
    assert r.DISCHARGE_SOC_REGS == [0x00A6, 0x00A8, 0x00A9, 0x00AA, 0x00AB]
    assert r.REG_CHARGE_SOC not in r.DISCHARGE_SOC_REGS


def test_work_mode_unknown_value():
    assert r.decode({0x008E: [7]})["work_mode"] == "Unknown (7)"


# --- Encoders / signedness ---------------------------------------------------

def test_work_mode_roundtrip():
    for raw, label in r.WORK_MODE_LABELS.items():
        assert r.encode_work_mode(label) == raw


def test_hhmm_roundtrip():
    for raw in (0, 1100, 1400, 1500, 2359):
        assert r.encode_hhmm(r.decode_hhmm(raw)) == raw


def test_hhmm_encode_known_values():
    assert r.encode_hhmm("14:00") == 1400
    assert r.encode_hhmm("15:00") == 1500


@pytest.mark.parametrize("bad", [
    "24:00", "23:60", "-1:00", "1200", "12:00:00", "ab:cd",
    "1:2", "01:2", "001:02",
])
def test_hhmm_encode_rejects_invalid(bad):
    with pytest.raises(ValueError):
        r.encode_hhmm(bad)


def test_hhmm_decode_rejects_invalid():
    with pytest.raises(ValueError):
        r.decode_hhmm(2400)


@pytest.mark.parametrize("raw,expected", [
    (0x0000, 0),
    (0x0001, 1),
    (0x7FFF, 32767),
    (0x8000, -32768),
    (0xFFFF, -1),
    (0xFED8, -296),
])
def test_signed16(raw, expected):
    assert r._signed16(raw) == expected
