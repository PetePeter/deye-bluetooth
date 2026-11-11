"""Shared captured telemetry plus slow-read frames for the three-phase model."""
from __future__ import annotations

from custom_components.deye_ble import models, protocol
from tests.test_registers import CLOCK_WORDS, FRAMES, GRID_FREQUENCY_WORDS


def _zero_frame(count: int) -> str:
    body = bytes([protocol.SLAVE, protocol.FUNC_READ, 2 * count]) + bytes(2 * count)
    return "+ok=" + (body + protocol.crc16(body)).hex().upper()


# Existing captured telemetry frames plus valid zero-valued replies for slow
# reads, as used by the coordinator's full-poll fixture.
FULL_FRAMES = {
    **{start: _zero_frame(count) for start, count in models.SLOW_BLOCKS},
    **FRAMES,
}


def decoded_words() -> dict[int, list[int]]:
    """Return the shared frames as a poll snapshot with the captured clock."""
    words = {
        start: protocol.parse_read(frame)
        for start, frame in FULL_FRAMES.items()
    }
    words.update(GRID_FREQUENCY_WORDS)
    words[models.REG_CLOCK] = list(CLOCK_WORDS)
    return words
