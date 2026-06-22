"""P5 — pure write-safety helper tests: verify_readback, detect_drift, register_to_key.

No bleak, no HA imports — all tests exercise pure functions from helpers.py.
"""
from __future__ import annotations

import pytest

from custom_components.deye_ble.helpers import detect_drift, register_to_key, verify_readback
from custom_components.deye_ble.registers import (
    REG_CHARGE_SOC,
    REG_MAX_SELL_POWER,
    REG_TOU_SLOT2_START,
    REG_TOU_SLOT3_START,
    REG_WORK_MODE,
)


# --- verify_readback --------------------------------------------------------

class TestVerifyReadback:
    def test_match_passes(self):
        assert verify_readback(0x008F, 100, 100) is None

    def test_mismatch_raises(self):
        with pytest.raises(ValueError, match="0x008F"):
            verify_readback(0x008F, 100, 50)

    def test_mismatch_includes_expected_and_actual(self):
        with pytest.raises(ValueError, match="expected 100.*got 50"):
            verify_readback(0x008F, 100, 50)

    def test_mismatch_includes_register_address(self):
        with pytest.raises(ValueError, match="0x008E"):
            verify_readback(0x008E, 0, 2)


# --- detect_drift ------------------------------------------------------------

class TestDetectDrift:
    def test_no_tracked_no_drift(self):
        assert detect_drift({}, {"work_mode": "Selling First"}) == []

    def test_match_no_drift(self):
        tracked = {REG_WORK_MODE: "Selling First"}
        current = {"work_mode": "Selling First"}
        assert detect_drift(tracked, current) == []

    def test_single_drift(self):
        tracked = {REG_MAX_SELL_POWER: 200}
        current = {"max_sell_power": 100}
        result = detect_drift(tracked, current)
        assert len(result) == 1
        assert result[0] == (REG_MAX_SELL_POWER, 200)

    def test_work_mode_raw_int_compared_to_label(self):
        """Tracked work_mode as raw int (0) is compared to decoded label."""
        tracked = {REG_WORK_MODE: 0}
        current = {"work_mode": "Selling First"}
        assert detect_drift(tracked, current) == []

        # Int 0 vs wrong label → drift.
        current_drifted = {"work_mode": "Zero Export to Load"}
        result = detect_drift(tracked, current_drifted)
        assert len(result) == 1
        assert result[0] == (REG_WORK_MODE, 0)

    def test_multiple_drift(self):
        tracked = {REG_WORK_MODE: 0, REG_MAX_SELL_POWER: 200}
        current = {"work_mode": "Zero Export to Load", "max_sell_power": 100}
        result = detect_drift(tracked, current)
        regs = {reg for reg, _ in result}
        assert REG_WORK_MODE in regs
        assert REG_MAX_SELL_POWER in regs

    def test_idempotent_no_change(self):
        tracked = {REG_MAX_SELL_POWER: 200}
        current = {"max_sell_power": 100}
        first = detect_drift(tracked, current)
        second = detect_drift(tracked, current)
        assert first == second

    def test_partial_current_missing_key_no_drift(self):
        tracked = {REG_MAX_SELL_POWER: 200}
        current = {"work_mode": "Selling First"}
        assert detect_drift(tracked, current) == []

    def test_tracked_register_not_in_control_set_ignored(self):
        tracked = {0x0088: 42}
        current = {"max_sell_power": 100}
        assert detect_drift(tracked, current) == []


# --- register_to_key ---------------------------------------------------------

class TestRegisterToKey:
    def test_known_control_registers(self):
        assert register_to_key(REG_WORK_MODE) == "work_mode"
        assert register_to_key(REG_MAX_SELL_POWER) == "max_sell_power"
        assert register_to_key(REG_TOU_SLOT2_START) == "charge_start"
        assert register_to_key(REG_TOU_SLOT3_START) == "charge_end"
        assert register_to_key(REG_CHARGE_SOC) == "charge_soc"

    def test_unknown_register_returns_none(self):
        assert register_to_key(0x0000) is None
