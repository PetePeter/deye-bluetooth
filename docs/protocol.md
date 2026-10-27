# Deye Logger BLE Local Protocol

Reverse-engineered 2026-06-22 from a cloud TLS/MQTT MITM capture and an Android
BLE HCI snoop. This is the authoritative reference for the integration.

## Transport

- BLE device advertises as the **logger serial** (e.g. `DEYE00000001`), service
  UUID `00000922-0000-1000-8000-00805f9b34fb`, `connectable: true`.
- One BLE central at a time — the phone app and HA cannot both connect.

| Role | Characteristic | Handle | Properties |
|------|----------------|--------|------------|
| Write commands | `0000fec7-...` | `0x001b` | Write Request (**with response**) |
| Notifications | `0000fed8-...` | `0x0018` | Notify (enable CCCD `0x0019` = `0100`) |

> The `fec7` characteristic requires write-**with**-response. Write-without-
> response (and the `fff3`/`fff4` pair) get no reply.

## Framing

ASCII AT commands, `\n`-terminated, over `fec7`; replies arrive as notifications
on `fed8`.

```
Handshake:  AT+DTYPE                       -> +ok=<dtype>,<dtype>  (logger model, e.g. 21521, 21511, 21510)
Read:       AT+INVDATA=8,<modbus_rtu_hex>  -> +ok=<modbus_response_hex>
Write:      AT+INVDATA=11,<modbus_rtu_hex> -> +ok=<modbus_0x10_ack_hex>
```

- `<n>` after `AT+INVDATA=` is the **byte length** of the Modbus frame (8 for a
  standard read, 11 for a single-register write).
- Modbus is standard RTU: slave `0x01`, big-endian address/values, CRC-16/Modbus
  (low byte first on the wire).
- Read = function `0x03` (read holding registers). Write = function `0x10`
  (write multiple, 1 register).

### Examples (verified live)

```
READ  max sell:  AT+INVDATA=8,01030002000125CA -> +ok=0103020104B817
WRITE max sell:  AT+INVDATA=11,0110008F0001020064B884 -> +ok=0110008F00013022
WRITE TOU end:   AT+INVDATA=11,0110009600010205DCB9AF -> +ok=011000960001E1E5
```

## Control registers

| Register | Meaning | Encoding |
|----------|---------|----------|
| `0x008D` | Solar Sell | on/off (normally 1) |
| `0x008E` | **Work Mode** | `0`=Selling First, `1`=Zero Export to Load, `2`=Zero Export to CT |
| `0x008F` | Max Sell Power | watts, 1:1 |
| `0x0091` | TOU enable | 1 = on |
| `0x0092` | TOU days bitfield | `0x00FF` = all days |
| `0x0094..0x0099` | TOU slot 1-6 start time | decimal HHMM in hex (`0x044C`=1100=11:00) |
| `0x009A..0x009F` | TOU slot 1-6 power | W (`0x3A98`=15000) |
| `0x00A0..0x00A5` | TOU slot 1-6 voltage | ×0.01 V (`0x1324`=49.00) |
| `0x00A6..0x00AB` | TOU slot 1-6 target SOC | % |
| `0x00AC..0x00B1` | TOU slot 1-6 grid-charge enable | 1 = on |

Charge window = slot 2: start `0x0095`, end (= slot 3 start) `0x0096`, grid-charge
`0x00AD`, target SOC `0x00A7`.

## Telemetry register blocks (app poll cycle)

