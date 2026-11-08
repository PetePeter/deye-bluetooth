from custom_components.deye_ble import codec


def test_s32_uses_low_word_first_and_twos_complement():
    assert codec.u32(0x5678, 0x1234) == 0x12345678
    assert codec.s32(0xFFFE, 0xFFFF) == -2


def test_scaled_temperature_subtracts_offset_before_scaling():
    assert codec.scaled(codec.u16(1023), 0.1, offset=1000) == 2.3


def test_bitfield_text_decodes_low_word_first_and_unknown_bits():
    words = {101: [0x0005, 0x0002]}
    bits = {0: "Grid overvoltage", 2: "PV overcurrent", 17: "Unexpected high bit"}

    assert codec.bitfield_text(words, 101, 2, bits) == (
        0x00020005,
        "Grid overvoltage, PV overcurrent, Unexpected high bit",
    )
