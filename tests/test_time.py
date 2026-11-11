"""Model time controls are exposed and encoded from their descriptors."""
from __future__ import annotations

from datetime import time as dt_time

import pytest

pytest.importorskip("homeassistant")

from custom_components.deye_ble.const import CONF_LOGGER_SN
from custom_components.deye_ble.models import get
from custom_components.deye_ble.time import DeyeModelTime


class FakeCoordinator:
    def __init__(self, model_id, data=None):
        self.model = get(model_id)
        self.data = data or {}
        self.write_calls = []
        self.updated = None
        self.dirty = False

    async def async_write(self, register, value):
        self.write_calls.append((register, value))

    async def async_write_many(self, values):
        self.write_calls.extend(values.items())

    def async_set_updated_data(self, data):
        self.updated = data
        self.data = data

    def mark_config_dirty(self):
        self.dirty = True


class FakeEntry:
    data = {CONF_LOGGER_SN: "DEYE00000001"}


def _entity(key, value):
    coordinator = FakeCoordinator("three_phase", {key: value})
    point = next(point for point in coordinator.model.points if point.key == key)
    return DeyeModelTime(coordinator, FakeEntry(), point), coordinator


@pytest.mark.parametrize(
    ("key", "expected_register", "initial", "updated"),
    [
        ("charge_start", 149, "11:00", dt_time(15, 0)),
        ("charge_end", 150, "15:00", dt_time(23, 30)),
    ],
)
@pytest.mark.asyncio
async def test_model_time_uses_descriptor_for_value_and_write(
    key, expected_register, initial, updated,
):
    entity, coordinator = _entity(key, initial)

    assert entity.native_value == dt_time.fromisoformat(initial)
    await entity.async_set_value(updated)

    assert coordinator.write_calls == [(expected_register, updated.hour * 100 + updated.minute)]
    assert coordinator.updated[key] == updated.strftime("%H:%M")
    assert coordinator.dirty
