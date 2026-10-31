"""P2 — Register map, telemetry decoders, and control encoders.

Maps the Deye logger's Modbus holding registers to the Home Assistant entity
keys used by ha-deyecloud-bridge, so the BLE integration is a drop-in.

The telemetry addresses below are the ones this inverter (Deye 8K hybrid,
single-phase LP1 register layout) actually answers on. They come from the ESP32
roof-controller fastpass poller that read this unit before this integration, and
whose values matched Deye Cloud. The map this file previously carried used the
0x02xx layout of the three-phase / high-voltage family, which decodes zeroes and
garbage here (battery voltage 0.0 V, solar power 59395 W, grid frequency 0 Hz).
A same-second comparison against Deye Cloud remains the acceptance test for any
change to this file.

`daily_consumption` has no dedicated register — the cloud computes it — so it is
NOT decoded here; it is derived in the entity layer (P4) from a midnight
baseline of `total_consumption` (0x0055, lifetime load energy), mirroring the
cloud bridge's daily_grid_import sensor.
"""
from __future__ import annotations

from datetime import datetime


# --- Poll plan --------------------------------------------------------------
# Every telemetry key below is covered by exactly one block, and no two blocks
# overlap (decode() resolves a register by finding the block that contains it).
# 0x02xx is deliberately absent: those addresses are not this unit's layout.

# Fast cycle — the live values the dashboard needs (PV power, battery power and
# SOC, SmartLoad, grid power/voltage, inverter output, grid connected, load).
# Four blocks, 42 registers: well under a second on this link.
FAST_BLOCKS: list[tuple[int, int]] = [
    (0x0096, 12),  # grid voltage L1 (0x0096), load voltage L1 (0x009D), grid current L1 (0x00A0)
    (0x00A2, 12),  # load current (0x00A4), SmartLoad (0x00A6), internal CT (0x00A7),
                   # grid (0x00A9), external CT (0x00AA), inverter power L1 (0x00AD)
    (0x00B0, 16),  # load power L1 (0x00B0), total load (0x00B2), battery temp (0x00B6),
                   # battery V (0x00B7), SOC (0x00B8), PV1/PV2 power (0x00BA/0x00BB),
                   # battery power/current (0x00BE/0x00BF)
    (0x00C2, 2),   # grid connected (0x00C2), gen connected (0x00C3)
]

# Slow cycle — energy counters, PV string volts/amps, temperatures, status and
# ratings. Polled every SLOW_TELEMETRY_INTERVAL and carried forward between
# reads so lifetime sensors never blank out.
SLOW_BLOCKS: list[tuple[int, int]] = [
    (0x0016, 5),   # 0x0016..0x001A — Inverter RTC (0x0016..0x0018), Status (0x0019), Rated Power (0x001A)
    (0x0035, 1),   # max solar power (W)
    (0x003B, 1),   # running status
    (0x0046, 6),   # 0x0046..0x004B — daily + lifetime battery charge/discharge
    (0x004C, 10),  # 0x004C..0x0055 — daily/total grid energy, grid frequency, load energy
    (0x005A, 2),   # 0x005A..0x005B — DC and AC heatsink temperatures
    (0x0060, 2),   # 0x0060..0x0061 — total PV production (uint32, low word first)
    (0x0065, 6),   # 0x0065..0x006A — Alarm 1 & 2 (0x0065..0x0066), Hardware Faults 1-4 (0x0067..0x006A)
    (0x006C, 5),   # 0x006C..0x0070 — daily PV production, PV1 V/I, PV2 V/I
    (0x00CC, 1),   # 0x00CC — Battery rated capacity (Ah)
    (0x00EB, 14),  # 0x00EB..0x00F8 — IO/AUX mode (0x00EB), smartload, work mode (0x00F4), max sell (0x00F5), solar sell (0x00F7), tou (0x00F8)
    (0x013C, 1),   # BMS battery SOC (%) — cross-check for the fast 0x00B8 value
]

# A full poll is both cycles: first refresh, and any caller wanting a snapshot.
READ_BLOCKS: list[tuple[int, int]] = FAST_BLOCKS + SLOW_BLOCKS

