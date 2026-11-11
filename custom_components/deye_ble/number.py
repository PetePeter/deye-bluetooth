"""Number entities — write-through controls for inverter settings."""
from __future__ import annotations

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_CLOUD_SYNC_INTERVAL,
    CONF_CLOUD_WINDOW,
    CONF_LIVE_INTERVAL,
    CONF_LOGGER_SN,
    CONF_SCAN_INTERVAL,
    CONF_SLOW_INTERVAL,
    DEFAULT_CLOUD_SYNC_INTERVAL,
    DEFAULT_CLOUD_WINDOW,
    DEFAULT_LIVE_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SLOW_INTERVAL,
    DOMAIN,
    MAX_CLOUD_SYNC_INTERVAL,
    MAX_CLOUD_WINDOW,
    MAX_LIVE_INTERVAL,
    MAX_SCAN_INTERVAL,
    MAX_SLOW_INTERVAL,
    MIN_CLOUD_SYNC_INTERVAL,
    MIN_CLOUD_WINDOW,
    MIN_LIVE_INTERVAL,
    MIN_SCAN_INTERVAL,
    MIN_SLOW_INTERVAL,
)
from .entity import OptionEntity, device_info
from .models.base import Number as NumberDescription


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    sn = entry.data[CONF_LOGGER_SN]
    entities: list[NumberEntity] = [cls(coordinator, entry) for cls in OPTION_NUMBERS]
    for point in coordinator.model.write_points:
        if any(isinstance(show, NumberDescription) for show in point.shows):
            entities.append(DeyeModelNumber(coordinator, entry, point))
    async_add_entities(entities)


class DeyeModelNumber(CoordinatorEntity, NumberEntity):
    """Editable number built from one model point and its verified write."""

    _attr_has_entity_name = True

    def __init__(self, coordinator, entry: ConfigEntry, point):
        super().__init__(coordinator)
        description = next(
            show for show in point.shows if isinstance(show, NumberDescription)
        )
        if point.write is None:
            raise ValueError(f"number point {point.key!r} has no write descriptor")
        self._key = point.key
        self._write = point.write
        self._attr_name = description.name
        self._attr_unique_id = f"{entry.data[CONF_LOGGER_SN]}_{point.key}"
        self._attr_native_unit_of_measurement = description.unit
        self._attr_native_min_value = description.minimum
        self._attr_native_max_value = description.maximum
        self._attr_native_step = description.step
        self._attr_mode = NumberMode(description.mode)
        self._attr_device_class = (
            NumberDeviceClass(description.device_class) if description.device_class else None
        )
        if description.icon:
            self._attr_icon = description.icon
        self._attr_device_info = device_info(entry.data[CONF_LOGGER_SN])

    @property
    def native_value(self) -> float | None:
        value = (self.coordinator.data or {}).get(self._key)
        return float(value) if value is not None else None

    async def async_set_native_value(self, value: float) -> None:
        int_value = int(value)
        writes = self._write.values(int_value, self.coordinator.data or {})
        if len(writes) == 1:
            register, raw_value = next(iter(writes.items()))
            await self.coordinator.async_write(register, raw_value)
        else:
            await self.coordinator.async_write_many(writes)
        self.coordinator.async_set_updated_data(
            {**(self.coordinator.data or {}), self._key: int_value},
        )
        self.coordinator.mark_config_dirty()


class _OptionNumber(OptionEntity, NumberEntity):
    _attr_native_unit_of_measurement = "s"
    _attr_device_class = NumberDeviceClass.DURATION
    _attr_mode = NumberMode.BOX
    _attr_native_step = 1

    @property
    def native_value(self) -> float:
        return float(self.option_value)

    async def async_set_native_value(self, value: float) -> None:
        await self.async_set_option(int(value))


class DeyeScanInterval(_OptionNumber):
    _option, _default = CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
    _attr_name = "Standard Poll Interval"
    _attr_icon = "mdi:timer-outline"
    _attr_native_min_value, _attr_native_max_value = MIN_SCAN_INTERVAL, MAX_SCAN_INTERVAL


class DeyeLiveInterval(_OptionNumber):
    _option, _default = CONF_LIVE_INTERVAL, DEFAULT_LIVE_INTERVAL
    _attr_name = "Live Poll Interval"
    _attr_icon = "mdi:timer-play-outline"
    _attr_native_min_value, _attr_native_max_value = MIN_LIVE_INTERVAL, MAX_LIVE_INTERVAL


class DeyeSlowInterval(_OptionNumber):
    _option, _default = CONF_SLOW_INTERVAL, DEFAULT_SLOW_INTERVAL
    _attr_name = "Slow Poll Interval"
    _attr_icon = "mdi:timer-sand"
    _attr_native_min_value, _attr_native_max_value = MIN_SLOW_INTERVAL, MAX_SLOW_INTERVAL
    _attr_native_step = 60


class DeyeCloudSyncInterval(_OptionNumber):
    _option, _default = CONF_CLOUD_SYNC_INTERVAL, DEFAULT_CLOUD_SYNC_INTERVAL
    _attr_name = "Cloud Quiet Window Every"
    _attr_icon = "mdi:cloud-sync-outline"
    _attr_native_min_value, _attr_native_max_value = MIN_CLOUD_SYNC_INTERVAL, MAX_CLOUD_SYNC_INTERVAL


class DeyeCloudWindow(_OptionNumber):
    _option, _default = CONF_CLOUD_WINDOW, DEFAULT_CLOUD_WINDOW
    _attr_name = "Cloud Quiet Window Length"
    _attr_icon = "mdi:cloud-clock-outline"
    _attr_native_min_value, _attr_native_max_value = MIN_CLOUD_WINDOW, MAX_CLOUD_WINDOW


OPTION_NUMBERS: list[type[_OptionNumber]] = [
    DeyeScanInterval, DeyeLiveInterval, DeyeSlowInterval, DeyeCloudSyncInterval, DeyeCloudWindow,
]
