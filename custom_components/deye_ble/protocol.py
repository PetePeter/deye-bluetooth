"""Pure Deye logger BLE local protocol — framing, Modbus RTU, CRC.

No I/O and no bleak: everything here is deterministic byte-shuffling so it can
be unit-tested without hardware. The bleak transport (connect / notify / write)
lives in transport.py and calls into these helpers.

Wire format (see docs/protocol.md):
    Handshake  AT+DTYPE\n                      -> +ok=21521,21521
    Read       AT+INVDATA=8,<modbus func03>\n  -> +ok=<modbus response>
    Write      AT+INVDATA=11,<modbus func10>\n -> +ok=<modbus 0x10 ack>

Modbus is standard RTU: slave 0x01, big-endian address/values, CRC-16/Modbus
appended low byte first.
"""
from __future__ import annotations

SLAVE = 0x01
FUNC_READ = 0x03           # read holding registers
FUNC_WRITE = 0x10          # write multiple registers
HANDSHAKE_ACK = "+ok=21521,21521"

_OK_PREFIX = "+ok="


class ProtocolError(Exception):
    """Malformed reply, bad CRC, or a Modbus exception response."""


# --- CRC-16/Modbus ---------------------------------------------------------

def crc16(data: bytes) -> bytes:
    """CRC-16/Modbus over *data*, returned as the 2 wire bytes (low byte first)."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return bytes((crc & 0xFF, (crc >> 8) & 0xFF))


def _with_crc(frame: bytes) -> bytes:
    return frame + crc16(frame)


# --- Frame building --------------------------------------------------------

def build_read(address: int, count: int) -> bytes:
    """Modbus func 0x03 read frame for *count* registers from *address*."""
    body = bytes((
        SLAVE, FUNC_READ,
        (address >> 8) & 0xFF, address & 0xFF,
        (count >> 8) & 0xFF, count & 0xFF,
    ))
    return _with_crc(body)


def build_write(address: int, value: int) -> bytes:
    """Modbus func 0x10 frame writing a single 16-bit *value* to *address*."""
    body = bytes((
        SLAVE, FUNC_WRITE,
        (address >> 8) & 0xFF, address & 0xFF,
        0x00, 0x01,            # quantity = 1 register
        0x02,                  # byte count
        (value >> 8) & 0xFF, value & 0xFF,
    ))
    return _with_crc(body)


# --- AT wrapping -----------------------------------------------------------

def wrap_read(frame: bytes) -> bytes:
    return b"AT+INVDATA=%d,%b\n" % (len(frame), frame.hex().upper().encode())


def wrap_write(frame: bytes) -> bytes:
    return b"AT+INVDATA=%d,%b\n" % (len(frame), frame.hex().upper().encode())


# --- Reply parsing ---------------------------------------------------------

def is_handshake_ack(reply: str) -> bool:
    return reply.strip() == HANDSHAKE_ACK


def _payload(reply: str) -> bytes:
    """Strip the `+ok=` prefix, decode hex, and verify the trailing CRC."""
    reply = reply.strip()
    if not reply.startswith(_OK_PREFIX):
        raise ProtocolError(f"missing +ok= prefix: {reply!r}")
    hex_body = reply[len(_OK_PREFIX):]
    try:
        raw = bytes.fromhex(hex_body)
    except ValueError as e:
        raise ProtocolError(f"non-hex payload: {hex_body!r}") from e
    if len(raw) < 4:
        raise ProtocolError(f"frame too short: {hex_body!r}")
    body, got_crc = raw[:-2], raw[-2:]
    if crc16(body) != got_crc:
        raise ProtocolError(f"CRC mismatch in {hex_body!r}")
    if body[1] & 0x80:
        code = body[2] if len(body) > 2 else 0
        raise ProtocolError(f"Modbus exception, function 0x{body[1]:02X} code 0x{code:02X}")
    return body


def parse_read(reply: str) -> list[int]:
    """Parse a func 0x03 response into a list of big-endian register values."""
    body = _payload(reply)
    if len(body) < 3 or body[1] != FUNC_READ:
        raise ProtocolError(f"not a read response: {reply!r}")
    byte_count = body[2]
    data = body[3:]
    if byte_count != len(data) or byte_count % 2 != 0:
        raise ProtocolError(f"byte count {byte_count} != {len(data)} data bytes")
    return [int.from_bytes(data[i:i + 2], "big") for i in range(0, len(data), 2)]


def parse_write_ack(reply: str, request: bytes) -> bool:
    """True if the func 0x10 ack echoes the address + quantity of *request*."""
    body = _payload(reply)
    if len(body) < 6 or body[1] != FUNC_WRITE:
        return False
    # Bytes 2-5 are address_hi, address_lo, qty_hi, qty_lo in both frames.
    return body[2:6] == request[2:6]
