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


# --- Poll plan --------------------------------------------------------------
# Minimal set of blocks that covers every telemetry key below. The Deye app
# polls 18 blocks; we only read what we decode. 0x0210 is read with count 14
# (the app used 8) so the block reaches inverter_temp at 0x021D.

READ_BLOCKS: list[tuple[int, int]] = [
    (0x00D2, 4),   # 0x00D2..0x00D5 — BMS charge/discharge voltage + current limits
    (0x0202, 14),  # 0x0202..0x020F — energy totals + daily grid
    (0x0210, 14),  # 0x0210..0x021D — daily/total solar + temperatures
    (0x024A, 6),   # 0x024A..0x024F — battery temp/voltage/soc/power
    (0x0256, 12),  # 0x0256..0x0261 — grid power (0x025F) + grid frequency (0x0261)
    (0x0270, 16),  # 0x0270..0x027F — inverter output L1-3 + grid voltages 0x0273-0x0275
    (0x0280, 16),  # 0x0280..0x028F — house load / UPS load
    (0x02A0, 16),  # 0x02A0..0x02AF — PV input power
]

# Control registers, read on the slower config cycle (P3).
CONTROL_BLOCKS: list[tuple[int, int]] = [
    (0x0068, 1),   # 0x0068 zero-export power (W, signed)
    (0x006C, 2),   # 0x006C max charge current, 0x006D max discharge current (A)
    (0x0073, 3),   # 0x0073/0x0074/0x0075 batt shutdown/restart/low SOC (%)
    (0x008E, 2),   # 0x008E work mode, 0x008F max sell power
    (0x0095, 2),   # 0x0095 charge window start, 0x0096 charge window end (HHMM)
    (0x00A6, 6),   # 0x00A6..0x00AB TOU slot 1-6 target SOC (%) — charge + discharge
    (0x00B2, 14),  # 0x00B2 peak-shaving flags + 0x00BE/0x00BF gen/grid shave power
]


# --- Control register addresses ---------------------------------------------

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


def decode(words_by_reg: dict[int, list[int]]) -> dict[str, float | int | str]:
    """Decode raw register words into HA entity key/value pairs.

    *words_by_reg* maps each polled block's start address to its list of
    register values (as returned by ``protocol.parse_read``). Keys whose
    registers were not present in *words_by_reg* are omitted, so a partial
    poll yields a partial dict rather than wrong values.
    """
    result: dict[str, float | int | str] = {}

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


# --- Encoders ---------------------------------------------------------------

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
