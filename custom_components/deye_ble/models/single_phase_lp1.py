"""Single-phase LP1 register map and decoder."""
from __future__ import annotations

from ..codec import (
    _lookup, _words, bitfield_text, clock as _clock, decode_hhmm, firmware,
    serial, split_bytes,
)

from .base import (
    BinarySensor, Capabilities, Model, Number, Point, Read, Select, Sensor,
    Switch, Write, pair_read, scalar_read,
)
from .three_phase_hybrid import WORK_MODE_LABELS


SP_REG_CLOCK = 22  # 22-24, packed like the three-phase 0x003E-0x0040

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
    "REG_SP_TOU_CHARGE", "decode_single_phase", "active_tou_slot",
]


def _mapped_value(words, register, labels):
    raw = _lookup(words, register)
    return None if raw is None else labels.get(raw, f"Unknown ({raw})")


def _bool_value(words, register):
    raw = _lookup(words, register)
    return None if raw is None else bool(raw)


def _bitfield_part(words, start, count, labels, index):
    decoded = bitfield_text(words, start, count, labels)
    return None if decoded is None else decoded[index]


def _byte_value(words, register, index):
    raw = _lookup(words, register)
    return None if raw is None else split_bytes(raw)[index]


def _firmware_part(words, start, index):
    decoded = firmware(words, start)
    return None if decoded is None else decoded[index]


def _sum_if_present(values, left, right):
    if left not in values or right not in values:
        return None
    return values[left] + values[right]


def _inverter_problem(values):
    if not all(key in values for key in ("device_state", "alarm_code", "fault_code")):
        return None
    return values["device_state"] in ("Alarm", "Fault") or bool(
        values["alarm_code"] or values["fault_code"]
    )


def _bms_problem(values):
    if not all(key in values for key in ("bms_alarm_code", "bms_fault_code")):
        return None
    return bool(values["bms_alarm_code"] or values["bms_fault_code"])


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


def _tou_value(words, key):
    decoded = _decode_tou(words)
    return None if decoded is None else decoded.get(key)


