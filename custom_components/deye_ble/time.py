"""Time entities built from model point descriptors."""
from __future__ import annotations

from datetime import time as dt_time

from homeassistant.components.time import TimeEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_LOGGER_SN, DOMAIN
from .entity import device_info
from .models.base import Time as TimeDescription


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    entities = [
        DeyeModelTime(coordinator, entry, point)
        for point in coordinator.model.write_points
        if any(isinstance(show, TimeDescription) for show in point.shows)
    ]
    async_add_entities(entities)


class DeyeModelTime(CoordinatorEntity, TimeEntity):
    """Writable time value described by one model point."""

    _attr_has_entity_name = True

    def __init__(self, coordinator, entry: ConfigEntry, point):
        super().__init__(coordinator)
        description = next(
            show for show in point.shows if isinstance(show, TimeDescription)
        )
        if point.write is None:
            raise ValueError(f"time point {point.key!r} has no write descriptor")
        self._key = point.key
        self._write = point.write
        self._attr_name = description.name
        self._attr_icon = description.icon
        self._attr_unique_id = f"{entry.data[CONF_LOGGER_SN]}_{point.key}"
        self._attr_device_info = device_info(entry.data[CONF_LOGGER_SN])

    @property
    def native_value(self) -> dt_time | None:
        value = (self.coordinator.data or {}).get(self._key)
        if not value:
            return None
        try:
            return dt_time.fromisoformat(value)
        except (TypeError, ValueError):
            return None

    async def async_set_value(self, value: dt_time) -> None:
        time_string = value.strftime("%H:%M")
        current = self.coordinator.data or {}
        writes = self._write.values(time_string, current)
        if len(writes) == 1:
            register, raw_value = next(iter(writes.items()))
            await self.coordinator.async_write(register, raw_value)
        else:
            await self.coordinator.async_write_many(writes)
        decoded = self.coordinator.model.decode(
            {register: [raw_value] for register, raw_value in writes.items()},
        )
        self.coordinator.async_set_updated_data(
            {**current, **decoded, self._key: time_string},
        )
        self.coordinator.mark_config_dirty()
