"""SUN G05 on-grid string inverter register map and decoder."""
from __future__ import annotations

from ..codec import clock as _clock, serial
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

STR_REG_CLOCK = 22  # 22-24, packed like the other profiles (deye_string "Date & Time")

__all__ = [
    "STR_LIVE_BLOCKS", "STR_FAST_BLOCKS", "STR_SLOW_BLOCKS", "STR_REG_CLOCK",
    "decode_string",
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
