"""Model select entities use each point's labels and verified write."""
from __future__ import annotations

import pytest

pytest.importorskip("homeassistant")

from custom_components.deye_ble.const import CONF_LOGGER_SN
from custom_components.deye_ble.models import get
from custom_components.deye_ble.select import DeyeModelSelect


class FakeCoordinator:
    def __init__(self, profile, data=None):
        self.model = get(profile)
        self.data = data or {}
        self.write_calls = []
        self.updated = None
        self.dirty = False

    async def async_write(self, reg, value):
        self.write_calls.append((reg, value))

    async def async_write_many(self, values):
        self.write_calls.extend(values.items())

    def async_set_updated_data(self, data):
        self.updated = data
        self.data = data

    def mark_config_dirty(self):
        self.dirty = True


class FakeEntry:
    data = {CONF_LOGGER_SN: "DEYE00000001"}


def _entity(profile, key):
    coordinator = FakeCoordinator(profile)
    point = next(point for point in coordinator.model.points if point.key == key)
    return DeyeModelSelect(coordinator, FakeEntry(), point), coordinator


@pytest.mark.asyncio
async def test_single_phase_select_encodes_label_and_updates_current_option():
    entity, coordinator = _entity("single_phase", "work_mode")

    await entity.async_select_option("Selling First")

    assert coordinator.write_calls == [(244, 0)]
    assert coordinator.updated["work_mode"] == "Selling First"
    assert entity.current_option == "Selling First"
    assert coordinator.dirty


def test_model_select_options_come_from_its_descriptor():
    entity, coordinator = _entity("single_phase", "energy_pattern")
    point = next(
        point for point in coordinator.model.points if point.key == "energy_pattern"
    )
    description = point.show

    assert entity.options == list(description.options.values())
