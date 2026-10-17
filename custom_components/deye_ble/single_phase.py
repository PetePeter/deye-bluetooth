"""Single-phase hybrid monitoring profile (device type 0x003).

Addresses/scales cross-checked with kellerza/sunsynk single_phase.py; initial
wire fixtures captured from SUN-6K-SG04LP1-EU, protocol 2.1, DTYPE 21511.
Controls deliberately unsupported: legacy control addresses are incompatible.
"""
from __future__ import annotations

from .registers import decode_clock

PROFILE = "single_phase"
READ_BLOCKS = [(22, 3), (70, 14), (84, 3), (90, 8), (108, 1),
               (150, 5), (164, 16), (180, 15)]
# register, scale, signed, offset
SCALARS = {
    "battery_voltage": (183, 0.01, False, 0),
    "battery_soc": (184, 1, False, 0),
    "battery_temp": (182, 0.1, False, 1000),
    "battery_power": (190, 1, True, 0),
    "grid_voltage_l1": (150, 0.1, False, 0),
    "grid_power": (169, 1, True, 0),
    "inverter_power_l1": (175, 1, True, 0),
    "house_load": (178, 1, True, 0),
    "grid_frequency": (79, 0.01, False, 0),
    "inverter_temp": (91, 0.1, False, 1000),
    "daily_solar": (108, 0.1, False, 0),
    "daily_grid_import": (76, 0.1, False, 0),
    "daily_grid_export": (77, 0.1, False, 0),
    "daily_consumption": (84, 0.1, False, 0),
}
# Low word first. Grid import high word is register 80, not register 79.
TOTALS = {
    "total_battery_charge": (72, 73),
    "total_battery_discharge": (74, 75),
    "total_grid_import": (78, 80),
    "total_grid_export": (81, 82),
    "total_consumption": (85, 86),
    "total_solar": (96, 97),
}
SENSOR_KEYS = frozenset(SCALARS) | frozenset(TOTALS) | {"solar_power"}


def decode(blocks):
    """Decode only this profile; missing words never become zero readings."""
    words = {start + i: value for start, values in blocks.items()
             for i, value in enumerate(values)}
    result = {"_register_profile": PROFILE}
    for key, (address, scale, signed, offset) in SCALARS.items():
        if address not in words:
            continue
        raw = words[address]
        if signed and raw >= 0x8000:
            raw -= 0x10000
        value = (raw - offset) * scale
        result[key] = int(value) if scale == 1 else round(value, 2)
    for key, (low, high) in TOTALS.items():
        if low in words and high in words:
            result[key] = round((words[low] + (words[high] << 16)) * 0.1, 2)
    # This 6 kW LP1 model has two PV inputs; omit unrelated PV3 registers.
    if 186 in words and 187 in words:
        result["solar_power"] = words[186] + words[187]
    if all(22 + i in words for i in range(3)):
        clock = decode_clock([words[22 + i] for i in range(3)])
        if clock is not None:
            result["inverter_clock"] = clock
    return result