# Control registers, read on the slower config cycle (P3). Only registers whose
# meaning this installation has seen read back sensibly are polled here.
CONTROL_BLOCKS: list[tuple[int, int]] = [
    (0x0016, 5),   # 0x0016..0x001A inverter real-time clock & rated power (single-phase LP1)
    (0x00CC, 16),  # 0x00CC..0x00DB batt capacity, zero export, batt current limits & SOCs
    (0x00E4, 5),   # 0x00E4 time sync, 0x00E6 grid charge current, 0x00E8 grid charge enable
    (0x00EB, 14),  # 0x00EB..0x00F8 io_mode, smartload, energy priority, work mode, max sell, solar sell, tou
    (0x0118, 1),   # 0x0118 peak shaving master flags
    (0x0124, 2),   # 0x0124 gen peak power, 0x0125 grid peak power
]


# --- Control register addresses ---------------------------------------------

# Inverter real-time clock, three contiguous registers packed as byte pairs (single-phase LP1):
#   0x0016  hi = year-2000, lo = month
#   0x0017  hi = day,       lo = hour
#   0x0018  hi = minute,    lo = second
REG_CLOCK = 0x0016
CLOCK_WORD_COUNT = 3

# System Time panel flags. Bit 0 is Time Sync.
REG_TIME_SYNC = 0x00E4
TIME_SYNC_MASK = 0x0001

REG_ZERO_EXPORT_POWER = 0x00CE          # Zero Export Power offset (W, single-phase LP1)
REG_MAX_CHARGE_CURRENT = 0x00D2         # Battery max charge current (A, 0-185)
REG_MAX_DISCHARGE_CURRENT = 0x00D3      # Battery max discharge current (A, 0-185)
REG_BATT_SHUTDOWN_SOC = 0x00D9          # Battery shutdown SOC (%, single-phase LP1)
REG_BATT_RESTART_SOC = 0x00DA           # Battery restart SOC (%, single-phase LP1)
REG_BATT_LOW_SOC = 0x00DB               # Battery low-warning SOC (%, single-phase LP1)
REG_GRID_CHARGE_CURRENT = 0x00E6        # Grid charge current limit (A, 0-185)
REG_GRID_CHARGE_ENABLE = 0x00E8         # Grid charge master toggle (0/1)
REG_IO_MODE = 0x00EB                    # AUX / SmartLoad / Generator port mode
REG_SMARTLOAD_OFF_SOC = 0x00ED          # SmartLoad OFF SOC (%)
REG_SMARTLOAD_ON_SOC = 0x00EF           # SmartLoad ON SOC (%)
REG_SMARTLOAD_MIN_SOLAR_POWER = 0x00F1  # SmartLoad min solar power threshold (W)
REG_ENERGY_PRIORITY = 0x00F3            # Energy pattern: 0=Battery First, 1=Load First
REG_WORK_MODE = 0x00F4                  # Single-phase work mode: 0=Selling, 1=Zero Export Load, 2=Zero Export CT
REG_MAX_SELL_POWER = 0x00F5             # Single-phase holding register: Max Sell Power (W)
REG_SOLAR_SELL_ENABLE = 0x00F7          # Solar Sell / Export surplus toggle (0/1)
REG_TOU_ENABLE = 0x00F8                 # Time-of-Use master enable (0=Disabled, 0x00FF=Enabled)

# Peak-shaving controls (0x0118 bitfield and 0x0124/0x0125 power ceilings)
REG_PEAK_SHAVING_FLAGS = 0x0118
GRID_PEAK_SHAVE_MASK = 0x0100           # 0x0118 bit 8 — grid peak-shaving enable
GEN_PEAK_SHAVE_MASK = 0x0010            # 0x0118 bit 4 — generator peak-shaving enable
REG_GEN_PEAK_SHAVING_POWER = 0x0124     # Generator peak-shaving power limit (W)
REG_GRID_PEAK_SHAVING_POWER = 0x0125    # Grid peak-shaving power limit (W)

# Kept for backward compatibility
REG_CHARGE_SOC = 0x00A7
REG_GEN_PEAK_POWER = 0x0124
REG_GRID_PEAK_POWER = 0x0125
REG_TOU_SLOT2_START = 0x00FA
REG_TOU_SLOT3_START = 0x00FB
DISCHARGE_SOC_REGS: list[int] = []


def set_flag(raw: int, mask: int, on: bool) -> int:
    """Return *raw* with *mask* bits set when *on*, cleared otherwise."""
    return (raw | mask) if on else (raw & ~mask)


# --- Enums and Labels --------------------------------------------------------

WORK_MODE_LABELS: dict[int, str] = {
    0: "Sell to Grid",
    1: "Zero Export to Load",
    2: "Zero Export to CT",
}
_WORK_MODE_IDS: dict[str, int] = {v: k for k, v in WORK_MODE_LABELS.items()}

