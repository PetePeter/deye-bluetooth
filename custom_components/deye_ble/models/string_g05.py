"""SUN G05 on-grid string inverter register map and decoder."""
from __future__ import annotations

from ..codec import (
    _decode_32, _decode_scalar, _lookup, bitfield_text, clock as _clock,
    firmware, serial, split_bytes,
)
from .base import BinarySensor, Capabilities, Model, Point, Read, Sensor, pair_read, scalar_read
from .single_phase_lp1 import (
    SP_ALARM_BITS, SP_DEVICE_STATES, SP_FAULT_BITS, _bitfield_part,
    _byte_value, _firmware_part, _inverter_problem, _mapped_value,
)


# --- String (on-grid, no battery) profile -----------------------------------
# Three-phase string inverters (SUN-xK-G03/G04/G05) report device type 0x0002
# and use the low "string" map (Modbus RTU V118). Addresses, scaling and
# signedness follow ha-solarman deye_string.yaml; the three-phase hybrid map
# above reads unmapped memory there (2910 A currents, -100 °C temperatures).
#
# Totals are 32-bit, low word first, like the other profiles. Temperatures use
# the same (raw - 1000) / 10 rule. There is no battery, no CT/meter by default
# (0x00C6+ meter block stays unread) and no control table: the profile is
# telemetry-only, so the read-only lock has nothing to guard but the clock.

STR_LIVE_BLOCKS: list[tuple[int, int]] = [
    (70, 16),   # 70-72 line voltages, 73-78 phase V/I, 79 freq, 80-85 output/input/apparent power
    (86, 6),    # 86-87 active power, 88-89 reactive power, 90 DC temp, 91 AC temp
]
STR_FAST_BLOCKS: list[tuple[int, int]] = [
    (59, 6),    # 59 state, 60 today production, 63-64 total production
    (101, 12),  # 101-102 alarm, 103-106 fault, 109-112 PV1/PV2 V + I
]
STR_SLOW_BLOCKS: list[tuple[int, int]] = [
    (2, 16),    # 2 protocol, 3-7 SN, 10-14 firmware, 16-17 rated power
    (18, 7),    # 18 MPPTs/phases, 22-24 inverter clock
]

STR_REG_CLOCK = 22  # 22-24, packed like the other profiles (deye_string "Date & Time")

_STR_DECODE_MAP: dict[str, tuple[int, float, bool, int]] = {
    "grid_line_voltage_l1_l2": (70, 0.1,  False, 0),
    "grid_line_voltage_l2_l3": (71, 0.1,  False, 0),
    "grid_line_voltage_l3_l1": (72, 0.1,  False, 0),
    "grid_voltage_l1":         (73, 0.1,  False, 0),
    "grid_voltage_l2":         (74, 0.1,  False, 0),
    "grid_voltage_l3":         (75, 0.1,  False, 0),
    "grid_current_l1":         (76, 0.1,  True,  0),
    "grid_current_l2":         (77, 0.1,  True,  0),
    "grid_current_l3":         (78, 0.1,  True,  0),
    "grid_frequency":          (79, 0.01, False, 0),
    "inverter_temp":           (90, 0.1,  False, 1000),  # DC side
    "inverter_ac_temp":        (91, 0.1,  False, 1000),  # AC side / heatsink
    "pv1_voltage":             (109, 0.1, False, 0),
    "pv1_current":             (110, 0.1, False, 0),
    "pv2_voltage":             (111, 0.1, False, 0),
    "pv2_current":             (112, 0.1, False, 0),
    "daily_solar":             (60, 0.1,  False, 0),
}

_STR_DECODE32: dict[str, tuple] = {
    "total_solar":             (63, 64, 0.1),
    "solar_power":             (86, 87, 0.1),        # AC active power, W (unsigned)
    "pv_input_power":          (82, 83, 0.1),        # DC input power, W (unsigned)
    "output_apparent_power":   (84, 85, 0.1),        # VA (unsigned)
    "output_reactive_power":   (88, 89, 0.1, True),  # var (signed)
}
_STR_CONFIG_MAP: dict[str, tuple[int, float, bool, int]] = {
    "protocol_version":        (2, 1, False, 0),
}


def decode_string(words_by_reg: dict[int, list[int]]) -> dict:
    """Decode a string-inverter poll (any subset of the STR_* tier blocks).

    Same contract as :func:`decode`: keys whose registers were not polled are
    omitted, never guessed.
    """
    result: dict = {}
    _decode_scalar(words_by_reg, _STR_DECODE_MAP, result)
    _decode_scalar(words_by_reg, _STR_CONFIG_MAP, result)
    _decode_32(words_by_reg, _STR_DECODE32, result)
    _decode_32(words_by_reg, _STR_CONFIG32, result)

    # No dedicated per-string power register: P = U x I (W).
    for n in (1, 2):
        v, i = result.get(f"pv{n}_voltage"), result.get(f"pv{n}_current")
        if v is not None and i is not None:
            result[f"pv{n}_power"] = round(v * i, 2)

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

    inverter_serial = serial(words_by_reg, 3)
    if inverter_serial is not None:
        result["inverter_serial"] = inverter_serial
    firmware_versions = firmware(words_by_reg, 10)
    if firmware_versions is not None:
        result["firmware_control"], result["firmware_comms"] = firmware_versions
    mppt = _lookup(words_by_reg, 18)
    if mppt is not None:
        result["mppt_count"], result["phase_count"] = split_bytes(mppt)

    inverter_clock = _clock(words_by_reg, STR_REG_CLOCK)
    if inverter_clock is not None:
        result["inverter_clock"] = inverter_clock

    return result



