"""Tests for the pure daily_calc helper."""
from __future__ import annotations

from datetime import date

from custom_components.deye_ble.helpers import daily_calc

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