POINTS = (
    Point("battery_soc", read=scalar_read(184, factor=1, signed=False, offset=0), show=Sensor("Battery SOC", unit="%", device_class="battery", state_class="measurement", precision=1), tier="live"),
    Point("battery_voltage", read=scalar_read(183, factor=0.01, signed=False, offset=0), show=Sensor("Battery Voltage", unit="V", device_class="voltage", state_class="measurement", precision=2), tier="live"),
    Point("battery_current", read=scalar_read(191, factor=0.01, signed=True, offset=0), show=Sensor("Battery Current", unit="A", device_class="current", state_class="measurement", precision=2), tier="live"),
    Point("battery_power", read=scalar_read(190, factor=1, signed=True, offset=0), show=Sensor("Battery Power", unit="W", device_class="power", state_class="measurement", icon="mdi:battery-charging", precision=0), tier="live"),
    Point("battery_temp", read=scalar_read(182, factor=0.1, signed=False, offset=1000), show=Sensor("Battery Temperature", unit="°C", device_class="temperature", state_class="measurement", precision=1), tier="live"),
    Point("inverter_temp", read=scalar_read(90, factor=0.1, signed=False, offset=1000), show=Sensor("Inverter Temperature", unit="°C", device_class="temperature", state_class="measurement", precision=1), tier="fast"),
    Point("inverter_ac_temp", read=scalar_read(91, factor=0.1, signed=False, offset=1000), show=Sensor("Inverter AC Temperature", unit="°C", device_class="temperature", state_class="measurement", precision=1), tier="fast"),
    Point("inverter_output_power", read=scalar_read(175, factor=1, signed=True, offset=0), show=Sensor("Inverter Output", unit="W", device_class="power", state_class="measurement", icon="mdi:lightning-bolt", precision=0), tier="live"),
    Point("inverter_output_voltage", read=scalar_read(154, factor=0.1, signed=False, offset=0), show=Sensor("Inverter Output Voltage", unit="V", device_class="voltage", state_class="measurement", precision=1), tier="live"),
    Point("inverter_output_current", read=scalar_read(164, factor=0.01, signed=True, offset=0), show=Sensor("Inverter Output Current", unit="A", device_class="current", state_class="measurement", precision=2), tier="live"),
    Point("inverter_output_frequency", read=scalar_read(193, factor=0.01, signed=False, offset=0), show=Sensor("Inverter Output Frequency", unit="Hz", device_class="frequency", state_class="measurement", precision=2), tier="live"),
    Point("pv1_power", read=scalar_read(186, factor=1, signed=False, offset=0), show=Sensor("PV1 Power", unit="W", device_class="power", state_class="measurement", icon="mdi:solar-panel", precision=0), tier="live"),
    Point("pv2_power", read=scalar_read(187, factor=1, signed=False, offset=0), show=Sensor("PV2 Power", unit="W", device_class="power", state_class="measurement", icon="mdi:solar-panel", precision=0), tier="live"),
    Point("pv1_voltage", read=scalar_read(109, factor=0.1, signed=False, offset=0), show=Sensor("PV1 Voltage", unit="V", device_class="voltage", state_class="measurement", icon="mdi:solar-panel", precision=1), tier="fast"),
    Point("pv1_current", read=scalar_read(110, factor=0.1, signed=False, offset=0), show=Sensor("PV1 Current", unit="A", device_class="current", state_class="measurement", icon="mdi:solar-panel", precision=1), tier="fast"),
    Point("pv2_voltage", read=scalar_read(111, factor=0.1, signed=False, offset=0), show=Sensor("PV2 Voltage", unit="V", device_class="voltage", state_class="measurement", icon="mdi:solar-panel", precision=1), tier="fast"),
    Point("pv2_current", read=scalar_read(112, factor=0.1, signed=False, offset=0), show=Sensor("PV2 Current", unit="A", device_class="current", state_class="measurement", icon="mdi:solar-panel", precision=1), tier="fast"),
    Point("grid_power", read=scalar_read(169, factor=1, signed=True, offset=0), show=Sensor("Grid Power", unit="W", device_class="power", state_class="measurement", icon="mdi:transmission-tower", precision=0), tier="live"),
    Point("grid_inverter_side_power", read=scalar_read(167, factor=1, signed=True, offset=0), show=Sensor("Grid Power (Inverter Side)", unit="W", device_class="power", state_class="measurement", icon="mdi:transmission-tower", precision=0), tier="live"),
    Point("grid_voltage", read=scalar_read(150, factor=0.1, signed=False, offset=0), show=Sensor("Grid Voltage", unit="V", device_class="voltage", state_class="measurement", precision=1), tier="live"),
    Point("grid_current", read=scalar_read(160, factor=0.01, signed=True, offset=0), show=Sensor("Grid Current", unit="A", device_class="current", state_class="measurement", precision=2), tier="live"),
    Point("grid_frequency", read=scalar_read(79, factor=0.01, signed=False, offset=0), show=Sensor("Grid Frequency", unit="Hz", device_class="frequency", state_class="measurement", precision=2), tier="fast"),
    Point("external_ct_current", read=scalar_read(162, factor=0.01, signed=True, offset=0), show=Sensor("External CT Current", unit="A", device_class="current", state_class="measurement", precision=2), tier="live"),
    Point("external_ct_power", read=scalar_read(170, factor=1, signed=True, offset=0), show=Sensor("External CT Power", unit="W", device_class="power", state_class="measurement", precision=0), tier="live"),
    Point("house_load", read=scalar_read(178, factor=1, signed=True, offset=0), show=Sensor("House Load", unit="W", device_class="power", state_class="measurement", icon="mdi:home-lightning-bolt", precision=0), tier="live"),
    Point("ups_power", read=scalar_read(176, factor=1, signed=True, offset=0), show=Sensor("UPS Power", unit="W", device_class="power", state_class="measurement", icon="mdi:power-plug", precision=0), tier="live"),
    Point("load_voltage", read=scalar_read(157, factor=0.1, signed=False, offset=0), show=Sensor("Load Voltage", unit="V", device_class="voltage", state_class="measurement", precision=1), tier="live"),
    Point("load_frequency", read=scalar_read(192, factor=0.01, signed=False, offset=0), show=Sensor("Load Frequency", unit="Hz", device_class="frequency", state_class="measurement", precision=2), tier="live"),
    Point("gen_power", read=scalar_read(166, factor=1, signed=True, offset=0), show=Sensor("Generator Power", unit="W", device_class="power", state_class="measurement", icon="mdi:engine", precision=0), tier="live"),
    Point("gen_voltage", read=scalar_read(181, factor=0.1, signed=False, offset=0), show=Sensor("Generator Voltage", unit="V", device_class="voltage", state_class="measurement", icon="mdi:engine", precision=1), tier="live"),
    Point("gen_frequency", read=scalar_read(196, factor=0.01, signed=False, offset=0), show=Sensor("Generator Frequency", unit="Hz", device_class="frequency", state_class="measurement", icon="mdi:engine", precision=2), tier="live"),
    Point("daily_solar", read=scalar_read(108, factor=0.1, signed=False, offset=0), show=Sensor("Solar Today", unit="kWh", device_class="energy", state_class="total_increasing", icon="mdi:solar-power", precision=2), tier="fast"),
    Point("daily_grid_import", read=scalar_read(76, factor=0.1, signed=False, offset=0), show=Sensor("Grid Import Today", unit="kWh", device_class="energy", state_class="total_increasing", icon="mdi:transmission-tower-import", precision=2), tier="fast"),
    Point("daily_grid_export", read=scalar_read(77, factor=0.1, signed=False, offset=0), show=Sensor("Grid Export Today", unit="kWh", device_class="energy", state_class="total_increasing", icon="mdi:transmission-tower-export", precision=2), tier="fast"),
    Point("daily_battery_charge", read=scalar_read(70, factor=0.1, signed=False, offset=0), show=Sensor("Battery Charge Today", unit="kWh", device_class="energy", state_class="total_increasing", precision=2), tier="fast"),
    Point("daily_battery_discharge", read=scalar_read(71, factor=0.1, signed=False, offset=0), show=Sensor("Battery Discharge Today", unit="kWh", device_class="energy", state_class="total_increasing", precision=2), tier="fast"),
    Point("daily_load", read=scalar_read(84, factor=0.1, signed=False, offset=0), show=Sensor("Consumption Today", unit="kWh", device_class="energy", state_class="total_increasing", icon="mdi:home-lightning-bolt", precision=2), tier="fast"),
    Point("daily_gen", read=scalar_read(62, factor=0.1, signed=False, offset=0), show=Sensor("Generator Today", unit="kWh", device_class="energy", state_class="total_increasing", icon="mdi:engine", precision=2), tier="fast"),
    Point("daily_inverter_energy", read=scalar_read(60, factor=0.1, signed=True, offset=0), show=Sensor("Inverter Energy Today", unit="kWh", device_class="energy", state_class="total", precision=2), tier="fast"),
    Point("bms_charge_voltage", read=scalar_read(312, factor=0.01, signed=False, offset=0), show=Sensor("BMS Charge Voltage", unit="V", device_class="voltage", state_class="measurement", precision=2), tier="fast"),
    Point("bms_discharge_voltage", read=scalar_read(313, factor=0.01, signed=False, offset=0), show=Sensor("BMS Discharge Voltage", unit="V", device_class="voltage", state_class="measurement", precision=2), tier="fast"),
    Point("bms_charge_current_limit", read=scalar_read(314, factor=1, signed=False, offset=0), show=Sensor("BMS Charge Current Limit", unit="A", device_class="current", state_class="measurement", precision=0), tier="fast"),
    Point("bms_discharge_current_limit", read=scalar_read(315, factor=1, signed=False, offset=0), show=Sensor("BMS Discharge Current Limit", unit="A", device_class="current", state_class="measurement", precision=0), tier="fast"),
    Point("bms_soc", read=scalar_read(316, factor=1, signed=False, offset=0), show=Sensor("BMS SOC", unit="%", device_class="battery", state_class="measurement", precision=0), tier="fast"),
    Point("bms_voltage", read=scalar_read(317, factor=0.01, signed=False, offset=0), show=Sensor("BMS Voltage", unit="V", device_class="voltage", state_class="measurement", precision=2), tier="fast"),
    Point("bms_current", read=scalar_read(318, factor=1, signed=True, offset=0), show=Sensor("BMS Current", unit="A", device_class="current", state_class="measurement", precision=0), tier="fast"),
    Point("bms_temp", read=scalar_read(319, factor=0.1, signed=False, offset=1000), show=Sensor("BMS Temperature", unit="°C", device_class="temperature", state_class="measurement", precision=1), tier="fast"),
    Point("bms_alarm_code", read=scalar_read(322, factor=1, signed=False, offset=0), show=Sensor("BMS Alarm Code", entity_category="diagnostic"), tier="fast"),
    Point("bms_fault_code", read=scalar_read(323, factor=1, signed=False, offset=0), show=Sensor("BMS Fault Code", entity_category="diagnostic"), tier="fast"),
    Point("protocol_version", read=scalar_read(2, factor=1, signed=False, offset=0), show=Sensor("Modbus Protocol", entity_category="diagnostic"), tier="slow"),
    Point("battery_absorption_voltage", read=scalar_read(202, factor=0.01, signed=False, offset=0), show=Sensor("Absorption Voltage", unit="V", device_class="voltage", precision=2, entity_category="diagnostic"), tier="slow"),
    Point("battery_float_voltage", read=scalar_read(203, factor=0.01, signed=False, offset=0), show=Sensor("Float Voltage", unit="V", device_class="voltage", precision=2, entity_category="diagnostic"), tier="slow"),
    Point("battery_capacity", read=scalar_read(204, factor=1, signed=False, offset=0), show=Sensor("Battery Capacity", unit="Ah", icon="mdi:battery", precision=0, entity_category="diagnostic"), tier="slow"),
    Point("battery_empty_voltage", read=scalar_read(205, factor=0.01, signed=False, offset=0), show=Sensor("Empty Voltage", unit="V", device_class="voltage", precision=2, entity_category="diagnostic"), tier="slow"),
    Point("zero_export_power", read=scalar_read(206, factor=1, signed=False, offset=0), write=Write((206,), "SUN-8K-SG05LP1-EU-SM2", lambda value, _data: int(value), reassertable=True), show=Number("Zero Export Power", unit="W", minimum=0, maximum=100, step=1, icon="mdi:transmission-tower-import", device_class="power", mode="box"), tier="slow"),
    Point("max_charge_current", read=scalar_read(210, factor=1, signed=False, offset=0), write=Write((210,), "SUN-8K-SG05LP1-EU-SM2", lambda value, _data: int(value), reassertable=True), show=Number("Max Charge Current", unit="A", minimum=0, maximum=280, step=1, icon="mdi:battery-arrow-up", device_class="current", mode="box"), tier="slow"),
    Point("max_discharge_current", read=scalar_read(211, factor=1, signed=False, offset=0), write=Write((211,), "SUN-8K-SG05LP1-EU-SM2", lambda value, _data: int(value), reassertable=True), show=Number("Max Discharge Current", unit="A", minimum=0, maximum=280, step=1, icon="mdi:battery-arrow-down", device_class="current", mode="box"), tier="slow"),
    Point("batt_shutdown_soc", read=scalar_read(217, factor=1, signed=False, offset=0), write=Write((217,), "SUN-8K-SG05LP1-EU-SM2", lambda value, _data: int(value), reassertable=True), show=Number("Battery Shutdown SOC", unit="%", minimum=0, maximum=100, step=1, icon="mdi:battery-off", device_class=None, mode="box"), tier="slow"),
    Point("batt_restart_soc", read=scalar_read(218, factor=1, signed=False, offset=0), write=Write((218,), "SUN-8K-SG05LP1-EU-SM2", lambda value, _data: int(value), reassertable=True), show=Number("Battery Restart SOC", unit="%", minimum=0, maximum=100, step=1, icon="mdi:battery-heart-variant", device_class=None, mode="box"), tier="slow"),
    Point("batt_low_soc", read=scalar_read(219, factor=1, signed=False, offset=0), write=Write((219,), "SUN-8K-SG05LP1-EU-SM2", lambda value, _data: int(value), reassertable=True), show=Number("Battery Low SOC", unit="%", minimum=0, maximum=100, step=1, icon="mdi:battery-alert", device_class=None, mode="box"), tier="slow"),
    Point("batt_shutdown_voltage", read=scalar_read(220, factor=0.01, signed=False, offset=0), show=Sensor("Shutdown Voltage", unit="V", device_class="voltage", precision=2, entity_category="diagnostic"), tier="slow"),
    Point("batt_restart_voltage", read=scalar_read(221, factor=0.01, signed=False, offset=0), show=Sensor("Restart Voltage", unit="V", device_class="voltage", precision=2, entity_category="diagnostic"), tier="slow"),
    Point("batt_low_voltage", read=scalar_read(222, factor=0.01, signed=False, offset=0), show=Sensor("Low Voltage", unit="V", device_class="voltage", precision=2, entity_category="diagnostic"), tier="slow"),
    Point("gen_charge_start_soc", read=scalar_read(226, factor=1, signed=False, offset=0), show=Sensor("Generator Charge Start SOC", unit="%", icon="mdi:engine", precision=0, entity_category="diagnostic"), tier="slow"),
    Point("gen_charge_current", read=scalar_read(227, factor=1, signed=False, offset=0), show=Sensor("Generator Charge Current", unit="A", device_class="current", icon="mdi:engine", precision=0, entity_category="diagnostic"), tier="slow"),
    Point("grid_charge_start_soc", read=scalar_read(229, factor=1, signed=False, offset=0), show=Sensor("Grid Charge Start SOC", unit="%", icon="mdi:transmission-tower-import", precision=0, entity_category="diagnostic"), tier="slow"),
    Point("grid_charge_current", read=scalar_read(230, factor=1, signed=False, offset=0), write=Write((230,), "SUN-8K-SG05LP1-EU-SM2", lambda value, _data: int(value), reassertable=True), show=Number("Grid Charge Current", unit="A", minimum=0, maximum=280, step=1, icon="mdi:transmission-tower-import", device_class="current", mode="box"), tier="slow"),
    Point("smartload_off_soc", read=scalar_read(237, factor=1, signed=False, offset=0), write=Write((237,), "SUN-8K-SG05LP1-EU-SM2", lambda value, _data: int(value), reassertable=True), show=Number("SmartLoad OFF Battery SOC", unit="%", minimum=0, maximum=100, step=1, icon="mdi:battery-arrow-down", device_class=None, mode="box"), tier="slow"),
    Point("smartload_on_soc", read=scalar_read(239, factor=1, signed=False, offset=0), write=Write((239,), "SUN-8K-SG05LP1-EU-SM2", lambda value, _data: int(value), reassertable=True), show=Number("SmartLoad ON Battery SOC", unit="%", minimum=0, maximum=100, step=1, icon="mdi:battery-arrow-up", device_class=None, mode="box"), tier="slow"),
    Point("smartload_min_solar_power", read=scalar_read(241, factor=1, signed=False, offset=0), write=Write((241,), "SUN-8K-SG05LP1-EU-SM2", lambda value, _data: int(value), reassertable=True), show=Number("SmartLoad Min Solar Power", unit="W", minimum=0, maximum=20000, step=50, icon="mdi:solar-power", device_class="power", mode="box"), tier="slow"),
    Point("max_sell_power", read=scalar_read(245, factor=1, signed=False, offset=0), write=Write((245,), "SUN-8K-SG05LP1-EU-SM2", lambda value, _data: int(value), reassertable=True), show=Number("Max Sell Power", unit="W", minimum=0, maximum=10000, step=1, icon="mdi:transmission-tower-export", device_class="power", mode="box"), tier="slow"),
    Point("grid_reconnect_time", read=scalar_read(282, factor=1, signed=False, offset=0), show=Sensor("Grid Reconnect Time", unit="s", device_class="duration", precision=0, entity_category="diagnostic"), tier="slow"),
    Point("grid_voltage_high_limit", read=scalar_read(287, factor=0.1, signed=False, offset=0), show=Sensor("Grid Voltage High Limit", unit="V", device_class="voltage", precision=1, entity_category="diagnostic"), tier="slow"),
    Point("grid_voltage_low_limit", read=scalar_read(288, factor=0.1, signed=False, offset=0), show=Sensor("Grid Voltage Low Limit", unit="V", device_class="voltage", precision=1, entity_category="diagnostic"), tier="slow"),
    Point("grid_frequency_high_limit", read=scalar_read(289, factor=0.01, signed=False, offset=0), show=Sensor("Grid Frequency High Limit", unit="Hz", device_class="frequency", precision=2, entity_category="diagnostic"), tier="slow"),
    Point("grid_frequency_low_limit", read=scalar_read(290, factor=0.01, signed=False, offset=0), show=Sensor("Grid Frequency Low Limit", unit="Hz", device_class="frequency", precision=2, entity_category="diagnostic"), tier="slow"),
    Point("gen_peak_power", read=scalar_read(292, factor=1, signed=False, offset=0), write=Write((292,), "SUN-8K-SG05LP1-EU-SM2", lambda value, _data: int(value), reassertable=True), show=Number("Gen Peak Shave Power", unit="W", minimum=0, maximum=30000, step=100, icon="mdi:engine", device_class="power", mode="box"), tier="slow"),
    Point("grid_peak_power", read=scalar_read(293, factor=1, signed=False, offset=0), write=Write((293,), "SUN-8K-SG05LP1-EU-SM2", lambda value, _data: int(value), reassertable=True), show=Number("Grid Peak Shave Power", unit="W", minimum=0, maximum=30000, step=100, icon="mdi:transmission-tower", device_class="power", mode="box"), tier="slow"),
    Point("total_solar", read=pair_read(96, 97, factor=0.1, signed=False), show=Sensor("Solar Total", unit="kWh", device_class="energy", state_class="total_increasing", precision=2), tier="fast"),
    Point("total_grid_import", read=pair_read(78, 80, factor=0.1, signed=False), show=Sensor("Grid Import Total", unit="kWh", device_class="energy", state_class="total_increasing", icon="mdi:transmission-tower-import", precision=2), tier="fast"),
    Point("total_grid_export", read=pair_read(81, 82, factor=0.1, signed=False), show=Sensor("Grid Export Total", unit="kWh", device_class="energy", state_class="total_increasing", icon="mdi:transmission-tower-export", precision=2), tier="fast"),
    Point("total_battery_charge", read=pair_read(72, 73, factor=0.1, signed=False), show=Sensor("Battery Charge Total", unit="kWh", device_class="energy", state_class="total_increasing", precision=2), tier="fast"),
    Point("total_battery_discharge", read=pair_read(74, 75, factor=0.1, signed=False), show=Sensor("Battery Discharge Total", unit="kWh", device_class="energy", state_class="total_increasing", precision=2), tier="fast"),
    Point("total_consumption", read=pair_read(85, 86, factor=0.1, signed=False), show=Sensor("Consumption Total", unit="kWh", device_class="energy", state_class="total_increasing", icon="mdi:home-lightning-bolt", precision=2), tier="fast"),
    Point("total_inverter_energy", read=pair_read(63, 64, factor=0.1, signed=True), show=Sensor("Inverter Energy Total", unit="kWh", device_class="energy", state_class="total", precision=2), tier="fast"),
    Point("rated_power", read=pair_read(16, 17, factor=0.1, signed=False), show=Sensor("Rated Power", unit="W", device_class="power", precision=0, entity_category="diagnostic"), tier="slow"),
    Point("grid_charge_enabled", read=Read((232,), lambda words: _bool_value(words, 232)), write=Write((232,), "SUN-8K-SG05LP1-EU-SM2", lambda value, _data: int(value), reassertable=True), show=Switch("Grid Charging", icon="mdi:transmission-tower-import"), tier="slow"),
    Point("io_mode", read=Read((235,), lambda words: _mapped_value(words, 235, SP_IO_MODES)), write=Write((235,), "SUN-8K-SG05LP1-EU-SM2", lambda value, _data: next(raw for raw, label in SP_IO_MODES.items() if label == value), reassertable=True), show=Select("GEN Port Mode", SP_IO_MODES, icon="mdi:engine"), tier="slow"),
    Point("energy_pattern", read=Read((243,), lambda words: _mapped_value(words, 243, SP_ENERGY_PATTERNS)), write=Write((243,), "SUN-8K-SG05LP1-EU-SM2", lambda value, _data: next(raw for raw, label in SP_ENERGY_PATTERNS.items() if label == value), reassertable=True), show=Select("Energy Pattern", SP_ENERGY_PATTERNS, icon="mdi:cog"), tier="slow"),
    Point("work_mode", read=Read((244,), lambda words: _mapped_value(words, 244, WORK_MODE_LABELS)), write=Write((244,), "SUN-8K-SG05LP1-EU-SM2", lambda value, _data: next(raw for raw, label in WORK_MODE_LABELS.items() if label == value), reassertable=True), show=Select("Work Mode", WORK_MODE_LABELS, icon="mdi:solar-power-variant"), tier="slow"),
    Point("solar_sell_enabled", read=Read((247,), lambda words: _bool_value(words, 247)), write=Write((247,), "SUN-8K-SG05LP1-EU-SM2", lambda value, _data: int(value), reassertable=True), show=Switch("Solar Sell", icon="mdi:transmission-tower-export"), tier="slow"),
    Point("solar_power", show=Sensor("Solar Power", unit="W", device_class="power", state_class="measurement", icon="mdi:solar-power", precision=0), compute=lambda values: _sum_if_present(values, 'pv1_power', 'pv2_power')),
    Point("device_state", read=Read((59,), lambda words: _mapped_value(words, 59, SP_DEVICE_STATES)), show=Sensor("Inverter State", icon="mdi:state-machine"), tier="fast"),
    Point("alarm_code", read=Read(tuple(range(101, 101+2)), lambda words: _bitfield_part(words, 101, 2, SP_ALARM_BITS, 0)), show=Sensor("Alarm Code", entity_category="diagnostic"), tier="fast"),
    Point("alarm", read=Read(tuple(range(101, 101+2)), lambda words: _bitfield_part(words, 101, 2, SP_ALARM_BITS, 1)), show=Sensor("Inverter Alarm", icon="mdi:alert-outline"), tier="fast"),
    Point("fault_code", read=Read(tuple(range(103, 103+4)), lambda words: _bitfield_part(words, 103, 4, SP_FAULT_BITS, 0)), show=Sensor("Fault Code", entity_category="diagnostic"), tier="fast"),
    Point("fault", read=Read(tuple(range(103, 103+4)), lambda words: _bitfield_part(words, 103, 4, SP_FAULT_BITS, 1)), show=Sensor("Inverter Fault", icon="mdi:alert-octagon-outline"), tier="fast"),
    Point("inverter_problem", show=BinarySensor("Inverter Problem", device_class="problem"), compute=lambda values: _inverter_problem(values)),
    Point("bms_problem", show=BinarySensor("BMS Problem", device_class="problem"), compute=lambda values: _bms_problem(values)),
    Point("grid_connected", read=Read((194,), lambda words: _bool_value(words, 194)), tier="live"),
    Point("gen_charge_enabled", read=Read((231,), lambda words: _bool_value(words, 231)), show=BinarySensor("Generator Charging", device_class=None, icon="mdi:engine", entity_category="diagnostic"), tier="slow"),
    Point("tou_enabled", read=Read((248, 250, 251, 252, 253, 254, 255, 256, 257, 258, 259, 260, 261, 268, 269, 270, 271, 272, 273, 274, 275, 276, 277, 278, 279), lambda words: _tou_value(words, "tou_enabled")), show=BinarySensor("Time of Use Enabled", icon="mdi:calendar-clock", entity_category="diagnostic"), tier="slow"),
    Point("tou_days", read=Read((248, 250, 251, 252, 253, 254, 255, 256, 257, 258, 259, 260, 261, 268, 269, 270, 271, 272, 273, 274, 275, 276, 277, 278, 279), lambda words: _tou_value(words, "tou_days")), tier="slow"),
    Point("tou_slots", read=Read((248, 250, 251, 252, 253, 254, 255, 256, 257, 258, 259, 260, 261, 268, 269, 270, 271, 272, 273, 274, 275, 276, 277, 278, 279), lambda words: _tou_value(words, "tou_slots")), tier="slow"),
    Point("inverter_serial", read=Read(tuple(range(3, 8)), lambda words: serial(words, 3)), show=Sensor("Inverter Serial", icon="mdi:identifier", entity_category="diagnostic"), tier="slow"),
    Point("firmware_control", read=Read(tuple(range(10, 15)), lambda words: _firmware_part(words, 10, 0)), show=Sensor("Control Board Firmware", icon="mdi:chip", entity_category="diagnostic"), tier="slow"),
    Point("firmware_comms", read=Read(tuple(range(10, 15)), lambda words: _firmware_part(words, 10, 1)), show=Sensor("Comms Board Firmware", icon="mdi:chip", entity_category="diagnostic"), tier="slow"),
    Point("mppt_count", read=Read((18,), lambda words: _byte_value(words, 18, 0)), show=Sensor("MPPT Count", icon="mdi:solar-panel", entity_category="diagnostic"), tier="slow"),
    Point("phase_count", read=Read((18,), lambda words: _byte_value(words, 18, 1)), tier="slow"),
    Point("inverter_clock", read=Read((SP_REG_CLOCK, SP_REG_CLOCK+1, SP_REG_CLOCK+2), lambda words: _clock(words, SP_REG_CLOCK)), show=Sensor("Inverter Clock", device_class="timestamp", icon="mdi:clock-outline", entity_category="diagnostic"), tier="slow"),
    Point("battery_mode", read=Read((213,), lambda words: _mapped_value(words, 213, SP_BATTERY_MODES)), show=Sensor("Battery Mode", icon="mdi:battery-sync", entity_category="diagnostic"), tier="slow"),
    Point("bms_protocol", read=Read((325,), lambda words: _mapped_value(words, 325, SP_BMS_PROTOCOLS)), show=Sensor("BMS Protocol", icon="mdi:battery-sync", entity_category="diagnostic"), tier="slow"),
)


MODEL = Model(
    id="single_phase",
    name="Single-phase",
    device_types=(0x0003,),
    points=POINTS,
    capabilities=Capabilities(
        has_battery=True,
        has_tou=True,
        clock_writable=True,
    ),
)

_blocks = MODEL.read_blocks()
SP_LIVE_BLOCKS = _blocks["live"]
SP_FAST_BLOCKS = _blocks["fast"]
SP_SLOW_BLOCKS = _blocks["slow"]


def decode_single_phase(words_by_reg):
    """Decode all declared model points through the common model path."""
    return MODEL.decode(words_by_reg)


__all__ += ["MODEL", "POINTS"]
