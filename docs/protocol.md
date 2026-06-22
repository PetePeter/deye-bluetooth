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
Handshake:  AT+DTYPE                       -> +ok=21521,21521
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
