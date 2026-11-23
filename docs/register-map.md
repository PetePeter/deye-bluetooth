# Three-phase hybrid register map (SUN-12K-SG04LP3)

Known holding registers (function `0x03`), collected from live cloud-MITM
captures of the logger's `user/up/modbus/sub` frames and from BLE reads. The
code in `models/three_phase_hybrid.py` is the source of truth for what the
integration actually reads; this page records what is *known*, including
registers not yet exposed, and the evidence behind each one.

Confidence:

- **CONF**: confirmed by a live write-diff, a live BLE read, or an exact
  arithmetic cross-check (for example, phases summing to a total register).
- **~**: likely; matches an app screen value, scale not proven.

Signed values are 16-bit two's complement. Power sign: positive = import from
the grid / discharge into the house.

## Settings (`0x0000`–`0x01FF`)

| Reg | Hex | Meaning | Scale | Conf | Exposed as |
| ---: | --- | --- | --- | :--: | --- |
| 62–64 | `0x003E`–`0x0040` | RTC: `YY MM`, `DD hh`, `mm ss` (one byte each) | — | CONF | `inverter_clock` |
| 102 | `0x0066` | Battery capacity | Ah | ~ | — |
| 104 | `0x0068` | Zero-export power | W, signed | CONF | `zero_export_power` |
| 108 | `0x006C` | Max charge current | A | CONF | `max_charge_current` |
| 109 | `0x006D` | Max discharge current | A | CONF | `max_discharge_current` |
| 115–117 | `0x0073`–`0x0075` | Battery shutdown / restart / low SOC | % | CONF | `batt_*_soc` |
| 128 | `0x0080` | Grid charge current | A | ~ | — |
| 141 | `0x008D` | Solar sell on/off | 0/1 | CONF | `solar_sell_enabled` |
| 142 | `0x008E` | Work mode: 0 Selling First, 1 Zero Export to Load, 2 Zero Export to CT | enum | CONF | `work_mode` |
| 143 | `0x008F` | Max sell power | W | CONF | `max_sell_power` |
| 145 | `0x0091` | TOU enable | 0/1 | CONF | — |
| 146 | `0x0092` | TOU days bitfield (`0xFF` = every day) | bits | CONF | — |
| 148–153 | `0x0094`–`0x0099` | TOU slot 1–6 start time | HHMM | CONF | slot 2/3 as `charge_start`/`charge_end` |
| 154–159 | `0x009A`–`0x009F` | TOU slot 1–6 power | W | CONF | — |
| 160–165 | `0x00A0`–`0x00A5` | TOU slot 1–6 voltage | ÷100 V | CONF | — |
| 166–171 | `0x00A6`–`0x00AB` | TOU slot 1–6 target SOC | % | CONF | `discharge_soc` (166, 168–171), `charge_soc` (167) |
| 172–177 | `0x00AC`–`0x00B1` | TOU slot 1–6 flags: b0 grid charge, b5 sell | bits | CONF | — |
| 178 | `0x00B2` | Advanced fn-1: b2 gen peak-shave, b4 grid peak-shave | bits | CONF | `gen_peak_shaving`, `grid_peak_shaving` |
| 190 | `0x00BE` | Generator peak-shaving power | W | CONF | `gen_peak_power` |
| 191 | `0x00BF` | Grid peak-shaving power | W | CONF | `grid_peak_power` |
| 210–213 | `0x00D2`–`0x00D5` | BMS charge V, discharge V (÷100), charge/discharge current limits (A) | mixed | CONF | `bms_*` |
| 228 | `0x00E4` | b0 = time sync; the RTC latches on its falling edge | bits | CONF | `time_sync` |
| 340 | `0x0154` | Max solar power | W | ~ | — |

## Energy totals (`0x0200`+)

| Reg | Meaning | Scale | Conf | Exposed as |
| ---: | --- | --- | :--: | --- |
| 516–517 | Battery charge total (lo, hi) | ÷10 kWh | CONF | `total_battery_charge` |
| 518–519 | Battery discharge total | ÷10 kWh | CONF | `total_battery_discharge` |
| 520 / 521 | Grid import / export today | ÷10 kWh | CONF | `daily_grid_import/export` |
| 522–523 | Grid import total | ÷10 kWh | CONF | `total_grid_import` |
| 524–525 | Grid export total | ÷10 kWh | CONF | `total_grid_export` |
| 527–528 | Consumption total | ÷10 kWh | CONF | `total_consumption` |
| 529 | Solar today | ÷10 kWh | CONF | `daily_solar` |
| 534–535 | Solar total | ÷10 kWh | CONF | `total_solar` |
| 541 | Inverter temperature | (x−1000)÷10 °C | CONF | `inverter_temp` |

