"""P1 — pure BLE transport protocol tests.

Every fixture here is a live-verified hex string from docs/protocol.md and
local-deye-cloud/docs/ble-local-mode.md, so these pin the implementation
against real device bytes (no hardware, no mocks).
"""
import pytest

from custom_components.deye_ble import protocol as p


# --- CRC-16/Modbus ---------------------------------------------------------

def test_crc16_matches_captured_read_frame():
    # Frame 01 03 00 02 00 01 was captured with trailing CRC 25 CA.
    assert p.crc16(bytes.fromhex("010300020001")) == bytes.fromhex("25ca")


def test_crc16_is_little_endian_on_the_wire():
    # Low byte first: the appended CRC for the max-sell write echoes 30 22.
    assert p.crc16(bytes.fromhex("0110008F0001020064")) == bytes.fromhex("b884")


# --- Frame building --------------------------------------------------------

def test_build_read_reg_0002():
    assert p.build_read(0x0002, 1).hex() == "01030002000125ca"


def test_build_read_reg_008f():
    assert p.build_read(0x008F, 1).hex() == "0103008f0001" + p.crc16(
        bytes.fromhex("0103008f0001")
    ).hex()


def test_build_write_max_sell_100w():
    assert p.build_write(0x008F, 100).hex() == "0110008f0001020064b884"


def test_build_write_tou_end_1500():
    # 15:00 -> HHMM 1500 -> 0x05DC.
    assert p.build_write(0x0096, 0x05DC).hex() == "0110009600010205dcb9af"


# --- AT wrapping -----------------------------------------------------------

def test_wrap_read_uses_len_8_and_newline():
    frame = p.build_read(0x0002, 1)
    assert p.wrap_read(frame) == b"AT+INVDATA=8,01030002000125CA\n"


def test_wrap_write_uses_len_11_and_newline():
    frame = p.build_write(0x008F, 100)
    assert p.wrap_write(frame) == b"AT+INVDATA=11,0110008F0001020064B884\n"


# --- Reply parsing ---------------------------------------------------------

def test_handshake_ack_recognised():
    assert p.is_handshake_ack("+ok=21521,21521") is True
    assert p.is_handshake_ack("+ok=0103020104B817") is False


def test_parse_read_single_register():
    assert p.parse_read("+ok=0103020104B817") == [0x0104]  # 260


def test_parse_read_max_sell_values():
    assert p.parse_read("+ok=0103020064B9AF") == [100]
    assert p.parse_read("+ok=0103020028B85A") == [40]


def test_parse_read_multi_register_big_endian():
    # 13-register battery block @0x0085 (count 13 -> 26 data bytes).
    data = "0001" "0002" "0003" "0004" "0005" "0006" "0007" \
           "0008" "0009" "000A" "000B" "000C" "000D"
    body = "01" "03" "1A" + data
    frame = bytes.fromhex(body)
    full = (frame + p.crc16(frame)).hex()
    assert p.parse_read("+ok=" + full) == list(range(1, 14))


def test_parse_write_ack_confirms_echo():
    frame = p.build_write(0x008F, 100)
    assert p.parse_write_ack("+ok=0110008F00013022", frame) is True


def test_parse_write_ack_rejects_wrong_address():
    frame = p.build_write(0x008F, 100)
    # Ack echoes register 0x0096, not 0x008F.
    assert p.parse_write_ack("+ok=011000960001E1E5", frame) is False


# --- Error paths -----------------------------------------------------------

def test_parse_read_rejects_bad_crc():
    with pytest.raises(p.ProtocolError):
        p.parse_read("+ok=0103020104FFFF")


def test_parse_read_rejects_short_frame():
    with pytest.raises(p.ProtocolError):
        p.parse_read("+ok=0103")


def test_parse_read_rejects_modbus_exception():
    # Function 0x83 = read (0x03) with the error bit set, exception code 0x02.
    frame = bytes.fromhex("018302")
    full = (frame + p.crc16(frame)).hex()
    with pytest.raises(p.ProtocolError):
        p.parse_read("+ok=" + full)
