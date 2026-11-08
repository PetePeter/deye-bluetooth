"""Single-phase LP1 register map and decoder."""
from __future__ import annotations

from ..codec import (
    _decode_32, _decode_scalar, _lookup, _words, bitfield_text, clock as _clock,
    decode_hhmm, firmware, serial, split_bytes,
)
from .three_phase_hybrid import WORK_MODE_LABELS


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
    # Signed: load minus grid import, so it falls while the grid charges the
    # battery. The month (65-68) and year (87-88) words undercount and glitch
    # at rollover, so they are not decoded (PR #8 review, SUN-8K-SG05LP1).
    "daily_inverter_energy":   (60,  0.1,  True,  0),
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
    "smartload_off_soc":       (237, 1,    False, 0),
    "smartload_on_soc":        (239, 1,    False, 0),
    "smartload_min_solar_power": (241, 1,  False, 0),
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
_SP_DECODE32: dict[str, tuple] = {
    "total_solar":             (96, 97, 0.1),
    "total_grid_import":       (78, 80, 0.1),
    "total_grid_export":       (81, 82, 0.1),
    "total_battery_charge":    (72, 73, 0.1),
    "total_battery_discharge": (74, 75, 0.1),
    "total_consumption":       (85, 86, 0.1),
    "total_inverter_energy":   (63, 64, 0.1, True),  # signed, like 60
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

    alarm = bitfield_text(words_by_reg, 101, 2, SP_ALARM_BITS)
    if alarm is not None:
        result["alarm_code"], result["alarm"] = alarm
    fault = bitfield_text(words_by_reg, 103, 4, SP_FAULT_BITS)
    if fault is not None:
        result["fault_code"], result["fault"] = fault
    if state is not None and alarm is not None and fault is not None:
        result["inverter_problem"] = (
            state in (3, 4) or result["alarm_code"] != 0 or result["fault_code"] != 0
        )
    if "bms_alarm_code" in result and "bms_fault_code" in result:
        result["bms_problem"] = bool(result["bms_alarm_code"] or result["bms_fault_code"])

    relay = _lookup(words_by_reg, 194)
    if relay is not None:
        result["grid_connected"] = relay != 0

    inverter_serial = serial(words_by_reg, 3)
    if inverter_serial is not None:
        result["inverter_serial"] = inverter_serial
    firmware_versions = firmware(words_by_reg, 10)
    if firmware_versions is not None:
        # Same rendering as the Deye app: control board 13.12.11, comms board 14.10.
        result["firmware_control"], result["firmware_comms"] = firmware_versions
    mppt = _lookup(words_by_reg, 18)
    if mppt is not None:
        result["mppt_count"], result["phase_count"] = split_bytes(mppt)

    inverter_clock = _clock(words_by_reg, SP_REG_CLOCK)
    if inverter_clock is not None:
        result["inverter_clock"] = inverter_clock

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



__all__ = [
    "SP_LIVE_BLOCKS", "SP_FAST_BLOCKS", "SP_SLOW_BLOCKS", "SP_REG_CLOCK",
    "SP_DEVICE_STATES", "SP_ENERGY_PATTERNS", "SP_BATTERY_MODES", "SP_IO_MODES",
    "SP_BMS_PROTOCOLS", "SP_ALARM_BITS", "SP_FAULT_BITS", "TOU_SLOTS",
    "REG_SP_TOU_ENABLE", "REG_SP_TOU_TIME", "REG_SP_TOU_POWER", "REG_SP_TOU_SOC",
    "REG_SP_TOU_CHARGE", "decode_single_phase", "active_tou_slot", "_SP_DECODE_MAP",
    "_SP_CONFIG_MAP", "_SP_DECODE32", "_SP_CONFIG32", "_WEEKDAYS", "_decode_tou",
]
