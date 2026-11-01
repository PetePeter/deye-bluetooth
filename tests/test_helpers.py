"""Tests for the pure daily_calc and infer_grid_connected helpers."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from custom_components.deye_ble import registers as r
from custom_components.deye_ble.helpers import (
    clock_drift_seconds,
    clock_within_tolerance,
    daily_calc,
    hold_spurious_total_resets,
    infer_grid_connected,
    register_to_key,
    time_of_day_drift_seconds,
)

_TODAY = date(2025, 6, 15)


def test_first_run_of_day():
    baseline, day, value = daily_calc(None, None, 1234.5, _TODAY)
    assert baseline == 1234.5
    assert day == _TODAY
    assert value == 0.0


def test_same_day_increment():
    baseline, day, value = daily_calc(1234.5, _TODAY, 1240.7, _TODAY)
    assert baseline == 1234.5
    assert day == _TODAY
    assert value == 6.2


def test_new_day_rebaseline():
    tomorrow = date(2025, 6, 16)
    baseline, day, value = daily_calc(1240.7, _TODAY, 1260.0, tomorrow)
    assert baseline == 1260.0
    assert day == tomorrow
    assert value == 0.0


def test_counter_backwards_rebaseline():
    baseline, day, value = daily_calc(1240.7, _TODAY, 1200.0, _TODAY)
    assert baseline == 1200.0
    assert day == _TODAY
    assert value == 0.0


def test_total_none_returns_none():
    baseline, day, value = daily_calc(1234.5, _TODAY, None, _TODAY)
    assert baseline == 1234.5
    assert day == _TODAY
    assert value is None


def test_rounding():
    baseline, day, value = daily_calc(1000.0, _TODAY, 1000.005, _TODAY)
    assert value == 0.0  # rounds to 2 dp
    baseline, day, value = daily_calc(1000.0, _TODAY, 1000.006, _TODAY)
    assert value == 0.01


# --- infer_grid_connected ---------------------------------------------------

def test_grid_connected_when_any_phase_energised():
    data = {"grid_voltage_l1": 232.3, "grid_voltage_l2": 233.2, "grid_voltage_l3": 233.8}
    assert infer_grid_connected(data) is True


def test_grid_disconnected_when_all_phases_collapsed():
    data = {"grid_voltage_l1": 0.0, "grid_voltage_l2": 0.3, "grid_voltage_l3": 0.0}
    assert infer_grid_connected(data) is False


def test_grid_connected_true_if_a_single_phase_present():
    # Single-phase grid / one phase still up -> connected.
    data = {"grid_voltage_l1": 230.0, "grid_voltage_l2": 0.0, "grid_voltage_l3": 0.0}
    assert infer_grid_connected(data) is True


def test_grid_disconnected_when_frequency_collapses_but_voltage_holds():
    # Live 2026-11-01 07:44 UTC: grid lost, every phase held ~229.6 V while
    # grid frequency and grid power read 0.
    data = {"grid_voltage_l1": 229.6, "grid_voltage_l2": 229.7,
            "grid_voltage_l3": 229.6, "grid_frequency": 0.0}
    assert infer_grid_connected(data) is False


def test_grid_connected_with_nominal_frequency():
    data = {"grid_voltage_l1": 232.3, "grid_frequency": 50.0}
    assert infer_grid_connected(data) is True


@pytest.mark.parametrize("override", [
    {"grid_connected": False},  # relay open
    {"grid_frequency": 0.0},
    {"grid_voltage": 0.0},
])
def test_single_phase_needs_relay_frequency_and_voltage(override):
    healthy = {"grid_connected": True, "grid_frequency": 50.0, "grid_voltage": 241.1}
    assert infer_grid_connected(healthy) is True
    assert infer_grid_connected({**healthy, **override}) is False


def test_grid_connected_none_when_no_voltage_keys():
    # No grid-voltage reading -> unknown, not a false "disconnected".
    assert infer_grid_connected({}) is None
    assert infer_grid_connected({"solar_power": 100}) is None


# --- hold_spurious_total_resets ---------------------------------------------

def test_spurious_zero_total_held_at_last_good():
    prev = {"total_grid_import": 2644.6}
    current = hold_spurious_total_resets(prev, {"total_grid_import": 0.0})
    assert current["total_grid_import"] == 2644.6


def test_all_four_blipped_totals_held_simultaneously():
    prev = {
        "total_grid_import": 2644.6,
        "total_battery_charge": 2377.7,
        "total_battery_discharge": 2236.5,
        "total_grid_export": 635.1,
    }
    current = hold_spurious_total_resets(prev, {k: 0.0 for k in prev})
    assert current == prev


def test_genuine_increase_passes_through():
    prev = {"total_grid_import": 2644.6}
    current = hold_spurious_total_resets(prev, {"total_grid_import": 2674.9})
    assert current["total_grid_import"] == 2674.9


def test_non_total_zero_is_not_held():
    # Instantaneous power legitimately reads 0 — never held.
    prev = {"grid_power": 1300}
    current = hold_spurious_total_resets(prev, {"grid_power": 0})
    assert current["grid_power"] == 0


def test_no_prev_passes_through_unchanged():
    # First cycle: nothing to hold against.
    current = hold_spurious_total_resets(None, {"total_grid_import": 0.0})
    assert current["total_grid_import"] == 0.0
    current = hold_spurious_total_resets({}, {"total_grid_import": 0.0})
    assert current["total_grid_import"] == 0.0


def test_missing_total_in_current_is_skipped():
    # Key not read this cycle -> no KeyError, left absent.
    prev = {"total_grid_import": 2644.6}
    current = hold_spurious_total_resets(prev, {"grid_power": 100})
    assert "total_grid_import" not in current


def test_zero_prev_does_not_hold():
    # Legitimate cold start at 0 -> a later 0 is not treated as a reset.
    prev = {"total_grid_import": 0.0}
    current = hold_spurious_total_resets(prev, {"total_grid_import": 0.0})
    assert current["total_grid_import"] == 0.0


# --- Inverter clock drift ---------------------------------------------------

def test_drift_positive_when_inverter_ahead():
    drift = clock_drift_seconds(
        datetime(2026, 8, 9, 10, 5, 30), datetime(2026, 8, 9, 10, 0, 0),
    )
    assert drift == 330


def test_drift_negative_when_inverter_behind():
    drift = clock_drift_seconds(
        datetime(2025, 8, 9, 10, 40, 20), datetime(2025, 8, 9, 10, 41, 50),
    )
    assert drift == -90


def test_drift_none_when_clock_unread():
    # Missing reading must not read as "perfectly in sync".
    assert clock_drift_seconds(None, datetime(2026, 8, 9, 10, 0, 0)) is None


def test_drift_ignores_timezone_on_now():
    # HA's dt_util.now() is tz-aware; the inverter reports bare wall clock. The
    # aware side is stripped, not converted — there is nothing to convert to.
    aware = datetime(2026, 8, 9, 10, 0, 0, tzinfo=timezone(timedelta(hours=10)))
    assert clock_drift_seconds(datetime(2026, 8, 9, 10, 0, 30), aware) == 30


def test_clock_registers_are_never_reasserted():
    # The RTC legitimately changes every second, so it must never enter the
    # local-wins drift-correction path — that would fight the inverter forever.
    for offset in range(r.CLOCK_WORD_COUNT):
        assert register_to_key(r.REG_CLOCK + offset) is None


# --- Time-of-day drift (date ignored, wrapped) ------------------------------

def test_time_of_day_drift_ignores_a_wrong_year():
    # The live fault: RTC a year behind AND ~89 minutes fast. Total drift is
    # ~-365 days, which buries the minutes; this is the sensor that sees them.
    drift = time_of_day_drift_seconds(
        datetime(2025, 8, 9, 10, 54, 43), datetime(2026, 8, 9, 9, 25, 19),
    )
    assert drift == 89 * 60 + 24


def test_time_of_day_drift_positive_when_inverter_ahead():
    drift = time_of_day_drift_seconds(
        datetime(2026, 8, 9, 10, 5, 30), datetime(2026, 8, 9, 10, 0, 0),
    )
    assert drift == 330


def test_time_of_day_drift_negative_when_inverter_behind():
    drift = time_of_day_drift_seconds(
        datetime(2026, 8, 9, 9, 58, 30), datetime(2026, 8, 9, 10, 0, 0),
    )
    assert drift == -90


@pytest.mark.parametrize("inverter,now,expected", [
    # Two minutes apart across midnight: the short way round, not 1438 minutes
    # the wrong way. A wrap artefact here would mimic the very event we hunt.
    (datetime(2026, 8, 10, 0, 1, 0), datetime(2026, 8, 9, 23, 59, 0), 120),
    (datetime(2026, 8, 9, 23, 59, 0), datetime(2026, 8, 10, 0, 1, 0), -120),
    # Same instant of day, different dates -> no time-of-day drift at all.
    (datetime(2025, 1, 1, 12, 0, 0), datetime(2026, 8, 9, 12, 0, 0), 0),
])
def test_time_of_day_drift_wraps_the_short_way(inverter, now, expected):
    assert time_of_day_drift_seconds(inverter, now) == expected


def test_time_of_day_drift_never_exceeds_half_a_day():
    # Every possible offset stays inside +/-12h, so the sensor can never report
    # a wrap artefact of up to 24 hours.
    now = datetime(2026, 8, 9, 0, 0, 0)
    for minute in range(0, 24 * 60, 7):
        inverter = now + timedelta(minutes=minute)
        assert -43200 <= time_of_day_drift_seconds(inverter, now) <= 43200


def test_time_of_day_drift_none_when_clock_unread():
    assert time_of_day_drift_seconds(None, datetime(2026, 8, 9, 10, 0, 0)) is None


def test_time_of_day_drift_ignores_timezone_on_now():
    aware = datetime(2026, 8, 9, 10, 0, 0, tzinfo=timezone(timedelta(hours=10)))
    assert time_of_day_drift_seconds(datetime(2026, 8, 9, 10, 0, 30), aware) == 30


# --- Clock-write verification -----------------------------------------------
# The clock is the one control that cannot use the strict verify_readback every
# other register uses: it ticks while the sequence runs, so the readback is
# *expected* to differ from what was written. It is accepted on drift instead.

_VERIFY_NOW = datetime(2026, 8, 9, 10, 0, 0)
_TOLERANCE = 90


def test_verify_accepts_a_readback_that_has_ticked_on():
    # The RTC advances during the commit + settle; a few seconds is success.
    readback = r.encode_clock(_VERIFY_NOW + timedelta(seconds=8))
    assert clock_within_tolerance(readback, _VERIFY_NOW, _TOLERANCE) is True


def test_verify_rejects_the_observed_year_corruption():
    # Regression test for the real fault: the year byte 0x1A landed as 0x7A,
    # giving 2122, with month/day/hour/minute/second all correct. Everything a
    # field-by-field check would compare still matches.
    corrupted = r.encode_clock(_VERIFY_NOW)
    corrupted[0] = (corrupted[0] & 0x00FF) | (0x7A << 8)
    assert clock_within_tolerance(corrupted, _VERIFY_NOW, _TOLERANCE) is False


def test_verify_rejects_an_undecodable_readback():
    # Month 13 decodes to nothing. "Couldn't tell" must never mean "accept".
    assert clock_within_tolerance([0x1A0D, 0x0908, 0x3800], _VERIFY_NOW, _TOLERANCE) is False


def test_verify_rejects_a_short_readback():
    assert clock_within_tolerance([0x1A08, 0x0908], _VERIFY_NOW, _TOLERANCE) is False


@pytest.mark.parametrize("offset,expected", [
    (90, True), (-90, True),     # exactly at tolerance is still a pass
    (91, False), (-91, False),   # one second past it is not
])
def test_verify_tolerance_boundary(offset, expected):
    readback = r.encode_clock(_VERIFY_NOW + timedelta(seconds=offset))
    assert clock_within_tolerance(readback, _VERIFY_NOW, _TOLERANCE) is expected


def test_verify_ignores_timezone_on_now():
    # dt_util.now() is aware; the inverter reports a bare wall clock.
    aware = datetime(2026, 8, 9, 10, 0, 0, tzinfo=timezone(timedelta(hours=10)))
    readback = r.encode_clock(datetime(2026, 8, 9, 10, 0, 5))
    assert clock_within_tolerance(readback, aware, _TOLERANCE) is True
