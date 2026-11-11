"""Pure conversions between Deye register words and Python values."""
from __future__ import annotations

from datetime import datetime
from typing import Mapping, Sequence


WordBlocks = Mapping[int, Sequence[int]]
CLOCK_WORD_COUNT = 3


def u16(raw: int) -> int:
    """Return a raw unsigned 16-bit word unchanged."""
    return raw


def s16(raw: int) -> int:
    """Interpret a 16-bit unsigned word as signed two's complement."""
    return raw if raw < 0x8000 else raw - 0x10000


def u32(low: int, high: int) -> int:
    """Join low-word-first 16-bit words into an unsigned 32-bit value."""
    return (high << 16) | low


def s32(low: int, high: int) -> int:
    """Join low-word-first words and interpret them as signed two's complement."""
    raw = u32(low, high)
    return raw - (1 << 32) if raw & 0x8000_0000 else raw


def scaled(raw: int, factor: float, *, offset: int = 0) -> int | float:
    """Apply a register offset and scale, preserving integer values when exact."""
    value = (raw - offset) * factor
    return int(value) if factor == 1 and offset == 0 else round(value, 2)


def lookup(words_by_reg: WordBlocks, reg: int) -> int | None:
    """Return the raw word at absolute register ``reg``, if it was polled."""
    for block_start, words in words_by_reg.items():
        offset = reg - block_start
        if 0 <= offset < len(words):
            return words[offset]
    return None


def words_at(words_by_reg: WordBlocks, start: int, count: int) -> list[int] | None:
    """Return a complete consecutive word range, or ``None`` if any are absent."""
    words = [lookup(words_by_reg, start + i) for i in range(count)]
    return None if any(word is None for word in words) else words


def _bit_texts(value: int, bits: Mapping[int, str]) -> str:
    if value == 0:
        return "OK"
    return ", ".join(
        bits.get(i, f"bit {i}") for i in range(value.bit_length()) if value >> i & 1
    )


def decode_words_bitfield(words: Sequence[int]) -> int:
    """Join a little-word-first bitfield of any length."""
    return sum(word << (16 * i) for i, word in enumerate(words))


def bitfield_text(
    words_by_reg: WordBlocks, start: int, count: int, bits: Mapping[int, str],
) -> tuple[int, str] | None:
    """Decode a contiguous bitfield to its raw value and readable text."""
    words = words_at(words_by_reg, start, count)
    if words is None:
        return None
    value = decode_words_bitfield(words)
    return value, _bit_texts(value, bits)


def serial(words_by_reg: WordBlocks, start: int) -> str | None:
    """Decode five consecutive words as the inverter's ASCII serial number."""
    words = words_at(words_by_reg, start, 5)
    if words is None:
        return None
    text = bytes(byte for word in words for byte in (word >> 8, word & 0xFF))
    return text.decode("ascii", "replace").strip("\x00 ")


def firmware(words_by_reg: WordBlocks, start: int) -> tuple[str, str] | None:
    """Decode the five-word control-board and comms-board firmware layout."""
    words = words_at(words_by_reg, start, 5)
    if words is None:
        return None
    control = ".".join(f"{word:04X}" for word in (words[3], words[2], words[1]))
    comms = ".".join(f"{word:04X}" for word in (words[4], words[0]))
    return control, comms


def split_bytes(raw: int) -> tuple[int, int]:
    """Return the high and low bytes of a register word."""
    return raw >> 8, raw & 0xFF


def clock(words_by_reg: WordBlocks, start: int) -> datetime | None:
    """Decode three packed RTC words beginning at ``start``."""
    words = words_at(words_by_reg, start, CLOCK_WORD_COUNT)
    return None if words is None else decode_clock(words)


def decode_clock(words: Sequence[int]) -> datetime | None:
    """Decode packed RTC byte pairs to a naive local datetime, if valid."""
    if len(words) < CLOCK_WORD_COUNT:
        return None
    year, month = 2000 + (words[0] >> 8), words[0] & 0xFF
    day, hour = words[1] >> 8, words[1] & 0xFF
    minute, second = words[2] >> 8, words[2] & 0xFF
    try:
        return datetime(year, month, day, hour, minute, second)
    except ValueError:
        return None


def encode_clock(value: datetime) -> list[int]:
    """Encode a local datetime to the three packed RTC words."""
    if not 2000 <= value.year <= 2255:
        raise ValueError(f"year out of register range: {value.year}")
    return [
        ((value.year - 2000) << 8) | value.month,
        (value.day << 8) | value.hour,
        (value.minute << 8) | value.second,
    ]


def encode_hhmm(time_str: str) -> int:
    """Encode canonical ``HH:MM`` text to decimal HHMM."""
    parts = time_str.split(":")
    if len(parts) != 2 or not all(len(part) == 2 and part.isdigit() for part in parts):
        raise ValueError(f"expected canonical 'HH:MM', got {time_str!r}")
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"time out of range: {time_str!r}")
    return hour * 100 + minute


def decode_hhmm(raw: int) -> str:
    """Decode decimal HHMM to canonical ``HH:MM`` text."""
    hour, minute = divmod(raw, 100)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"invalid HHMM register value: {raw}")
    return f"{hour:02d}:{minute:02d}"


def set_flag(raw: int, mask: int, on: bool) -> int:
    """Set or clear bits without changing the other bits in a word."""
    return (raw | mask) if on else (raw & ~mask)


# Private aliases retained for callers that imported the old implementation helpers.
_signed16 = s16
_lookup = lookup
_words = words_at
_bit_texts = _bit_texts
_decode_words_bitfield = decode_words_bitfield


__all__ = [
    "CLOCK_WORD_COUNT", "WordBlocks", "u16", "s16", "u32", "s32", "scaled",
    "lookup", "words_at", "bitfield_text",
    "serial", "firmware", "split_bytes", "clock", "decode_clock", "encode_clock",
    "encode_hhmm", "decode_hhmm", "set_flag",
    "_signed16", "_lookup", "_words",
    "_bit_texts", "_decode_words_bitfield",
]