RUNNING_STATUS_LABELS: dict[int, str] = {
    0: "Standby",
    1: "Self-Checking",
    2: "Normal",
    3: "Alarm",
    4: "Fault",
}

IO_MODE_LABELS: dict[int, str] = {
    0: "Generator",
    1: "SmartLoad",
    2: "Microinverter",
}
_IO_MODE_IDS: dict[str, int] = {v: k for k, v in IO_MODE_LABELS.items()}

ENERGY_PRIORITY_LABELS: dict[int, str] = {
    0: "Battery First",
    1: "Load First",
}
_ENERGY_PRIORITY_IDS: dict[str, int] = {v: k for k, v in ENERGY_PRIORITY_LABELS.items()}


# --- Decode map --------------------------------------------------------------
# Each entry: (absolute_register, scale, signed, offset).
# Decoded value = (signed16(raw) if signed else raw - offset) * scale.
# Temperatures use offset=1000, scale=0.1  ->  (raw - 1000) / 10  °C.

_DECODE_MAP: dict[str, tuple[int, float, bool, int]] = {
    # Live power (W). solar_power is computed below as PV1 + PV2.
    "pv1_power":               (0x00BA, 1,    False, 0),
    "pv2_power":               (0x00BB, 1,    False, 0),
    "grid_power":              (0x00A9, 1,    True,  0),     # negative = exporting
    "battery_power":           (0x00BE, 1,    True,  0),     # negative = charging
    "house_load":              (0x00B2, 1,    True,  0),     # total load power
    "ups_power":               (0x00B0, 1,    True,  0),     # load power L1 (essential)
    "load_power_l1":           (0x00B0, 1,    True,  0),
    "gen_power":               (0x00A6, 1,    True,  0),     # Gen / SmartLoad port
    "external_ct_l1_power":    (0x00AA, 1,    True,  0),
    "internal_ct_l1_power":    (0x00A7, 1,    True,  0),
    "inverter_power_l1":       (0x00AD, 1,    True,  0),

    # Battery
    "battery_soc":             (0x00B8, 1,    False, 0),     # % (fast block)
    "battery_soc_bms":         (0x013C, 1,    False, 0),     # % (BMS mirror, slow)
    "battery_voltage":         (0x00B7, 0.01, False, 0),     # V
    "battery_current":         (0x00BF, 0.01, True,  0),     # A
    "battery_temp":            (0x00B6, 0.1,  False, 1000),  # °C

    # PV strings
    "pv1_voltage":             (0x006D, 0.1,  False, 0),     # V
    "pv1_current":             (0x006E, 0.1,  False, 0),     # A
    "pv2_voltage":             (0x006F, 0.1,  False, 0),     # V
    "pv2_current":             (0x0070, 0.1,  False, 0),     # A

    # AC metrics
    "grid_voltage_l1":         (0x0096, 0.1,  False, 0),     # V
    "grid_current_l1":         (0x00A0, 0.01, True,  0),     # A
    "grid_frequency":          (0x004F, 0.01, False, 0),     # Hz
    "load_voltage_l1":         (0x009D, 0.1,  False, 0),     # V
    "load_current_l1":         (0x00A4, 0.01, True,  0),     # A

    # Temperatures (heatsinks)
    "dc_temperature":          (0x005A, 0.1,  False, 1000),  # °C
    "inverter_temp":           (0x005B, 0.1,  False, 1000),  # °C (AC heatsink)

    # Status / rating
    "running_status":          (0x003B, 1,    False, 0),     # 0 standby .. 4 fault
    "max_solar_power":         (0x0035, 1,    False, 0),     # W
    "rated_power":             (0x001A, 1,    False, 0),     # W (inverter rating)
    "battery_rated_capacity":  (0x00CC, 1,    False, 0),     # Ah (battery rated capacity)

    # Energy — daily (kWh)
    "daily_solar":             (0x006C, 0.1,  False, 0),
    "daily_battery_charge":    (0x0046, 0.1,  False, 0),
    "daily_battery_discharge": (0x0047, 0.1,  False, 0),
    "daily_grid_import":       (0x004C, 0.1,  False, 0),
    "daily_grid_export":       (0x004D, 0.1,  False, 0),
    "daily_load_energy":       (0x0054, 0.1,  False, 0),

    # Energy — lifetime totals (kWh). total_solar is a 32-bit pair, see decode().
    "total_grid_import":       (0x004E, 0.1,  False, 0),
    "total_grid_export":       (0x0051, 0.1,  False, 0),
    "total_consumption":       (0x0055, 0.1,  False, 0),  # lifetime load energy
    "total_load_energy":       (0x0055, 0.1,  False, 0),

    # Control mirrors (decoded from CONTROL_BLOCKS)
    "zero_export_power":       (0x00CE, 1,    False, 0),     # W (grid-comp offset)
    "max_charge_current":      (0x00D2, 1,    False, 0),     # A
    "max_discharge_current":   (0x00D3, 1,    False, 0),     # A
    "batt_shutdown_soc":       (0x00D9, 1,    False, 0),     # %
    "batt_restart_soc":        (0x00DA, 1,    False, 0),     # %
    "batt_low_soc":            (0x00DB, 1,    False, 0),     # %
    "grid_charge_current":     (0x00E6, 1,    False, 0),     # A
    "smartload_off_soc":       (0x00ED, 1,    False, 0),     # %
    "smartload_on_soc":        (0x00EF, 1,    False, 0),     # %
    "smartload_min_solar_power":(0x00F1, 1,   False, 0),     # W
    "max_sell_power":          (0x00F5, 1,    False, 0),     # W
    "gen_peak_shaving_power":  (0x0124, 1,    False, 0),     # W
    "grid_peak_shaving_power": (0x0125, 1,    False, 0),     # W
}