The app handshakes once, then reads these 18 blocks each poll. Map to integration
sensors per [`ha-deyecloud-bridge`](https://github.com/PetePeter/ha-deyecloud-bridge)
keys (solar_power, house_load, grid_power, battery_power/soc/voltage/temp,
inverter_temp, daily/total energy, inverter L1-3 power, max_sell_power).

| Reg | Count | Reg | Count | Reg | Count |
|-----|-------|-----|-------|-----|-------|
| `0x0002` | 1 | `0x0210` | 8 | `0x0290` | 16 |
| `0x0016` | 3 | `0x0228` | 1 | `0x02A0` | 16 |
| `0x006F` | 1 | `0x024A` | 6 | `0x02B0` | 16 |
| `0x0085` | 13 | `0x0256` | 10 | `0x02C1` | 4 |
| `0x0150` | 1 | `0x0261` | 15 | | |
| `0x01F4` | 1 | `0x0270` | 16 | | |
| `0x0202` | 14 | `0x0280` | 16 | | |

Exact per-register decode (scaling, signedness) to be finalised in build phase P2
against captured `+ok=` responses and the existing
[`modbus-decode.md`](https://github.com/PetePeter/local-deye-cloud/blob/master/docs/modbus-decode.md).

## Cloud cross-reference (from MITM)

Cloud control writes arrive on `user/down/control/order` as Deye opType-4 (read
list) / opType-5 (write list) wrapping the same registers — confirming the BLE
register addresses match the cloud's. Example work-mode write set `0x008E`, and
the readback ack echoed the new value.

## The logger caches read responses

Measured 2026-08-14 by reading the RTC block (`0x003E`, count 3) at 1 Hz over a
single BLE session with **no writes at all**. The inverter clock advances one
second per second, so any run of byte-identical frames is the logger replaying a
cached response rather than asking the inverter:

```text
22:19:30-35   0103061A080E161316AD5C   2026-08-14 22:19:22    6 identical reads
22:19:36-37   0103061A080E16131EAC9A   2026-08-14 22:19:30    2 identical
22:19:38-45   0103061A080E1613202D4A   2026-08-14 22:19:32    8 identical
22:19:46-47   0103061A080E1613282C8C   2026-08-14 22:19:40    2 identical
22:19:48-54   0103061A080E16132AAD4D   2026-08-14 22:19:42    7 identical
```

The window is **variable and reaches at least 8 seconds**. Five runs do not
establish a maximum, so no fixed delay should be treated as "long enough".

This affects every reader on this transport, not just the clock.

### Consequence 1 — a read-back can lie in both directions

A read taken inside the window carries no information about a write just issued.
It can report the *old* value (failing a write that actually landed) and — the
dangerous direction — it can report a plausible *pre-write* value while the
write has actually corrupted the register, i.e. a silent false pass.

Anything verifying a write over this transport must therefore verify on
**freshness**, not elapsed time: keep the frame seen before the write, and
discard any later frame identical to it. The clock sync additionally requires
two consecutive *distinct* frames, because the cache can refresh from the
inverter a moment before a write lands, producing a frame that is genuinely new
yet still predates the write. See `coordinator._verify_fresh`.

### Consequence 2 — instantaneous drift readings are biased

Because a served frame can be up to the cache age old, every single-sample
drift figure is biased **negative** (the inverter looks slower than it is) by up
to that age. Concretely, in the run above each fresh frame starts ~6 s behind
site time, matching the −6 s the drift sensor reported, and then ages a further
2–8 s before refreshing.

- Multi-hour **means keep their shape** — the bias is bounded and roughly
  constant, so the ~3.1 s/day free-run rate measured over 2026-08-09 → 08-13
  stands.
- Individual figures do not. "The clock is 9 seconds slow" is not a supportable
  claim from one sample; it is drift plus up to ~8 s of staleness.
- It also explains part of the ±5 s sample scatter previously attributed to
  noise. Variable cache age is a mechanism, not randomness, and it is quantised
  by the refresh cycle rather than normally distributed.


## Write the clock as ONE frame

Three separate quantity-1 `0x10` frames covering `0x003E`-`0x0040` corrupt the
year byte on this hardware. One contiguous quantity-3 frame does not.

Interleaved trial, 2026-08-15, Time Sync ON, every write targeting "now", with
only the frame shape varying:

```text
1 block  ok        1 single  2122 CORRUPT
2 block  ok        2 single  2074 CORRUPT
3 block  ok        3 single  2074 CORRUPT
4 block  ok        4 single  2074 CORRUPT
5 block  ok        5 single  ok

BLOCK   0/5 corrupt        SINGLE  4/5 corrupt
```

Interleaved deliberately: every clean block write sits between two failing
single writes, so drift in the logger's state over the run cannot masquerade as
the effect.

The corrupt years are `0x1A + N*0x30` — 0x4A (2074) and 0x7A (2122), never
anything else across six samples. `0x30` is the ASCII offset for digits, and
these frames travel as ASCII hex inside `AT+INVDATA=`, so the pattern is
consistent with a byte passing through one or two extra hex-to-text conversions
at an offset that only the single-register layout produces. The month, day,
hour, minute and second bytes were correct in every corrupt sample; the year is
simply the field where `+0x30` still yields a plausible-looking value instead of
an obviously invalid one.

Every corrupt frame passed CRC, so this is not transmission noise — the wrong
value was checksummed correctly by whatever produced it. And the `0x10` ack
echoes only address and quantity, never the values, so an ack can never confirm
what was written. That is why the clock is verified by read-back.

`build_write` is deliberately unchanged: every other control here writes a
single register and none show this fault.

### The falling edge is still required

A block write does not commit on its own. Ten block writes with Time Sync left
OFF and no toggle — targets varied across 2020-2035, all months, hours outside
the TOU windows — produced **0/10 commits**, with the RTC ticking its own time
throughout. The commit sequence is unchanged: arm, write, clear.