_STR_CONFIG32: dict[str, tuple[int, int, float]] = {
    "rated_power": (16, 17, 0.1),
}

__all__ = [
    "STR_LIVE_BLOCKS", "STR_FAST_BLOCKS", "STR_SLOW_BLOCKS", "STR_REG_CLOCK",
    "decode_string", "_STR_DECODE_MAP", "_STR_DECODE32", "_STR_CONFIG_MAP",
    "_STR_CONFIG32",
]


def _product_if_present(values, left, right):
    if left not in values or right not in values:
        return None
    return round(values[left] * values[right], 2)


POINTS = (
    Point("grid_line_voltage_l1_l2", read=scalar_read(70, factor=0.1, signed=False, offset=0), tier="live"),
    Point("grid_line_voltage_l2_l3", read=scalar_read(71, factor=0.1, signed=False, offset=0), tier="live"),
    Point("grid_line_voltage_l3_l1", read=scalar_read(72, factor=0.1, signed=False, offset=0), tier="live"),
    Point("grid_voltage_l1", read=scalar_read(73, factor=0.1, signed=False, offset=0), show=Sensor("Grid Voltage L1", unit="V", device_class="voltage", state_class="measurement", precision=1), tier="live"),
    Point("grid_voltage_l2", read=scalar_read(74, factor=0.1, signed=False, offset=0), show=Sensor("Grid Voltage L2", unit="V", device_class="voltage", state_class="measurement", precision=1), tier="live"),
    Point("grid_voltage_l3", read=scalar_read(75, factor=0.1, signed=False, offset=0), show=Sensor("Grid Voltage L3", unit="V", device_class="voltage", state_class="measurement", precision=1), tier="live"),
    Point("grid_current_l1", read=scalar_read(76, factor=0.1, signed=True, offset=0), show=Sensor("Grid Current L1", unit="A", device_class="current", state_class="measurement", precision=1), tier="live"),
    Point("grid_current_l2", read=scalar_read(77, factor=0.1, signed=True, offset=0), show=Sensor("Grid Current L2", unit="A", device_class="current", state_class="measurement", precision=1), tier="live"),
    Point("grid_current_l3", read=scalar_read(78, factor=0.1, signed=True, offset=0), show=Sensor("Grid Current L3", unit="A", device_class="current", state_class="measurement", precision=1), tier="live"),
    Point("grid_frequency", read=scalar_read(79, factor=0.01, signed=False, offset=0), show=Sensor("Grid Frequency", unit="Hz", device_class="frequency", state_class="measurement", precision=2), tier="live"),
    Point("inverter_temp", read=scalar_read(90, factor=0.1, signed=False, offset=1000), show=Sensor("Inverter DC Temperature", unit="°C", device_class="temperature", state_class="measurement", precision=1), tier="live"),
    Point("inverter_ac_temp", read=scalar_read(91, factor=0.1, signed=False, offset=1000), show=Sensor("Inverter AC Temperature", unit="°C", device_class="temperature", state_class="measurement", precision=1), tier="live"),
    Point("pv1_voltage", read=scalar_read(109, factor=0.1, signed=False, offset=0), show=Sensor("PV1 Voltage", unit="V", device_class="voltage", state_class="measurement", icon="mdi:solar-panel", precision=1), tier="fast"),
    Point("pv1_current", read=scalar_read(110, factor=0.1, signed=False, offset=0), show=Sensor("PV1 Current", unit="A", device_class="current", state_class="measurement", icon="mdi:solar-panel", precision=1), tier="fast"),
    Point("pv2_voltage", read=scalar_read(111, factor=0.1, signed=False, offset=0), show=Sensor("PV2 Voltage", unit="V", device_class="voltage", state_class="measurement", icon="mdi:solar-panel", precision=1), tier="fast"),
    Point("pv2_current", read=scalar_read(112, factor=0.1, signed=False, offset=0), show=Sensor("PV2 Current", unit="A", device_class="current", state_class="measurement", icon="mdi:solar-panel", precision=1), tier="fast"),
    Point("daily_solar", read=scalar_read(60, factor=0.1, signed=False, offset=0), show=Sensor("Solar Today", unit="kWh", device_class="energy", state_class="total_increasing", icon="mdi:solar-power", precision=2), tier="fast"),
    Point("protocol_version", read=scalar_read(2, factor=1, signed=False, offset=0), show=Sensor("Modbus Protocol", entity_category="diagnostic"), tier="slow"),
    Point("total_solar", read=pair_read(63, 64, factor=0.1, signed=False), show=Sensor("Solar Total", unit="kWh", device_class="energy", state_class="total_increasing", icon="mdi:solar-power", precision=1), tier="fast"),
    Point("solar_power", read=pair_read(86, 87, factor=0.1, signed=False), show=Sensor("Solar Power", unit="W", device_class="power", state_class="measurement", icon="mdi:solar-power", precision=0), tier="live"),
    Point("pv_input_power", read=pair_read(82, 83, factor=0.1, signed=False), show=Sensor("PV Input Power", unit="W", device_class="power", state_class="measurement", icon="mdi:solar-power-variant", precision=0), tier="live"),
    Point("output_apparent_power", read=pair_read(84, 85, factor=0.1, signed=False), tier="live"),
    Point("output_reactive_power", read=pair_read(88, 89, factor=0.1, signed=True), tier="live"),
    Point("rated_power", read=pair_read(16, 17, factor=0.1, signed=False), show=Sensor("Rated Power", unit="W", device_class="power", precision=0, entity_category="diagnostic"), tier="slow"),
    Point("pv1_power", show=Sensor("PV1 Power", unit="W", device_class="power", state_class="measurement", icon="mdi:solar-panel", precision=0), compute=lambda values: _product_if_present(values, 'pv1_voltage', 'pv1_current')),
    Point("pv2_power", show=Sensor("PV2 Power", unit="W", device_class="power", state_class="measurement", icon="mdi:solar-panel", precision=0), compute=lambda values: _product_if_present(values, 'pv2_voltage', 'pv2_current')),
    Point("device_state", read=Read((59,), lambda words: _mapped_value(words, 59, SP_DEVICE_STATES)), show=Sensor("Inverter State", icon="mdi:state-machine"), tier="fast"),
    Point("alarm_code", read=Read(tuple(range(101, 101+2)), lambda words: _bitfield_part(words, 101, 2, SP_ALARM_BITS, 0)), show=Sensor("Alarm Code", entity_category="diagnostic"), tier="fast"),
    Point("alarm", read=Read(tuple(range(101, 101+2)), lambda words: _bitfield_part(words, 101, 2, SP_ALARM_BITS, 1)), show=Sensor("Inverter Alarm", icon="mdi:alert-outline"), tier="fast"),
    Point("fault_code", read=Read(tuple(range(103, 103+4)), lambda words: _bitfield_part(words, 103, 4, SP_FAULT_BITS, 0)), show=Sensor("Fault Code", entity_category="diagnostic"), tier="fast"),
    Point("fault", read=Read(tuple(range(103, 103+4)), lambda words: _bitfield_part(words, 103, 4, SP_FAULT_BITS, 1)), show=Sensor("Inverter Fault", icon="mdi:alert-octagon-outline"), tier="fast"),
    Point("inverter_problem", show=BinarySensor("Inverter Problem", device_class="problem"), compute=lambda values: _inverter_problem(values)),
    Point("inverter_serial", read=Read(tuple(range(3, 8)), lambda words: serial(words, 3)), show=Sensor("Inverter Serial", icon="mdi:identifier", entity_category="diagnostic"), tier="slow"),
    Point("firmware_control", read=Read(tuple(range(10, 15)), lambda words: _firmware_part(words, 10, 0)), show=Sensor("Control Board Firmware", icon="mdi:chip", entity_category="diagnostic"), tier="slow"),
    Point("firmware_comms", read=Read(tuple(range(10, 15)), lambda words: _firmware_part(words, 10, 1)), show=Sensor("Comms Board Firmware", icon="mdi:chip", entity_category="diagnostic"), tier="slow"),
    Point("mppt_count", read=Read((18,), lambda words: _byte_value(words, 18, 0)), show=Sensor("MPPT Count", icon="mdi:solar-panel", entity_category="diagnostic"), tier="slow"),
    Point("phase_count", read=Read((18,), lambda words: _byte_value(words, 18, 1)), show=Sensor("Phase Count", icon="mdi:sine-wave", entity_category="diagnostic"), tier="slow"),
    Point("inverter_clock", read=Read((STR_REG_CLOCK, STR_REG_CLOCK+1, STR_REG_CLOCK+2), lambda words: _clock(words, STR_REG_CLOCK)), show=Sensor("Inverter Clock", device_class="timestamp", icon="mdi:clock-outline", entity_category="diagnostic"), tier="slow"),
)


MODEL = Model(
    id="string",
    name="String (on-grid)",
    device_types=(0x0002, 0x0200),
    points=POINTS,
    capabilities=Capabilities(),
)

_blocks = MODEL.read_blocks()
STR_LIVE_BLOCKS = _blocks["live"]
STR_FAST_BLOCKS = _blocks["fast"]
STR_SLOW_BLOCKS = _blocks["slow"]


def decode_string(words_by_reg):
    """Decode all declared string-model points through the common path."""
    return MODEL.decode(words_by_reg)


__all__ += ["MODEL", "POINTS"]