# 32-bit lifetime PV production, little-endian word pair.
REG_TOTAL_PRODUCTION = 0x0060

# 32-bit lifetime battery energy counters, little-endian word pairs, 0.1 kWh.
REG_TOTAL_BATTERY_CHARGE = 0x0048
REG_TOTAL_BATTERY_DISCHARGE = 0x004A
_BATTERY_TOTALS: tuple[tuple[str, int], ...] = (
    ("total_battery_charge", REG_TOTAL_BATTERY_CHARGE),
    ("total_battery_discharge", REG_TOTAL_BATTERY_DISCHARGE),
)


DEYE_FAULT_CODES: dict[int, str] = {
    1: "F01: DC input polarity reversed",
    2: "F02: DC insulation impedance low",
    3: "F03: Leakage current high",
    4: "F04: Grounding fault",
    5: "F05: Grid voltage abnormal",
    6: "F06: DC/DC soft start failure",
    7: "F07: DC/DC overcurrent",
    8: "F08: Grid voltage low",
    9: "F09: Auxiliary power supply failure",
    10: "F10: Grid voltage high",
    11: "F11: Grid frequency high",
    12: "F12: Grid frequency low",
    13: "F13: Working mode changed",
    14: "F14: DC bus voltage high",
    15: "F15: DC bus voltage low",
    16: "F16: GFCI sensor failure",
    17: "F17: AC overcurrent software protection",
    18: "F18: AC overcurrent hardware protection",
    19: "F19: Tz_Integ_Fault failure",
    20: "F20: DC overcurrent hardware protection",
    21: "F21: DC overcurrent software protection",
    22: "F22: Tz_GFCI_OC_Fault",
    23: "F23: AC leakage current failure",
    24: "F24: DC component high",
    25: "F25: DC bus unbalance",
    26: "F26: DC bus pre-charge failure",
    27: "F27: Relay open check failure",
    28: "F28: Relay short check failure",
    29: "F29: Parallel communication failure",
    30: "F30: Parallel current unbalance",
    31: "F31: Battery reverse connection",
    32: "F32: Master/slave comms failure",
    33: "F33: AC overcurrent in backup mode",
    34: "F34: AC voltage high in backup mode",
    35: "F35: No AC Grid detected",
    36: "F36: Backup overload",
    37: "F37: Fan failure",
    38: "F38: Internal EEPROM read/write failure",
    41: "F41: Parallel CAN communication fault",
    42: "F42: Master missing in parallel system",
    46: "F46: Inverter heatsink over-temperature",
    47: "F47: Grid frequency out of range",
    48: "F48: Grid impedance high",
    56: "F56: DC Bus low voltage",
    58: "F58: BMS communication loss",
    63: "F63: ARC fault detected",
    64: "F64: Heatsink over-temperature failure",
}


# --- Decode -----------------------------------------------------------------

def _signed16(raw: int) -> int:
    """Interpret a 16-bit unsigned value as signed (two's complement)."""
    return raw if raw < 0x8000 else raw - 0x10000


