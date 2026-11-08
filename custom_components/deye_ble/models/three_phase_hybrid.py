"""Three-phase hybrid register map and decoder."""
from __future__ import annotations

from ..codec import _lookup, clock as _clock, decode_hhmm, encode_hhmm, set_flag
from .base import (
    Capabilities, Model, Number, Point, Read, Select, Sensor, Switch, Time, Write,
    pair_read, scalar_read,
)


# Poll blocks are derived from POINTS below.


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


# --- Work mode enum ---------------------------------------------------------

WORK_MODE_LABELS: dict[int, str] = {
    0: "Selling First",
    1: "Zero Export to Load",
    2: "Zero Export to CT",
}

_WORK_MODE_IDS: dict[str, int] = {v: k for k, v in WORK_MODE_LABELS.items()}


def encode_work_mode(label: str) -> int:
    """Encode a work-mode label to its register value (0, 1, or 2)."""
    return _WORK_MODE_IDS[label]


__all__ = [
    "LIVE_BLOCKS", "FAST_BLOCKS", "SLOW_BLOCKS", "REG_CLOCK", "REG_TIME_SYNC",
    "TIME_SYNC_MASK", "REG_ZERO_EXPORT_POWER", "REG_MAX_CHARGE_CURRENT",
    "REG_MAX_DISCHARGE_CURRENT", "REG_BATT_SHUTDOWN_SOC", "REG_BATT_RESTART_SOC",
    "REG_BATT_LOW_SOC", "REG_WORK_MODE", "REG_MAX_SELL_POWER", "REG_TOU_SLOT2_START",
    "REG_TOU_SLOT3_START", "REG_CHARGE_SOC", "DISCHARGE_SOC_REGS",
    "REG_PEAK_SHAVING_FLAGS", "REG_GEN_PEAK_POWER", "REG_GRID_PEAK_POWER",
    "GEN_PEAK_SHAVE_MASK", "GRID_PEAK_SHAVE_MASK", "WORK_MODE_LABELS",
    "encode_work_mode", "decode",
]


def _mapped_value(words, register, labels):
    raw = _lookup(words, register)
    return None if raw is None else labels.get(raw, f"Unknown ({raw})")


def _flag_value(words, register, mask):
    raw = _lookup(words, register)
    return None if raw is None else bool(raw & mask)


def _hhmm_value(words, register):
    raw = _lookup(words, register)
    if raw is None:
        return None
    try:
        return decode_hhmm(raw)
    except ValueError:
        return None


