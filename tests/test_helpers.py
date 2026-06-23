"""Tests for the pure daily_calc and infer_grid_connected helpers."""
from __future__ import annotations

from datetime import date

from custom_components.deye_ble.helpers import daily_calc, infer_grid_connected

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


def test_grid_connected_none_when_no_voltage_keys():
    # No grid-voltage reading -> unknown, not a false "disconnected".
    assert infer_grid_connected({}) is None
    assert infer_grid_connected({"solar_power": 100}) is None
