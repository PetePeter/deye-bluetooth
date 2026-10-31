"""P2 — Register map, telemetry decoders, and control encoders.

Maps the Deye logger's Modbus holding registers to the Home Assistant entity
keys used by ha-deyecloud-bridge, so the BLE integration is a drop-in.

Every register address + scaling below was confirmed by a live same-second
comparison of a BLE read against the corresponding Deye Cloud / HA sensor value
(see local-deye-cloud/docs/stats-register-decode.md and the capture in
local-deye-cloud/captures/app_readmap.txt). The decode tests assert against the
exact captured `+ok=` frames, so they catch any regression against real device
data rather than against re-derived assumptions.

`daily_consumption` has no dedicated register — the cloud computes it — so it is
NOT decoded here; it is derived in the entity layer (P4) from a midnight
baseline of `total_consumption` (0x020F), mirroring the cloud bridge's
daily_grid_import sensor.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable


# --- Poll plan --------------------------------------------------------------
# Minimal set of blocks that covers every telemetry key below. The Deye app
# polls 18 blocks; we only read what we decode. 0x0210 is read with count 14
# (the app used 8) so the block reaches inverter_temp at 0x021D.

# Polling tiers (see PROFILES at the end of the module): LIVE every poll,
# FAST about once a minute, SLOW on the configurable slow interval.
LIVE_BLOCKS: list[tuple[int, int]] = [
    (0x024A, 6),   # 0x024A..0x024F — battery temp/voltage/soc/power
    (0x0256, 12),  # 0x0256..0x0261 — grid power (0x025F) + grid frequency (0x0261)
    (0x0270, 16),  # 0x0270..0x027F — inverter output L1-3 + grid voltages 0x0273-0x0275
    (0x0280, 16),  # 0x0280..0x028F — house load / UPS load
    (0x02A0, 16),  # 0x02A0..0x02AF — PV input power + PV1 voltage/current
]
FAST_BLOCKS: list[tuple[int, int]] = [
    (0x0202, 14),  # 0x0202..0x020F — energy totals + daily grid
    (0x0210, 14),  # 0x0210..0x021D — daily/total solar + temperatures
]

# Slow tier: BMS limits plus the control registers (best-effort reads).
SLOW_BLOCKS: list[tuple[int, int]] = [
    (0x00D2, 4),   # 0x00D2..0x00D5 — BMS charge/discharge voltage + current limits
    (0x003E, 3),   # 0x003E..0x0040 inverter real-time clock
    (0x0068, 1),   # 0x0068 zero-export power (W, signed)
    (0x006C, 2),   # 0x006C max charge current, 0x006D max discharge current (A)
    (0x0073, 3),   # 0x0073/0x0074/0x0075 batt shutdown/restart/low SOC (%)
    (0x008E, 2),   # 0x008E work mode, 0x008F max sell power
    (0x0095, 2),   # 0x0095 charge window start, 0x0096 charge window end (HHMM)
    (0x00A6, 6),   # 0x00A6..0x00AB TOU slot 1-6 target SOC (%) — charge + discharge
    (0x00B2, 14),  # 0x00B2 peak-shaving flags + 0x00BE/0x00BF gen/grid shave power
    (0x00E4, 1),   # 0x00E4 System Time panel flags — bit 0 is Time Sync
]


# --- Control register addresses ---------------------------------------------

# Inverter real-time clock, three contiguous registers packed as byte pairs:
#   0x003E  hi = year-2000, lo = month
#   0x003F  hi = day,       lo = hour
#   0x0040  hi = minute,    lo = second
# Confirmed against two captured cloud writes taken at different times —
# 0x1A08/0x0908/0x3800 ("System Time 2026/08/09 08:56") and 0x1A08/0x0909/0x1E00
# (the app's "set 09:30") — see
# local-deye-cloud/docs/inverter-clock-2026-08-09.md. Writing these registers
# alone does nothing: the RTC latches them only on the Time Sync falling edge
# below (coordinator.async_sync_clock drives the full sequence).
REG_CLOCK = 0x003E
CLOCK_WORD_COUNT = 3

# System Time panel flags. Bit 0 is Time Sync, and the RTC commits the staged
# clock registers when it goes 1 -> 0. Live values are 0x0AEB (on) / 0x0AEA
# (off): the app only ever writes the low byte, and the high byte carries the
# panel's other controls (AM/PM, Auto Dim, Beep, Factory Reset) — so this is
# always a read-modify-write via set_flag, never a literal.
#
# Time Sync OFF also stops the logger's cloud calibration reaching the RTC.
# Since the commit edge *ends* with the bit clear, any writer must put it back
# where it found it, or setting the clock silently disables the only other
# mechanism that corrects it. That is the origin of the year-out fault.
REG_TIME_SYNC = 0x00E4
TIME_SYNC_MASK = 0x0001

REG_ZERO_EXPORT_POWER = 0x0068      # zero-export power / grid-comp offset (W, signed)
REG_MAX_CHARGE_CURRENT = 0x006C     # battery max charge current (A)
REG_MAX_DISCHARGE_CURRENT = 0x006D  # battery max discharge current (A)
REG_BATT_SHUTDOWN_SOC = 0x0073      # battery shutdown SOC (%)
REG_BATT_RESTART_SOC = 0x0074       # battery restart SOC (%)
REG_BATT_LOW_SOC = 0x0075           # battery low-warning SOC (%)
REG_WORK_MODE = 0x008E
REG_MAX_SELL_POWER = 0x008F
REG_TOU_SLOT2_START = 0x0095  # charge window start
REG_TOU_SLOT3_START = 0x0096  # charge window end
REG_CHARGE_SOC = 0x00A7       # slot 2 target SOC — the charge ceiling

# Discharge floor = the target SOC on every NON-charge slot (1, 3, 4, 5, 6).
# Slot 2 (0x00A7) is the grid-charge slot and is excluded. Writing the same
# value to all five makes the inverter hold that SOC whenever it isn't charging.
DISCHARGE_SOC_REGS = [0x00A6, 0x00A8, 0x00A9, 0x00AA, 0x00AB]

# Peak-shaving controls (confirmed by live MITM 2026-07-09, device SN 2507245326:
# opType-5 write == device readback ACK). 0x00B2 is a packed Advanced-Function-1
# bitfield — the enable flags share it with other, unmapped functions (ARC,
# BMS-stop, Parallel, DRM...), so toggling one MUST read-modify-write to preserve
# the rest. 0x00BE/0x00BF are plain watt setpoints.
REG_PEAK_SHAVING_FLAGS = 0x00B2
REG_GEN_PEAK_POWER = 0x00BE     # generator peak-shaving power cap (W)
REG_GRID_PEAK_POWER = 0x00BF    # grid peak-shaving power cap (W)

GEN_PEAK_SHAVE_MASK = 0x0004    # 0x00B2 bit 2 — generator peak-shaving enable
GRID_PEAK_SHAVE_MASK = 0x0010   # 0x00B2 bit 4 — grid peak-shaving enable


def set_flag(raw: int, mask: int, on: bool) -> int:
    """Return *raw* with *mask* bits set when *on*, cleared otherwise.

    Used for read-modify-write on the packed 0x00B2 flag register so a single
    peak-shaving toggle never clobbers the other function bits sharing it.
    """
    return (raw | mask) if on else (raw & ~mask)


# --- Work mode enum ---------------------------------------------------------

WORK_MODE_LABELS: dict[int, str] = {
    0: "Selling First",
    1: "Zero Export to Load",
    2: "Zero Export to CT",
}

_WORK_MODE_IDS: dict[str, int] = {v: k for k, v in WORK_MODE_LABELS.items()}


# --- Decode map --------------------------------------------------------------
# Each entry: (absolute_register, scale, signed, offset).
# Decoded value = (signed16(raw) if signed else raw - offset) * scale.
# Temperatures use offset=1000, scale=0.1  ->  (raw - 1000) / 10  °C.

_DECODE_MAP: dict[str, tuple[int, float, bool, int]] = {
    # Live power (W)
    "solar_power":             (0x02A0, 1,    False, 0),
    "grid_power":              (0x025F, 1,    True,  0),
    "battery_power":           (0x024E, 1,    True,  0),
    "house_load":              (0x0283, 1,    True,  0),
    "ups_power":               (0x028D, 1,    True,  0),
    "inverter_power_l1":       (0x0279, 1,    True,  0),
    "inverter_power_l2":       (0x027A, 1,    True,  0),
    "inverter_power_l3":       (0x027B, 1,    True,  0),

    # Battery
    "battery_soc":             (0x024C, 1,    False, 0),     # %
    "battery_voltage":         (0x024B, 0.01, False, 0),     # V
    # PV string 1 operating point. High V + low A at a capped output means the
    # MPPT is backed off (sun available, load-limited); PV2 is unwired here.
    "pv1_voltage":             (0x02A4, 0.1,  False, 0),     # V
    "pv1_current":             (0x02A5, 0.1,  False, 0),     # A
    "battery_temp":            (0x024A, 0.1,  False, 1000),  # °C
    "inverter_temp":           (0x021D, 0.1,  False, 1000),  # °C

    # BMS limits (slow telemetry, cross-checked against the app BMS panel)
    "bms_charge_voltage":         (0x00D2, 0.01, False, 0),  # V
    "bms_discharge_voltage":      (0x00D3, 0.01, False, 0),  # V
    "bms_charge_current_limit":   (0x00D4, 1,    False, 0),  # A
    "bms_discharge_current_limit":(0x00D5, 1,    False, 0),  # A

    # Grid AC metrics (÷10 V, ÷100 Hz). Phase order is L1, L3, L2 — only the L2
    # register (0x0275) was directly cross-checked against the app.
    "grid_voltage_l1":         (0x0273, 0.1,  False, 0),     # V
    "grid_voltage_l3":         (0x0274, 0.1,  False, 0),     # V
    "grid_voltage_l2":         (0x0275, 0.1,  False, 0),     # V
    "grid_frequency":          (0x0261, 0.01, False, 0),     # Hz

    # Energy — daily (kWh)
    "daily_solar":             (0x0211, 0.1,  False, 0),
    "daily_grid_import":       (0x0208, 0.1,  False, 0),
    "daily_grid_export":       (0x0209, 0.1,  False, 0),

    # Energy — lifetime totals (kWh)
    "total_solar":             (0x0216, 0.1,  False, 0),
    "total_grid_import":       (0x020A, 0.1,  False, 0),
    "total_grid_export":       (0x020C, 0.1,  False, 0),
    "total_battery_charge":    (0x0204, 0.1,  False, 0),
    "total_battery_discharge": (0x0206, 0.1,  False, 0),
    "total_consumption":       (0x020F, 0.1,  False, 0),  # lifetime; daily_consumption derived in P4

    # Control mirrors (decoded from CONTROL_BLOCKS)
    "zero_export_power":       (0x0068, 1,    True,  0),     # W (grid-comp offset; negative = force import)
    "max_charge_current":      (0x006C, 1,    False, 0),     # A (battery charge current limit)
    "max_discharge_current":   (0x006D, 1,    False, 0),     # A (battery discharge current limit)
    "batt_shutdown_soc":       (0x0073, 1,    False, 0),     # % (battery shutdown floor)
    "batt_restart_soc":        (0x0074, 1,    False, 0),     # % (battery restart threshold)
    "batt_low_soc":            (0x0075, 1,    False, 0),     # % (battery low-warning threshold)
    "max_sell_power":          (0x008F, 1,    False, 0),     # W
    "charge_soc":              (0x00A7, 1,    False, 0),     # % (TOU charge target, slot 2)
    "discharge_soc":           (0x00A6, 1,    False, 0),     # % (TOU floor, slot 1 representative)
    "gen_peak_power":          (0x00BE, 1,    False, 0),     # W (generator peak-shaving cap)
    "grid_peak_power":         (0x00BF, 1,    False, 0),     # W (grid peak-shaving cap)
    # work_mode (enum), charge_start/charge_end (HHMM), and the 0x00B2 peak-shaving
    # enable bits are handled specially below.
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
    """Decode raw register words into HA entity key/value pairs.

    *words_by_reg* maps each polled block's start address to its list of
    register values (as returned by ``protocol.parse_read``). Keys whose
    registers were not present in *words_by_reg* are omitted, so a partial
    poll yields a partial dict rather than wrong values.
    """
    result: dict[str, float | int | str | datetime] = {}

    for key, (reg, scale, signed, offset) in _DECODE_MAP.items():
        raw = _lookup(words_by_reg, reg)
        if raw is None:
            continue
        value = _signed16(raw) if signed else raw
        value = (value - offset) * scale
        result[key] = int(value) if (scale == 1 and offset == 0) else round(value, 2)

    work_mode_raw = _lookup(words_by_reg, REG_WORK_MODE)
    if work_mode_raw is not None:
        result["work_mode"] = WORK_MODE_LABELS.get(work_mode_raw, f"Unknown ({work_mode_raw})")

    # Peak-shaving enable flags share the packed 0x00B2 register. Publish the raw
    # word too — the switch entities need it to read-modify-write a single bit
    # without clobbering the other function bits.
    flags_raw = _lookup(words_by_reg, REG_PEAK_SHAVING_FLAGS)
    if flags_raw is not None:
        result["peak_shaving_flags_raw"] = flags_raw
        result["grid_peak_shaving"] = bool(flags_raw & GRID_PEAK_SHAVE_MASK)
        result["gen_peak_shaving"] = bool(flags_raw & GEN_PEAK_SHAVE_MASK)

    # Inverter RTC — three packed registers, decoded together. An implausible
    # frame yields no key at all: undecoded beats quietly wrong for a clock.
    clock_words = [_lookup(words_by_reg, REG_CLOCK + i) for i in range(CLOCK_WORD_COUNT)]
    if all(w is not None for w in clock_words):
        clock = decode_clock(clock_words)
        if clock is not None:
            result["inverter_clock"] = clock

    # Time Sync enable — bit 0 of the packed System Time panel register. Only
    # the decoded bool is published; a writer must re-read the raw word inside
    # its own session, since the other bits are not ours to replay.
    time_sync_raw = _lookup(words_by_reg, REG_TIME_SYNC)
    if time_sync_raw is not None:
        result["time_sync"] = bool(time_sync_raw & TIME_SYNC_MASK)

    # TOU charge window times are stored as HHMM; decode to "HH:MM" strings.
    # An unset/invalid slot value (e.g. 0xFFFF) is omitted rather than published.
    for key, reg in (("charge_start", REG_TOU_SLOT2_START), ("charge_end", REG_TOU_SLOT3_START)):
        raw = _lookup(words_by_reg, reg)
        if raw is None:
            continue
        try:
            result[key] = decode_hhmm(raw)
        except ValueError:
            continue

    return result


# --- Single-phase hybrid profile ---------------------------------------------
# Everything above is the three-phase (SG04LP3-style) map. Single-phase
# low-voltage hybrids (SG01LP1/SG03LP1/SG05LP1) report device type 0x0003 in
# register 0 and keep their telemetry far lower, so the three-phase map decodes
# garbage there (SOC 0 %, PV 59 kW, temperatures -100 °C: 0x02A0 and every
# 14th register above 0x0250 read 0xE803 filler on these units).
#
# Confirmed on SUN-8K-SG05LP1-EU-SM2 + DL1000B (logger 21511), 2026-10-31: a
# full 0x0000-0x02FF dump matched against the same-minute Deye Cloud values
# (SOC 52 %, 52.22 V, 7.45 A, 389 W, 16.6 °C, PV1 239.8 V, totals 99.9 / 387.8 /
# 391.4 / 195.0 / 155.3 kWh, BMS 55.20 V / 160 A / 170 A). Addresses and lookups
# follow ha-solarman deye_hybrid.yaml and kellerza/sunsynk single_phase.py.
#
# The control registers differ too (0x006C, the three-phase max charge current,
# is daily PV here), so each profile carries its own control table (PROFILES).
# Single-phase control addresses were write/read-back verified on a
# SUN-8K-SG01LP1 + DL1000B (PR #6), and are gated by the read-only lock like
# every other write.

REG_DEVICE_TYPE = 0x0000
DEVICE_TYPE_SINGLE_PHASE = 0x0003

PROFILE_THREE_PHASE = "three_phase"
PROFILE_SINGLE_PHASE = "single_phase"


def profile_from_device_type(raw: int) -> str:
    """Map the register-0 device type to a decode profile."""
    return PROFILE_SINGLE_PHASE if raw == DEVICE_TYPE_SINGLE_PHASE else PROFILE_THREE_PHASE


# Reads above 16 registers get no BLE reply.
SP_LIVE_BLOCKS: list[tuple[int, int]] = [
    (150, 16),  # grid/inverter/load voltages, grid/CT/inverter currents
    (166, 16),  # gen/grid/CT/inverter/load power, load current, gen voltage
    (182, 15),  # battery temp/V/SOC/I/P, PV1/PV2 power, frequencies, relays, gen freq
]
SP_FAST_BLOCKS: list[tuple[int, int]] = [
    (59, 16),   # 59 state, 60/62 today energy/gen, 65-69 month/year, 70-74 battery energy
    (75, 16),   # 75-86 grid/load energy + grid frequency (79), 87-88 year load, 90 DC temp
    (91, 7),    # 91 AC temp, 92-95 generator energy, 96-97 total PV
    (101, 12),  # 101-106 alarm/fault words, 108 daily PV, 109-112 PV1/PV2 V + I
    (312, 13),  # BMS charge/discharge V, limits, SOC, V, I, temp, alarm/fault words
]
SP_SLOW_BLOCKS: list[tuple[int, int]] = [
    (2, 16),    # 2 protocol, 3-7 SN, 10-14 firmware, 16-17 rated power
    (18, 7),    # 18 MPPTs/phases, 22-24 inverter clock
    (200, 16),  # battery type, charge voltages, capacity, currents
    (216, 16),  # SOC/voltage thresholds, generator + grid charging
    (232, 16),  # grid charge enable, IO mode, energy pattern, work mode, max sell
    (248, 16),  # TOU enable, slot times + power
    (264, 16),  # TOU slot voltage/SOC/charge source
    (280, 14),  # peak shaving flags, grid protection limits, peak shaving power
    (325, 1),   # BMS protocol
]

SP_REG_CLOCK = 22  # 22-24, packed like the three-phase 0x003E-0x0040

# (register, scale, signed, offset) — same layout as _DECODE_MAP.
_SP_DECODE_MAP: dict[str, tuple[int, float, bool, int]] = {
    # Battery (inverter side)
    "battery_soc":             (184, 1,    False, 0),
    "battery_voltage":         (183, 0.01, False, 0),
    "battery_current":         (191, 0.01, True,  0),     # + discharge, - charge
    "battery_power":           (190, 1,    True,  0),     # + discharge, - charge
    "battery_temp":            (182, 0.1,  False, 1000),
    # Inverter
    "inverter_temp":           (90,  0.1,  False, 1000),  # DC side
    "inverter_ac_temp":        (91,  0.1,  False, 1000),
    "inverter_output_power":   (175, 1,    True,  0),
    "inverter_output_voltage": (154, 0.1,  False, 0),
    "inverter_output_current": (164, 0.01, True,  0),
    "inverter_output_frequency": (193, 0.01, False, 0),
    # PV
    "pv1_power":               (186, 1,    False, 0),
    "pv2_power":               (187, 1,    False, 0),
    "pv1_voltage":             (109, 0.1,  False, 0),
    "pv1_current":             (110, 0.1,  False, 0),
    "pv2_voltage":             (111, 0.1,  False, 0),
    "pv2_current":             (112, 0.1,  False, 0),
    # Grid
    "grid_power":              (169, 1,    True,  0),     # + import, - export
    "grid_inverter_side_power": (167, 1,   True,  0),
    "grid_voltage":            (150, 0.1,  False, 0),
    "grid_current":            (160, 0.01, True,  0),
    "grid_frequency":          (79,  0.01, False, 0),
    "external_ct_current":     (162, 0.01, True,  0),
    "external_ct_power":       (170, 1,    True,  0),
    # Load
    "house_load":              (178, 1,    True,  0),
    "ups_power":               (176, 1,    True,  0),
    "load_voltage":            (157, 0.1,  False, 0),
    "load_current":            (179, 0.01, True,  0),
    "load_frequency":          (192, 0.01, False, 0),
    # Generator port
    "gen_power":               (166, 1,    True,  0),
    "gen_voltage":             (181, 0.1,  False, 0),
    "gen_frequency":           (196, 0.01, False, 0),
    # Energy — today (kWh)
    "daily_solar":             (108, 0.1,  False, 0),
    "daily_grid_import":       (76,  0.1,  False, 0),
    "daily_grid_export":       (77,  0.1,  False, 0),
    "daily_battery_charge":    (70,  0.1,  False, 0),
    "daily_battery_discharge": (71,  0.1,  False, 0),
    "daily_load":              (84,  0.1,  False, 0),
    "daily_gen":               (62,  0.1,  False, 0),
    "daily_inverter_energy":   (60,  0.1,  False, 0),
    # Energy — month (kWh)
    "month_solar":             (65,  0.1,  False, 0),
    "month_load":              (66,  0.1,  False, 0),
    "month_grid_import":       (67,  0.1,  False, 0),
    # BMS (as the inverter sees it over CAN)
    "bms_charge_voltage":          (312, 0.01, False, 0),
    "bms_discharge_voltage":       (313, 0.01, False, 0),
    "bms_charge_current_limit":    (314, 1,    False, 0),
    "bms_discharge_current_limit": (315, 1,    False, 0),
    "bms_soc":                 (316, 1,    False, 0),
    "bms_voltage":             (317, 0.01, False, 0),
    "bms_current":             (318, 1,    True,  0),
    "bms_temp":                (319, 0.1,  False, 1000),
    "bms_alarm_code":          (322, 1,    False, 0),
    "bms_fault_code":          (323, 1,    False, 0),
}

# Settings, decoded from SP_SLOW_BLOCKS.
_SP_CONFIG_MAP: dict[str, tuple[int, float, bool, int]] = {
    "protocol_version":        (2,   1,    False, 0),
    "battery_absorption_voltage": (202, 0.01, False, 0),
    "battery_float_voltage":   (203, 0.01, False, 0),
    "battery_capacity":        (204, 1,    False, 0),     # Ah
    "battery_empty_voltage":   (205, 0.01, False, 0),
    "zero_export_power":       (206, 1,    False, 0),
    "max_charge_current":      (210, 1,    False, 0),
    "max_discharge_current":   (211, 1,    False, 0),
    "batt_shutdown_soc":       (217, 1,    False, 0),
    "batt_restart_soc":        (218, 1,    False, 0),
    "batt_low_soc":            (219, 1,    False, 0),
    "batt_shutdown_voltage":   (220, 0.01, False, 0),
    "batt_restart_voltage":    (221, 0.01, False, 0),
    "batt_low_voltage":        (222, 0.01, False, 0),
    "gen_charge_start_soc":    (226, 1,    False, 0),
    "gen_charge_current":      (227, 1,    False, 0),
    "grid_charge_start_soc":   (229, 1,    False, 0),
    "grid_charge_current":     (230, 1,    False, 0),
    "max_sell_power":          (245, 1,    False, 0),
    "grid_reconnect_time":     (282, 1,    False, 0),     # s
    "grid_voltage_high_limit": (287, 0.1,  False, 0),
    "grid_voltage_low_limit":  (288, 0.1,  False, 0),
    "grid_frequency_high_limit": (289, 0.01, False, 0),
    "grid_frequency_low_limit":  (290, 0.01, False, 0),
    "gen_peak_power":          (292, 1,    False, 0),
    "grid_peak_power":         (293, 1,    False, 0),
}

# 32-bit counters, low word first: (low register, high register, scale).
# Grid import skips 79 (that is grid frequency), so its high word is 80.
_SP_DECODE32: dict[str, tuple[int, int, float]] = {
    "total_solar":             (96, 97, 0.1),
    "total_grid_import":       (78, 80, 0.1),
    "total_grid_export":       (81, 82, 0.1),
    "total_battery_charge":    (72, 73, 0.1),
    "total_battery_discharge": (74, 75, 0.1),
    "total_consumption":       (85, 86, 0.1),
    "year_solar":              (68, 69, 0.1),
    "year_load":               (87, 88, 0.1),
}
_SP_CONFIG32: dict[str, tuple[int, int, float]] = {
    "rated_power":             (16, 17, 0.1),
}

SP_DEVICE_STATES: dict[int, str] = {
    0: "Standby", 1: "Self-test", 2: "Normal", 3: "Alarm", 4: "Fault",
}
SP_ENERGY_PATTERNS: dict[int, str] = {0: "Battery First", 1: "Load First"}
SP_BATTERY_MODES: dict[int, str] = {0: "Voltage", 1: "Capacity", 2: "No Battery"}
SP_IO_MODES: dict[int, str] = {0: "Generator", 1: "SmartLoad", 2: "Microinverter"}
SP_BMS_PROTOCOLS: dict[int, str] = {
    0: "PYLON", 1: "Tianbangda", 2: "KOK", 3: "Keith", 4: "Toppai", 5: "Peneng 485",
    6: "Jeris 485", 7: "Sunwoda 485", 8: "Xinrui 485", 9: "Tianbangda 485",
    10: "Shenggao Electric CAN",
}
# Alarm words 101-102 and fault words 103-106 as one bitfield each (bit -> text).
SP_ALARM_BITS: dict[int, str] = {
    1: "Fan failure", 2: "Grid phase failure", 3: "Meter communication failure",
    30: "Battery loss", 31: "Parallel communication quality",
}
SP_FAULT_BITS: dict[int, str] = {
    6: "DC/DC soft start failure", 9: "Auxiliary power supply failure",
    12: "Working mode changed", 17: "AC over-current", 18: "Tz_Integ_Fault",
    19: "DC over-current", 22: "AC current leakage", 63: "Temperature too high",
}

TOU_SLOTS = 6
REG_SP_TOU_ENABLE = 248   # bit 0 enable, bits 1-7 Monday..Sunday
REG_SP_TOU_TIME = 250     # 250-255 slot start, HHMM
REG_SP_TOU_POWER = 256    # 256-261 W
REG_SP_TOU_SOC = 268      # 268-273 %
REG_SP_TOU_CHARGE = 274   # 274-279 bit 0 grid, bit 1 generator
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _decode_scalar(words_by_reg, table, result) -> None:
    for key, (reg, scale, signed, offset) in table.items():
        raw = _lookup(words_by_reg, reg)
        if raw is None:
            continue
        value = ((_signed16(raw) if signed else raw) - offset) * scale
        result[key] = int(value) if (scale == 1 and offset == 0) else round(value, 2)


def _decode_32(words_by_reg, table, result) -> None:
    for key, (lo_reg, hi_reg, scale) in table.items():
        lo, hi = _lookup(words_by_reg, lo_reg), _lookup(words_by_reg, hi_reg)
        if lo is None or hi is None:
            continue
        result[key] = round(((hi << 16) | lo) * scale, 2)


def _words(words_by_reg, start: int, count: int) -> list[int] | None:
    words = [_lookup(words_by_reg, start + i) for i in range(count)]
    return None if any(w is None for w in words) else words


def _bit_texts(value: int, bits: dict[int, str]) -> str:
    """'OK' for 0, else the known bit texts (unknown bits as 'bit N')."""
    if value == 0:
        return "OK"
    return ", ".join(
        bits.get(i, f"bit {i}") for i in range(value.bit_length()) if value >> i & 1
    )


def _decode_words_bitfield(words: list[int]) -> int:
    """Little-word-first multi-register bitfield (ha-solarman rule 3)."""
    return sum(w << (16 * i) for i, w in enumerate(words))


def decode_single_phase(words_by_reg: dict[int, list[int]]) -> dict:
    """Decode a single-phase poll (any subset of the SP_* tier blocks).

    Same contract as :func:`decode`: keys whose registers were not polled are
    omitted, never guessed.
    """
    result: dict = {}
    _decode_scalar(words_by_reg, _SP_DECODE_MAP, result)
    _decode_scalar(words_by_reg, _SP_CONFIG_MAP, result)
    _decode_32(words_by_reg, _SP_DECODE32, result)
    _decode_32(words_by_reg, _SP_CONFIG32, result)

    if "pv1_power" in result and "pv2_power" in result:
        result["solar_power"] = result["pv1_power"] + result["pv2_power"]

    state = _lookup(words_by_reg, 59)
    if state is not None:
        result["device_state"] = SP_DEVICE_STATES.get(state, f"Unknown ({state})")

    alarm = _words(words_by_reg, 101, 2)
    if alarm is not None:
        code = _decode_words_bitfield(alarm)
        result["alarm_code"] = code
        result["alarm"] = _bit_texts(code, SP_ALARM_BITS)
    fault = _words(words_by_reg, 103, 4)
    if fault is not None:
        code = _decode_words_bitfield(fault)
        result["fault_code"] = code
        result["fault"] = _bit_texts(code, SP_FAULT_BITS)
    if state is not None and alarm is not None and fault is not None:
        result["inverter_problem"] = (
            state in (3, 4) or result["alarm_code"] != 0 or result["fault_code"] != 0
        )
    if "bms_alarm_code" in result and "bms_fault_code" in result:
        result["bms_problem"] = bool(result["bms_alarm_code"] or result["bms_fault_code"])

    relay = _lookup(words_by_reg, 194)
    if relay is not None:
        result["grid_connected"] = relay != 0

    sn = _words(words_by_reg, 3, 5)
    if sn is not None:
        text = bytes(b for w in sn for b in (w >> 8, w & 0xFF)).decode("ascii", "replace")
        result["inverter_serial"] = text.strip("\x00 ")
    fw = _words(words_by_reg, 10, 5)
    if fw is not None:
        # Same rendering as the Deye app: control board 13.12.11, comms board 14.10.
        result["firmware_control"] = ".".join(f"{w:04X}" for w in (fw[3], fw[2], fw[1]))
        result["firmware_comms"] = ".".join(f"{w:04X}" for w in (fw[4], fw[0]))
    mppt = _lookup(words_by_reg, 18)
    if mppt is not None:
        result["mppt_count"] = mppt >> 8
        result["phase_count"] = mppt & 0xFF

    clock_words = _words(words_by_reg, SP_REG_CLOCK, CLOCK_WORD_COUNT)
    if clock_words is not None:
        clock = decode_clock(clock_words)
        if clock is not None:
            result["inverter_clock"] = clock

    for key, reg, labels in (
        ("battery_mode", 213, SP_BATTERY_MODES),
        ("io_mode", 235, SP_IO_MODES),
        ("energy_pattern", 243, SP_ENERGY_PATTERNS),
        ("work_mode", 244, WORK_MODE_LABELS),
        ("bms_protocol", 325, SP_BMS_PROTOCOLS),
    ):
        raw = _lookup(words_by_reg, reg)
        if raw is not None:
            result[key] = labels.get(raw, f"Unknown ({raw})")

    for key, reg in (("gen_charge_enabled", 231), ("grid_charge_enabled", 232),
                     ("solar_sell_enabled", 247)):
        raw = _lookup(words_by_reg, reg)
        if raw is not None:
            result[key] = bool(raw)

    tou = _decode_tou(words_by_reg)
    if tou is not None:
        result.update(tou)

    return result


def _decode_tou(words_by_reg) -> dict | None:
    enable = _lookup(words_by_reg, REG_SP_TOU_ENABLE)
    times = _words(words_by_reg, REG_SP_TOU_TIME, TOU_SLOTS)
    power = _words(words_by_reg, REG_SP_TOU_POWER, TOU_SLOTS)
    soc = _words(words_by_reg, REG_SP_TOU_SOC, TOU_SLOTS)
    charge = _words(words_by_reg, REG_SP_TOU_CHARGE, TOU_SLOTS)
    if None in (enable, times, power, soc, charge):
        return None
    slots = []
    for i in range(TOU_SLOTS):
        try:
            start = decode_hhmm(times[i])
            end = decode_hhmm(times[(i + 1) % TOU_SLOTS])
        except ValueError:
            return None
        slots.append({
            "start": start, "end": end, "power": power[i], "soc": soc[i],
            "grid_charge": bool(charge[i] & 1), "gen_charge": bool(charge[i] & 2),
        })
    return {
        "tou_enabled": bool(enable & 1),
        "tou_days": [d for i, d in enumerate(_WEEKDAYS) if enable >> (i + 1) & 1],
        "tou_slots": slots,
    }


def active_tou_slot(slots: list[dict], hhmm: str) -> int | None:
    """Index of the TOU slot covering wall-clock ``'HH:MM'`` (slots wrap at midnight)."""
    for i, slot in enumerate(slots):
        start, end = slot["start"], slot["end"]
        if (start <= hhmm < end) if start < end else (hhmm >= start or hhmm < end):
            return i
    return None


# --- Profiles ---------------------------------------------------------------

TIER_LIVE = "live"
TIER_FAST = "fast"
TIER_SLOW = "slow"
ALL_TIERS = frozenset((TIER_LIVE, TIER_FAST, TIER_SLOW))

SP_IO_MODE_IDS = {v: k for k, v in SP_IO_MODES.items()}
SP_ENERGY_PATTERN_IDS = {v: k for k, v in SP_ENERGY_PATTERNS.items()}


@dataclass(frozen=True)
class RegisterProfile:
    """Everything that differs between the three-phase and single-phase maps.

    LIVE and FAST reads are fatal on failure (the poll failed); SLOW reads are
    best-effort, and the slow tier only counts as done when every slow block
    was read. ``controls`` maps a coordinator data key to its holding
    register; a control entity exists only where its key is listed.
    """

    live: list[tuple[int, int]]
    fast: list[tuple[int, int]]
    slow: list[tuple[int, int]]
    decode: Callable[[dict[int, list[int]]], dict]
    controls: dict[str, int]
    clock_reg: int
    # None where the profile has no Time Sync commit edge to drive.
    time_sync_reg: int | None


PROFILES: dict[str, RegisterProfile] = {
    PROFILE_THREE_PHASE: RegisterProfile(
        live=LIVE_BLOCKS,
        fast=FAST_BLOCKS,
        slow=SLOW_BLOCKS,
        decode=decode,
        controls={
            "work_mode": REG_WORK_MODE,
            "max_sell_power": REG_MAX_SELL_POWER,
            "zero_export_power": REG_ZERO_EXPORT_POWER,
            "charge_soc": REG_CHARGE_SOC,
            "max_charge_current": REG_MAX_CHARGE_CURRENT,
            "max_discharge_current": REG_MAX_DISCHARGE_CURRENT,
            "batt_shutdown_soc": REG_BATT_SHUTDOWN_SOC,
            "batt_restart_soc": REG_BATT_RESTART_SOC,
            "batt_low_soc": REG_BATT_LOW_SOC,
            "grid_peak_power": REG_GRID_PEAK_POWER,
            "gen_peak_power": REG_GEN_PEAK_POWER,
        },
        clock_reg=REG_CLOCK,
        time_sync_reg=REG_TIME_SYNC,
    ),
    PROFILE_SINGLE_PHASE: RegisterProfile(
        live=SP_LIVE_BLOCKS,
        fast=SP_FAST_BLOCKS,
        slow=SP_SLOW_BLOCKS,
        decode=decode_single_phase,
        controls={
            "zero_export_power": 206,
            "max_charge_current": 210,
            "max_discharge_current": 211,
            "batt_shutdown_soc": 217,
            "batt_restart_soc": 218,
            "batt_low_soc": 219,
            "grid_charge_current": 230,
            "grid_charge_enabled": 232,
            "io_mode": 235,
            "energy_pattern": 243,
            "work_mode": 244,
            "max_sell_power": 245,
            "solar_sell_enabled": 247,
            "gen_peak_power": 292,
            "grid_peak_power": 293,
        },
        clock_reg=SP_REG_CLOCK,
        # Unverified on single-phase: a clock sync there writes only the clock
        # block, never the three-phase Time Sync address.
        time_sync_reg=None,
    ),
}


def tier_blocks(profile: RegisterProfile, tiers) -> list[tuple[tuple[int, int], bool]]:
    """(block, fatal) pairs for *tiers*, in LIVE, FAST, SLOW order."""
    out = []
    for tier, blocks in ((TIER_LIVE, profile.live), (TIER_FAST, profile.fast), (TIER_SLOW, profile.slow)):
        if tier in tiers:
            out += [(block, tier != TIER_SLOW) for block in blocks]
    return out


# --- Encoders ---------------------------------------------------------------

def decode_clock(words: list[int]) -> datetime | None:
    """Decode the three packed RTC registers to a naive local ``datetime``.

    The inverter stores a bare wall clock with no timezone, so the result is
    deliberately naive — attaching a timezone below the entity layer would
    invent information the device never reported.

    Returns ``None`` when the frame does not describe a real date/time (the
    ``datetime`` constructor rejects month 13, day 32, hour 24, and friends).
    """
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
    """Encode a local ``datetime`` to the three packed RTC registers.

    Inverse of :func:`decode_clock`. Only the wall-clock fields are used, so an
    aware value encodes as the site time it displays — the inverter has no
    timezone to convert into. The words are staged in the registers; committing
    them is the Time Sync edge's job (see REG_TIME_SYNC).
    """
    if not 2000 <= value.year <= 2255:
        raise ValueError(f"year out of register range: {value.year}")
    return [
        ((value.year - 2000) << 8) | value.month,
        (value.day << 8) | value.hour,
        (value.minute << 8) | value.second,
    ]


def encode_work_mode(label: str) -> int:
    """Encode a work-mode label to its register value (0, 1, or 2)."""
    return _WORK_MODE_IDS[label]


def encode_hhmm(time_str: str) -> int:
    """Encode ``'HH:MM'`` to decimal HHMM for a TOU time register.

    Example: ``'14:00'`` -> ``1400`` (``0x0578``). Rejects malformed or
    out-of-range times.
    """
    parts = time_str.split(":")
    if len(parts) != 2 or not all(len(p) == 2 and p.isdigit() for p in parts):
        raise ValueError(f"expected canonical 'HH:MM', got {time_str!r}")
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"time out of range: {time_str!r}")
    return hour * 100 + minute


def decode_hhmm(raw: int) -> str:
    """Decode a TOU time register value to ``'HH:MM'``.

    Example: ``0x05DC`` (1500) -> ``'15:00'``. Rejects out-of-range raw values.
    """
    hour, minute = divmod(raw, 100)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"invalid HHMM register value: {raw}")
    return f"{hour:02d}:{minute:02d}"
