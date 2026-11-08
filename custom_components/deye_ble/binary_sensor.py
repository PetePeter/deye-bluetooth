"""Binary sensor entities — grid-connection state and clock-sync health."""
from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_LOGGER_SN, DOMAIN
from .entity import device_info
from .helpers import infer_grid_connected
from .models.base import BinarySensor as BinarySensorDescription


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    model = coordinator.model
    entities: list[BinarySensorEntity] = []
    for point in model.points:
        for description in point.shows:
            if isinstance(description, BinarySensorDescription):
                entities.append(DeyeFlag(coordinator, entry, point.key, description))

    if any(key.startswith("grid_voltage") for key in (point.key for point in model.points)):
        entities.append(DeyeGridConnected(coordinator, entry))
    if model.capabilities.cloud_time_sync:
        entities.append(DeyeCloudClockSyncEnabled(coordinator, entry))
    async_add_entities(entities)



class DeyeGridConnected(CoordinatorEntity, BinarySensorEntity):
    """On-grid state, inferred from grid frequency and the per-phase voltages.

    There is no verified three-phase grid-relay register over BLE, so this is
    derived (see helpers.infer_grid_connected).
    """

    _attr_has_entity_name = True
    _attr_name = "Grid Connected"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_icon = "mdi:transmission-tower"

    def __init__(self, coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        sn = entry.data[CONF_LOGGER_SN]
        self._attr_unique_id = f"{sn}_grid_connected"
        self._attr_device_info = device_info(sn)

    @property
    def is_on(self) -> bool | None:
        return infer_grid_connected(self.coordinator.data or {})


class DeyeCloudClockSyncEnabled(CoordinatorEntity, BinarySensorEntity):
    """Time Sync (0x00E4 bit 0) — whether cloud clock calibration is allowed.

    OFF means the inverter refuses the logger's cloud time calibration: its RTC
    free-runs (~3 s/day slow) until something writes it. Nothing in the Deye app
    shows this, and setting the time from the app is what silently turns it off —
    the inverter ended up a full year out that way, uncorrected for months.
    Exposed so the state is visible instead of inferred from drift after the fact.
    """

    _attr_has_entity_name = True
    _attr_name = "Cloud Clock Sync Enabled"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:cloud-clock"

    def __init__(self, coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        sn = entry.data[CONF_LOGGER_SN]
        self._attr_unique_id = f"{sn}_time_sync"
        self._attr_device_info = device_info(sn)

    @property
    def is_on(self) -> bool | None:
        return (self.coordinator.data or {}).get("time_sync")


class DeyeFlag(CoordinatorEntity, BinarySensorEntity):
    """A boolean value exposed by the current model's point descriptors."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator,
        entry: ConfigEntry,
        key: str,
        description: BinarySensorDescription,
    ):
        super().__init__(coordinator)
        self._key = key
        sn = entry.data[CONF_LOGGER_SN]
        self._attr_name = description.name
        self._attr_unique_id = f"{sn}_{key}"
        self._attr_device_class = (
            BinarySensorDeviceClass(description.device_class)
            if description.device_class else None
        )
        if description.icon:
            self._attr_icon = description.icon
        if description.entity_category:
            self._attr_entity_category = EntityCategory(description.entity_category)
        self._attr_device_info = device_info(sn)

    @property
    def is_on(self) -> bool | None:
        return (self.coordinator.data or {}).get(self._key)
