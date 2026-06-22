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
    (0x0202, 14),  # 0x0202..0x020F — energy totals + daily grid
    (0x0210, 14),  # 0x0210..0x021D — daily/total solar + temperatures
    (0x024A, 6),   # 0x024A..0x024F — battery temp/voltage/soc/power
    (0x0256, 10),  # 0x0256..0x025F — grid power (at 0x025F)
    (0x0270, 16),  # 0x0270..0x027F — inverter output L1-3
    (0x0280, 16),  # 0x0280..0x028F — house load / UPS load
    (0x02A0, 16),  # 0x02A0..0x02AF — PV input power
]

# Control registers, read on the slower config cycle (P3).
CONTROL_BLOCKS: list[tuple[int, int]] = [
    (0x008E, 2),   # 0x008E work mode, 0x008F max sell power
]


# --- Control register addresses ---------------------------------------------

REG_WORK_MODE = 0x008E
REG_MAX_SELL_POWER = 0x008F
REG_TOU_SLOT2_START = 0x0095  # charge window start
REG_TOU_SLOT3_START = 0x0096  # charge window end
REG_CHARGE_SOC = 0x00A7


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
    "max_sell_power":          (0x008F, 1,    False, 0),     # W
    # work_mode handled specially below (enum label, not numeric scale)
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