## Live telemetry

| Reg | Hex | Meaning | Scale | Conf | Exposed as |
| ---: | --- | --- | --- | :--: | --- |
| 586 | `0x024A` | Battery temperature | (x−1000)÷10 °C | CONF | `battery_temp` |
| 587 | `0x024B` | Battery voltage | ÷100 V | CONF | `battery_voltage` |
| 588 | `0x024C` | Battery SOC | % | CONF | `battery_soc` |
| 590 | `0x024E` | Battery power (negative = charging) | W, signed | CONF | `battery_power` |
| 604–606 | `0x025C`–`0x025E` | Grid power L1/L2/L3 (internal CT) | W, signed | CONF (sums to 607) | `grid_power_l1..3` |
| 607 | `0x025F` | Grid power total | W, signed | CONF | `grid_power` |
| 609 | `0x0261` | Grid frequency | ÷100 Hz | CONF | `grid_frequency` |
| 610–612 | `0x0262`–`0x0264` | Grid current L1/L2/L3 | ÷100 A, signed | CONF (matches P/V) | `grid_current_l1..3` |
| 616–618 | `0x0268`–`0x026A` | External CT power L1/L2/L3 | W, signed | CONF (sums to 619) | — |
| 619 | `0x026B` | External CT power total | W, signed | CONF | — |
| 622–625 | `0x026E`–`0x0271` | Mirror of 604–607 | W, signed | CONF | — |
| 627 / 628 / 629 | `0x0273`–`0x0275` | Grid voltage L1 / **L3** / **L2** (note the order) | ÷10 V | CONF | `grid_voltage_l*` |
| 633–635 | `0x0279`–`0x027B` | Inverter output L1/L2/L3 | W, signed | CONF | `inverter_power_l*` |
| 636 | `0x027C` | Inverter output total | W, signed | CONF (sums 633–635) | — |
| 640–642 | `0x0280`–`0x0282` | House load L1/L2/L3 | W, signed | CONF (sums to 643; = grid + inverter per phase) | `house_load_l1..3` |
| 643 | `0x0283` | House load | W, signed | CONF | `house_load` |
| 644–646 | `0x0284`–`0x0286` | Load voltage L1/L2/L3 | ÷10 V | ~ | — |
| 650–652 | `0x028A`–`0x028C` | UPS/backup load L1/L2/L3 | W, signed | ~ (sums to 653; equal to 640–642 in every capture so far) | — |
| 653 | `0x028D` | UPS power | W, signed | CONF | `ups_power` |
| 672 | `0x02A0` | Solar power | W | CONF | `solar_power` |
| 676 / 677 | `0x02A4` / `0x02A5` | PV1 voltage / current | ÷10 | CONF | `pv1_voltage`, `pv1_current` |

### Grid phase evidence (2026-06-23 MITM full read, ~12.4 kW import)

```text
604-607  3689  4702  3983  12374   (3689+4702+3983 = 12374)
610-612  1585  1972  1686          (15.9 / 19.7 / 16.9 A; x ~235 V ~= phase W)
616-619  3638  4703  3994  12335   (external CT; sums to 12335)
622-625  3689  4702  3983  12374   (mirror of 604-607)
```

### Load phase evidence

Per phase, house load = grid power + inverter output (signed), and the three
phases sum to 643:

```text
                       L1     L2     L3    total
BLE capture (0x0270/0x0280 frames in tests/test_registers.py)
  grid 604-606        -90   -256    304
  inverter 633-635    434    423    494
  house load 640-642  344    167    798   1309 (= 643)
MITM 2026-06-23
  grid 604-606       3689   4702   3983
  inverter 633-635  -3493  -3146  -3118
  house load 640-642  196   1556    865   2617 (= 643)
```

Both captures show 650–653 equal to 640–643, so which block is grid-side house
load and which is backup-port load is not yet proven by a capture with a load
on only one port.

## Source captures

Raw captures and full per-register dumps (which contain device identifiers and
so are not copied here) live in the private `local-deye-cloud` repo:
`docs/full-read-snapshot-2026-06-23.md`, `docs/register-scan-2026-07-09.md`,
`docs/peak-shaving-registers.md`, `docs/inverter-clock-2026-08-09.md`.