def _lookup(words_by_reg: dict[int, list[int]], reg: int) -> int | None:
    """Return the raw word at absolute register *reg*, or None if not polled."""
    for block_start, words in words_by_reg.items():
        offset = reg - block_start
        if 0 <= offset < len(words):
            return words[offset]
    return None


def decode(words_by_reg: dict[int, list[int]]) -> dict[str, float | int | str | datetime]:
    """Decode raw register words into HA entity key/value pairs."""
    result: dict[str, float | int | str | datetime] = {}

    for key, (reg, scale, signed, offset) in _DECODE_MAP.items():
        raw = _lookup(words_by_reg, reg)
        if raw is None:
            continue
        value = _signed16(raw) if signed else raw
        value = (value - offset) * scale
        result[key] = int(value) if (scale == 1 and offset == 0) else round(value, 2)

    # Lifetime PV production is one 32-bit counter split over two registers
    low = _lookup(words_by_reg, REG_TOTAL_PRODUCTION)
    high = _lookup(words_by_reg, REG_TOTAL_PRODUCTION + 1)
    if low is not None and high is not None:
        result["total_solar"] = round(((high << 16) | low) * 0.1, 2)

    # Lifetime battery charge / discharge: same uint32 LE pair layout, 0.1 kWh.
    for key, reg in _BATTERY_TOTALS:
        low = _lookup(words_by_reg, reg)
        high = _lookup(words_by_reg, reg + 1)
        if low is not None and high is not None:
            result[key] = round(((high << 16) | low) * 0.1, 2)

    # Solar power is the sum of the two strings
    if "pv1_power" in result or "pv2_power" in result:
        result["solar_power"] = int(
            result.get("pv1_power", 0) + result.get("pv2_power", 0)
        )
        result["total_dc_input_power"] = result["solar_power"]

    # Grid Reactive Power (VAR) synthesized from apparent power (V*I) and active power (P)
    if "grid_voltage_l1" in result and "grid_current_l1" in result and "grid_power" in result:
        v = result["grid_voltage_l1"]
        i = abs(result["grid_current_l1"])
        p = abs(result["grid_power"])
        s = v * i
        if s > p:
            result["grid_total_reactive_power"] = round((s * s - p * p) ** 0.5, 1)
        else:
            result["grid_total_reactive_power"] = 0.0

    # Work mode
    work_mode_raw = _lookup(words_by_reg, REG_WORK_MODE)
    if work_mode_raw is not None:
        result["work_mode"] = WORK_MODE_LABELS.get(work_mode_raw, f"Unknown ({work_mode_raw})")

    # Inverter status
    running_status_raw = _lookup(words_by_reg, 0x003B)
    if running_status_raw is not None:
        status_label = RUNNING_STATUS_LABELS.get(running_status_raw, "Normal")
        result["running_status"] = status_label
        result["inverter_status"] = status_label

    # AC voltage alias
    if "grid_voltage_l1" in result:
        result["ac_voltage_l1"] = result["grid_voltage_l1"]

    # IO / AUX mode
    io_mode_raw = _lookup(words_by_reg, REG_IO_MODE)
    if io_mode_raw is not None:
        result["io_mode"] = IO_MODE_LABELS.get(io_mode_raw, f"Unknown ({io_mode_raw})")

    # Energy Priority
    energy_priority_raw = _lookup(words_by_reg, REG_ENERGY_PRIORITY)
    if energy_priority_raw is not None:
        result["energy_priority"] = ENERGY_PRIORITY_LABELS.get(energy_priority_raw, "Load First")

    # Solar Sell switch mirror
    solar_sell_raw = _lookup(words_by_reg, REG_SOLAR_SELL_ENABLE)
    if solar_sell_raw is not None:
        result["solar_sell"] = bool(solar_sell_raw)

    # Grid Charge master switch mirror
    grid_charge_raw = _lookup(words_by_reg, REG_GRID_CHARGE_ENABLE)
    if grid_charge_raw is not None:
        result["grid_charge_enabled"] = bool(grid_charge_raw)

    # Time of Use master switch mirror
    tou_raw = _lookup(words_by_reg, REG_TOU_ENABLE)
    if tou_raw is not None:
        result["tou_enabled"] = bool(tou_raw)

    # Peak shaving flags
    peak_raw = _lookup(words_by_reg, REG_PEAK_SHAVING_FLAGS)
    if peak_raw is not None:
        result["peak_shaving_flags_raw"] = peak_raw
        result["grid_peak_shaving"] = bool(peak_raw & GRID_PEAK_SHAVE_MASK)
        result["gen_peak_shaving"] = bool(peak_raw & GEN_PEAK_SHAVE_MASK)

    # Inverter RTC
    clock_words = [_lookup(words_by_reg, REG_CLOCK + i) for i in range(CLOCK_WORD_COUNT)]
    if all(w is not None for w in clock_words):
        clock = decode_clock(clock_words)
        if clock is not None:
            result["inverter_clock"] = clock

    # Time Sync enable
    time_sync_raw = _lookup(words_by_reg, REG_TIME_SYNC)
    if time_sync_raw is not None:
        result["time_sync"] = bool(time_sync_raw & TIME_SYNC_MASK)

    # UPS load = total load minus SmartLoad
    if "house_load" in result and "gen_power" in result:
        result["ups_load_power"] = round(result["house_load"] - result["gen_power"], 1)
    elif "ups_power" in result:
        result["ups_load_power"] = result["ups_power"]

    # Operating states
    if "grid_power" in result:
        grid_power = result["grid_power"]
        result["grid_status"] = (
            "BUY" if grid_power > 20 else "SELL" if grid_power < -20 else "IDLE"
        )
    if "battery_power" in result:
        battery_power = result["battery_power"]
        result["battery_status"] = (
            "Charging"
            if battery_power < -20
            else "Discharging" if battery_power > 20 else "Stand-by"
        )

    # Decoded hardware faults (Registers 103..106: 0x0067..0x006A)
    f0 = _lookup(words_by_reg, 0x0067)
    f1 = _lookup(words_by_reg, 0x0068)
    f2 = _lookup(words_by_reg, 0x0069)
    f3 = _lookup(words_by_reg, 0x006A)
    if all(w is not None for w in (f0, f1, f2, f3)):
        fault_mask = f0 | (f1 << 16) | (f2 << 32) | (f3 << 48)
        if fault_mask == 0:
            result["has_fault"] = False
            result["fault_message"] = "OK"
            result["fault_codes"] = []
        else:
            result["has_fault"] = True
            active = []
            for bit in range(64):
                if fault_mask & (1 << bit):
                    code_num = bit + 1
                    active.append(DEYE_FAULT_CODES.get(code_num, f"F{code_num:02d}: Hardware Fault {code_num}"))
            result["fault_codes"] = active
            result["fault_message"] = "; ".join(active) if active else "Fault Active"

    # Decoded alarms (Registers 101..102: 0x0065..0x0066)
    a0 = _lookup(words_by_reg, 0x0065)
    a1 = _lookup(words_by_reg, 0x0066)
    if a0 is not None and a1 is not None:
        alarm_mask = a0 | (a1 << 16)
        result["has_alarm"] = bool(alarm_mask != 0)
        result["alarm_message"] = "Warning Active" if alarm_mask != 0 else "OK"

    return result