POINTS = (
    Point("solar_power", read=scalar_read(672, factor=1, signed=False, offset=0), show=Sensor("Solar Power", unit="W", device_class="power", state_class="measurement", icon="mdi:solar-power", precision=0), tier="live"),
    Point("grid_power", read=scalar_read(607, factor=1, signed=True, offset=0), show=Sensor("Grid Power", unit="W", device_class="power", state_class="measurement", icon="mdi:transmission-tower", precision=0), tier="live"),
    Point("battery_power", read=scalar_read(590, factor=1, signed=True, offset=0), show=Sensor("Battery Power", unit="W", device_class="power", state_class="measurement", icon="mdi:battery-charging", precision=0), tier="live"),
    Point("house_load", read=scalar_read(643, factor=1, signed=True, offset=0), show=Sensor("House Load", unit="W", device_class="power", state_class="measurement", icon="mdi:home-lightning-bolt", precision=0), tier="live"),
    Point("ups_power", read=scalar_read(653, factor=1, signed=True, offset=0), show=Sensor("UPS Power", unit="W", device_class="power", state_class="measurement", icon="mdi:power-plug", precision=0), tier="live"),
    Point("inverter_power_l1", read=scalar_read(633, factor=1, signed=True, offset=0), show=Sensor("Inverter Output L1", unit="W", device_class="power", state_class="measurement", icon="mdi:lightning-bolt", precision=0), tier="live"),
    Point("inverter_power_l2", read=scalar_read(634, factor=1, signed=True, offset=0), show=Sensor("Inverter Output L2", unit="W", device_class="power", state_class="measurement", icon="mdi:lightning-bolt", precision=0), tier="live"),
    Point("inverter_power_l3", read=scalar_read(635, factor=1, signed=True, offset=0), show=Sensor("Inverter Output L3", unit="W", device_class="power", state_class="measurement", icon="mdi:lightning-bolt", precision=0), tier="live"),
    Point("battery_soc", read=scalar_read(588, factor=1, signed=False, offset=0), show=Sensor("Battery SOC", unit="%", device_class="battery", state_class="measurement", precision=1), tier="live"),
    Point("battery_voltage", read=scalar_read(587, factor=0.01, signed=False, offset=0), show=Sensor("Battery Voltage", unit="V", device_class="voltage", state_class="measurement", precision=2), tier="live"),
    Point("pv1_voltage", read=scalar_read(676, factor=0.1, signed=False, offset=0), show=Sensor("PV1 Voltage", unit="V", device_class="voltage", state_class="measurement", icon="mdi:solar-panel", precision=1), tier="live"),
    Point("pv1_current", read=scalar_read(677, factor=0.1, signed=False, offset=0), show=Sensor("PV1 Current", unit="A", device_class="current", state_class="measurement", icon="mdi:solar-panel", precision=1), tier="live"),
    Point("battery_temp", read=scalar_read(586, factor=0.1, signed=False, offset=1000), show=Sensor("Battery Temperature", unit="°C", device_class="temperature", state_class="measurement", precision=1), tier="live"),
    Point("inverter_temp", read=scalar_read(541, factor=0.1, signed=False, offset=1000), show=Sensor("Inverter Temperature", unit="°C", device_class="temperature", state_class="measurement", precision=1), tier="fast"),
    Point("bms_charge_voltage", read=scalar_read(210, factor=0.01, signed=False, offset=0), show=Sensor("BMS Charge Voltage", unit="V", device_class="voltage", state_class="measurement", precision=2), tier="slow"),
    Point("bms_discharge_voltage", read=scalar_read(211, factor=0.01, signed=False, offset=0), show=Sensor("BMS Discharge Voltage", unit="V", device_class="voltage", state_class="measurement", precision=2), tier="slow"),
    Point("bms_charge_current_limit", read=scalar_read(212, factor=1, signed=False, offset=0), show=Sensor("BMS Charge Current Limit", unit="A", device_class="current", state_class="measurement", precision=0), tier="slow"),
    Point("bms_discharge_current_limit", read=scalar_read(213, factor=1, signed=False, offset=0), show=Sensor("BMS Discharge Current Limit", unit="A", device_class="current", state_class="measurement", precision=0), tier="slow"),
    Point("grid_voltage_l1", read=scalar_read(627, factor=0.1, signed=False, offset=0), show=Sensor("Grid Voltage L1", unit="V", device_class="voltage", state_class="measurement", precision=1), tier="live"),
    Point("grid_voltage_l3", read=scalar_read(628, factor=0.1, signed=False, offset=0), show=Sensor("Grid Voltage L3", unit="V", device_class="voltage", state_class="measurement", precision=1), tier="live"),
    Point("grid_voltage_l2", read=scalar_read(629, factor=0.1, signed=False, offset=0), show=Sensor("Grid Voltage L2", unit="V", device_class="voltage", state_class="measurement", precision=1), tier="live"),
    Point("grid_frequency", read=scalar_read(609, factor=0.01, signed=False, offset=0), show=Sensor("Grid Frequency", unit="Hz", device_class="frequency", state_class="measurement", precision=2), tier="live"),
    Point("daily_solar", read=scalar_read(529, factor=0.1, signed=False, offset=0), show=Sensor("Solar Today", unit="kWh", device_class="energy", state_class="total_increasing", icon="mdi:solar-power", precision=2), tier="fast"),
    Point("daily_grid_import", read=scalar_read(520, factor=0.1, signed=False, offset=0), show=Sensor("Grid Import Today", unit="kWh", device_class="energy", state_class="total_increasing", icon="mdi:transmission-tower-import", precision=2), tier="fast"),
    Point("daily_grid_export", read=scalar_read(521, factor=0.1, signed=False, offset=0), show=Sensor("Grid Export Today", unit="kWh", device_class="energy", state_class="total_increasing", icon="mdi:transmission-tower-export", precision=2), tier="fast"),
    Point("zero_export_power", read=scalar_read(104, factor=1, signed=True, offset=0), write=Write((104,), "SUN-12K-SG04LP3", lambda value, _data: int(value), reassertable=True), show=Number("Zero Export Power", unit="W", minimum=-15000, maximum=100, step=1, icon="mdi:transmission-tower-import", device_class="power", mode="box"), tier="slow"),
    Point("max_charge_current", read=scalar_read(108, factor=1, signed=False, offset=0), write=Write((108,), "SUN-12K-SG04LP3", lambda value, _data: int(value), reassertable=True), show=Number("Max Charge Current", unit="A", minimum=0, maximum=280, step=1, icon="mdi:battery-arrow-up", device_class="current", mode="box"), tier="slow"),
    Point("max_discharge_current", read=scalar_read(109, factor=1, signed=False, offset=0), write=Write((109,), "SUN-12K-SG04LP3", lambda value, _data: int(value), reassertable=True), show=Number("Max Discharge Current", unit="A", minimum=0, maximum=280, step=1, icon="mdi:battery-arrow-down", device_class="current", mode="box"), tier="slow"),
    Point("batt_shutdown_soc", read=scalar_read(115, factor=1, signed=False, offset=0), write=Write((115,), "SUN-12K-SG04LP3", lambda value, _data: int(value), reassertable=True), show=Number("Battery Shutdown SOC", unit="%", minimum=0, maximum=100, step=1, icon="mdi:battery-off", device_class=None, mode="box"), tier="slow"),
    Point("batt_restart_soc", read=scalar_read(116, factor=1, signed=False, offset=0), write=Write((116,), "SUN-12K-SG04LP3", lambda value, _data: int(value), reassertable=True), show=Number("Battery Restart SOC", unit="%", minimum=0, maximum=100, step=1, icon="mdi:battery-heart-variant", device_class=None, mode="box"), tier="slow"),
    Point("batt_low_soc", read=scalar_read(117, factor=1, signed=False, offset=0), write=Write((117,), "SUN-12K-SG04LP3", lambda value, _data: int(value), reassertable=True), show=Number("Battery Low SOC", unit="%", minimum=0, maximum=100, step=1, icon="mdi:battery-alert", device_class=None, mode="box"), tier="slow"),
    Point("max_sell_power", read=scalar_read(143, factor=1, signed=False, offset=0), write=Write((143,), "SUN-12K-SG04LP3", lambda value, _data: int(value), reassertable=True), show=(Sensor("Max Sell Power", unit="W", device_class="power", state_class="measurement", icon="mdi:transmission-tower-export", precision=0), Number("Max Sell Power", unit="W", minimum=0, maximum=10000, step=1, icon="mdi:transmission-tower-export", device_class="power", mode="box"),), tier="slow"),
    Point("charge_soc", read=scalar_read(167, factor=1, signed=False, offset=0), write=Write((167,), "SUN-12K-SG04LP3", lambda value, _data: int(value), reassertable=True), show=Number("Charge Target SOC", unit="%", minimum=10, maximum=100, step=5, icon="mdi:battery-arrow-up", device_class=None, mode="slider"), tier="slow"),
    Point("discharge_soc", read=scalar_read(166, factor=1, signed=False, offset=0), write=Write((166, 168, 169, 170, 171,), "SUN-12K-SG04LP3", lambda value, _data: {reg: int(value) for reg in DISCHARGE_SOC_REGS}, reassertable=True), show=Number("Discharge SOC", unit="%", minimum=10, maximum=100, step=5, icon="mdi:battery-arrow-down", mode="slider"), tier="slow"),
    Point("gen_peak_power", read=scalar_read(190, factor=1, signed=False, offset=0), write=Write((190,), "SUN-12K-SG04LP3", lambda value, _data: int(value), reassertable=True), show=Number("Gen Peak Shave Power", unit="W", minimum=0, maximum=30000, step=100, icon="mdi:engine", device_class="power", mode="box"), tier="slow"),
    Point("grid_peak_power", read=scalar_read(191, factor=1, signed=False, offset=0), write=Write((191,), "SUN-12K-SG04LP3", lambda value, _data: int(value), reassertable=True), show=Number("Grid Peak Shave Power", unit="W", minimum=0, maximum=30000, step=100, icon="mdi:transmission-tower", device_class="power", mode="box"), tier="slow"),
    Point("total_solar", read=pair_read(534, 535, factor=0.1, signed=False), show=Sensor("Solar Total", unit="kWh", device_class="energy", state_class="total_increasing", precision=2), tier="fast"),
    Point("total_grid_import", read=pair_read(522, 523, factor=0.1, signed=False), show=Sensor("Grid Import Total", unit="kWh", device_class="energy", state_class="total_increasing", icon="mdi:transmission-tower-import", precision=2), tier="fast"),
    Point("total_grid_export", read=pair_read(524, 525, factor=0.1, signed=False), show=Sensor("Grid Export Total", unit="kWh", device_class="energy", state_class="total_increasing", icon="mdi:transmission-tower-export", precision=2), tier="fast"),
    Point("total_battery_charge", read=pair_read(516, 517, factor=0.1, signed=False), show=Sensor("Battery Charge Total", unit="kWh", device_class="energy", state_class="total_increasing", precision=2), tier="fast"),
    Point("total_battery_discharge", read=pair_read(518, 519, factor=0.1, signed=False), show=Sensor("Battery Discharge Total", unit="kWh", device_class="energy", state_class="total_increasing", precision=2), tier="fast"),
    Point("total_consumption", read=pair_read(527, 528, factor=0.1, signed=False), tier="fast"),
    Point("work_mode", read=Read((REG_WORK_MODE,), lambda words: _mapped_value(words, REG_WORK_MODE, WORK_MODE_LABELS)), write=Write((142,), "SUN-12K-SG04LP3", lambda value, _data: next(raw for raw, label in WORK_MODE_LABELS.items() if label == value), reassertable=True), show=Select("Work Mode", WORK_MODE_LABELS, icon="mdi:solar-power-variant"), tier="slow"),
    Point("peak_shaving_flags_raw", read=Read((REG_PEAK_SHAVING_FLAGS,), lambda words: _lookup(words, REG_PEAK_SHAVING_FLAGS)), tier="slow"),
    Point("grid_peak_shaving", read=Read((REG_PEAK_SHAVING_FLAGS,), lambda words: _flag_value(words, REG_PEAK_SHAVING_FLAGS, GRID_PEAK_SHAVE_MASK)), write=Write((178,), "SUN-12K-SG04LP3", lambda value, data: set_flag(int(data['peak_shaving_flags_raw']), GRID_PEAK_SHAVE_MASK, bool(value)), reassertable=False), show=Switch("Grid Peak Shaving", icon="mdi:transmission-tower"), tier="slow"),
    Point("gen_peak_shaving", read=Read((REG_PEAK_SHAVING_FLAGS,), lambda words: _flag_value(words, REG_PEAK_SHAVING_FLAGS, GEN_PEAK_SHAVE_MASK)), write=Write((178,), "SUN-12K-SG04LP3", lambda value, data: set_flag(int(data['peak_shaving_flags_raw']), GEN_PEAK_SHAVE_MASK, bool(value)), reassertable=False), show=Switch("Gen Peak Shaving", icon="mdi:engine"), tier="slow"),
    Point("inverter_clock", read=Read((REG_CLOCK, REG_CLOCK+1, REG_CLOCK+2), lambda words: _clock(words, REG_CLOCK)), show=Sensor("Inverter Clock", device_class="timestamp", icon="mdi:clock-outline", entity_category="diagnostic"), tier="slow"),
    Point("time_sync", read=Read((REG_TIME_SYNC,), lambda words: _flag_value(words, REG_TIME_SYNC, TIME_SYNC_MASK)), tier="slow"),
    Point("charge_start", read=Read((149,), lambda words: _hhmm_value(words, 149)), write=Write((149,), "SUN-12K-SG04LP3", lambda value, _data: encode_hhmm(value), reassertable=True), show=Time("Grid Charge From", icon="mdi:battery-charging"), tier="slow"),
    Point("charge_end", read=Read((150,), lambda words: _hhmm_value(words, 150)), write=Write((150,), "SUN-12K-SG04LP3", lambda value, _data: encode_hhmm(value), reassertable=True), show=Time("Grid Charge To", icon="mdi:battery-charging-outline"), tier="slow"),
)


MODEL = Model(
    id="three_phase",
    name="Three-phase",
    device_types=(),
    points=POINTS,
    capabilities=Capabilities(
        has_battery=True,
        has_tou=True,
        clock_writable=True,
        cloud_time_sync=True,
    ),
)

_blocks = MODEL.read_blocks()
LIVE_BLOCKS = _blocks["live"]
FAST_BLOCKS = _blocks["fast"]
SLOW_BLOCKS = _blocks["slow"]


def decode(words_by_reg):
    """Decode all declared model points through the common model path."""
    return MODEL.decode(words_by_reg)


__all__ += ["MODEL", "POINTS"]