# --- Encoders ---------------------------------------------------------------

def decode_clock(words: list[int]) -> datetime | None:
    if len(words) < CLOCK_WORD_COUNT:
        return None
    year, month = 2000 + (words[0] >> 8), words[0] & 0xFF
    day, hour = words[1] >> 8, words[1] & 0xFF
    minute, second = words[2] >> 8, words[2] & 0xFF
    try:
        return datetime(year, month, day, hour, minute, second)
    except ValueError:
        return None


def encode_clock(value: datetime) -> list[int]:
    if not 2000 <= value.year <= 2255:
        raise ValueError(f"year out of register range: {value.year}")
    return [
        ((value.year - 2000) << 8) | value.month,
        (value.day << 8) | value.hour,
        (value.minute << 8) | value.second,
    ]


def encode_work_mode(label: str) -> int:
    return _WORK_MODE_IDS[label]


def encode_io_mode(label: str) -> int:
    return _IO_MODE_IDS[label]


def encode_energy_priority(label: str) -> int:
    return _ENERGY_PRIORITY_IDS[label]


def encode_hhmm(time_str: str) -> int:
    parts = time_str.split(":")
    if len(parts) != 2 or not all(len(p) == 2 and p.isdigit() for p in parts):
        raise ValueError(f"expected canonical 'HH:MM', got {time_str!r}")
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"time out of range: {time_str!r}")
    return hour * 100 + minute


def decode_hhmm(raw: int) -> str:
    hour, minute = divmod(raw, 100)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"invalid HHMM register value: {raw}")
    return f"{hour:02d}:{minute:02d}"
